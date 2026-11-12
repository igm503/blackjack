// Layered split solver.
//
// Computes exactly the same quantity as SplitSolverTD (full-information
// optimal play of all split hands, every line of play enumerated) but is
// organised for throughput:
//
//   * States are grouped by R, the multiset of cards drawn since the split.
//     Every transition draws exactly one card, so the states with |R| = k
//     ("layer k") depend only on layer k+1.
//   * A forward pass discovers which states are reachable in each layer.
//     Layers are kept sorted by the packed key of R; since R -> R + card is
//     a monotone map on keys, layer k+1 is produced by a 10-way merge of
//     layer k with no hashing.
//   * A backward pass computes values layer by layer. Children are located
//     with ten monotone cursors into the next layer, again with no hashing,
//     and each layer is split across threads.
//   * One dealer evaluation is shared by every state with the same R.
//   * Which states are reachable does not depend on the dealer's upcard, so
//     the forward pass can be done once per pair (plan) and reused for every
//     upcard (evaluate); evaluations of one plan may run concurrently.
#pragma once

#include <atomic>
#include <chrono>
#include <memory>
#include <stdexcept>
#include <string>

#include "bj.hpp"

namespace bj {

class LayeredSplitSolver {
 public:
  // Where in the split to start solving from. The default (lone hand, one
  // more waiting) is the position right after a pair has been split.
  struct Root {
    bool lone = true;     // current hand holds one pair card and awaits its second
    int second = -1;      // >= 0: current hand is the two cards [pair, second]
    int pips = 0;         // otherwise: 3+ card hand, pip count with ace = 1 ...
    bool soft = false;    // ... and whether an ace is currently counted as 11
    int pending = 1;      // hands still waiting for their second card
    int splits_left = -1; // -1: rules.max_splits - 1
  };
  struct Result {
    double value = 0;  // optimal expected return from the root state onwards
    double stand = NEG_INF, hit = NEG_INF, dbl = NEG_INF;  // filled for hand roots
    size_t nodes = 0, values = 0, dealer_evals = 0, dealer_shortcuts = 0;
    int layers = 0;
    double forward_seconds = 0, backward_seconds = 0;
  };

  LayeredSplitSolver(const Rules &rules, const DealerTable &dealer, int threads = 1)
      : rules_(rules), dealer_(dealer), threads_(threads) {
    L_ = rules.max_splits;
    if (L_ > 6) throw std::invalid_argument("max_splits > 6 not supported");
    NC_ = 0;
    for (int s = 0; s <= L_; ++s)
      for (int m = 0; m + s <= L_; ++m) {
        ms_[m][s] = NC_;
        cm_[NC_] = m;
        cs_[NC_] = s;
        ++NC_;
      }
    NH_ = 0;
    for (int c = 0; c < NC_; ++c) hidx_[c] = -1;
    // hand states: (0,0) plus every combo with m >= 1
    for (int c = 0; c < NC_; ++c)
      if (cm_[c] >= 1 || cs_[c] == 0) {
        hidx_[c] = NH_;
        hm_[NH_] = cm_[c];
        hs_[NH_] = cs_[c];
        ++NH_;
      }
    for (int c = 0; c < NC_; ++c) hcanon_[c] = cm_[c] == 0 ? hidx_[ms_[0][0]] : hidx_[c];
    // state transition table and self-test of the mask shifts
    for (int b = 0; b < 32; ++b)
      for (int c = 0; c < NR; ++c) {
        trans_[b][c] = -1;
        if (b == 21 || (b >= 22 && b - 20 < 2)) continue;
        int pips = b < 22 ? b : b - 20;
        bool soft = b >= 22;
        int np = pips + c + 1;
        bool nsoft = (soft || c == ACE) && np <= 11;
        if (np <= 21) trans_[b][c] = int8_t(nsoft ? np + 20 : np);
      }
    for (int b = 0; b < 32; ++b)
      for (int c = 0; c < NR; ++c) {
        int nb = trans_[b][c];
        trans2_[b][c] = uint8_t(nb < 0 ? 32 : nb);
        dcls_[b][c] = uint8_t(nb < 0 ? 6 : stand_class(nb < 22 ? nb : nb - 10));
      }
    for (int b = 0; b < 32; ++b) {
      if (!((HITTABLE >> b) & 1)) continue;
      if (b >= 22 && b - 20 < 2) continue;
      for (int c = 0; c < NR; ++c) {
        bool bust;
        uint32_t nh = shift(1u << b, c, bust);
        uint32_t want = trans_[b][c] < 0 ? 0u : 1u << trans_[b][c];
        if (nh != want || bust != (trans_[b][c] < 0)) throw std::logic_error("shift self-test");
      }
    }
  }

  struct Layer {
    std::vector<uint64_t> key;
    std::vector<uint32_t> w;  // one bit per (m,s) "needs a card" state, plus GNEED
    std::vector<uint32_t> h;  // NH per node: 3+ card hand states
    std::vector<uint16_t> f;  // NH per node: two-card hand states, by second card
    size_t size() const { return key.size(); }
  };

  // Result of the forward pass: the reachable states of every layer.
  struct Plan {
    std::vector<Layer> layers;
    Counts supply{};       // cards assumed available when the plan was built
    int pair = 0;
    Root root;
    size_t nodes = 0, max_nodes = 0;
    uint64_t max_vals = 0, values = 0;
    double seconds = 0;
    // bytes of working memory one evaluate() call needs
    size_t eval_bytes(int nh) const { return 2 * (8 * size_t(max_vals) + (8 + 2 * size_t(nh)) * (max_nodes + 1)); }
  };
  int hand_combos() const { return NH_; }

  // Forward pass. `supply` must contain at least every card that will be
  // unseen in any later evaluate() call (pass the shoe without the upcard
  // removed to share one plan between upcards). If upcard >= 0 the plan is
  // specialised to that upcard's hole-card constraint.
  Plan plan(const Counts &supply, int pair_rank, const Root *root_in = nullptr, int upcard = -1) {
    auto t0 = std::chrono::steady_clock::now();
    Root root = root_in ? *root_in : Root{};
    if (root.splits_left < 0) root.splits_left = L_ - 1;
    if (L_ < 1 || root.pending < 0 || root.pending + root.splits_left > L_)
      throw std::invalid_argument("bad split root");
    PlanCtx pc;
    pc.S = supply;
    pc.N = total_cards(supply);
    pc.forb = upcard >= 0 ? forbidden_hole(upcard) : -1;
    set_pair(pc.pair, pair_rank);

    Plan P;
    P.supply = supply;
    P.pair = pair_rank;
    P.root = root;
    P.layers.resize(1);
    {
      Layer &l0 = P.layers[0];
      l0.key.assign(1, 0);
      l0.w.assign(1, 0);
      l0.h.assign(NH_, 0);
      l0.f.assign(NH_, 0);
      int ms = ms_[root.pending][root.splits_left];
      if (root.lone) {
        l0.w[0] = 1u << ms;
      } else if (root.second >= 0) {
        l0.f[hcanon_[ms]] = uint16_t(1u << root.second);
      } else {
        l0.h[hcanon_[ms]] = 1u << (root.soft ? root.pips + 20 : root.pips);
      }
    }
    for (int k = 0;; ++k) {
      Layer &cur = P.layers[k];
      std::atomic<uint64_t> tot{0};
      parallel_for(cur.size(), cur.size() >= 4096 ? threads_ : 1, [&](int, size_t a, size_t b) {
        uint64_t t = 0;
        for (size_t i = a; i < b; ++i) {
          close_node(cur, i);
          uint32_t w = cur.w[i];
          t += ((w & GNEED) ? 6 : 0) + __builtin_popcount(w & WMASK);
          for (int hc = 0; hc < NH_; ++hc)
            t += __builtin_popcount(cur.h[i * NH_ + hc]) + __builtin_popcount(cur.f[i * NH_ + hc]);
        }
        tot += t;
      });
      P.nodes += cur.size();
      P.values += tot.load();
      P.max_nodes = std::max(P.max_nodes, cur.size());
      P.max_vals = std::max<uint64_t>(P.max_vals, tot.load());
      Layer next;
      build_next(pc, cur, k, next);
      if (pc.overflow.load()) throw std::overflow_error("split key overflow");
      if (next.size() == 0) break;
      P.layers.push_back(std::move(next));
    }
    P.seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    return P;
  }

  // Backward pass for one upcard. `unseen` is the exact set of cards the
  // player cannot see (hole card included). Does not modify the plan, so
  // several evaluations of one plan can run at the same time.
  Result evaluate(const Plan &P, const Counts &unseen, int upcard, int threads = -1) const {
    auto t0 = std::chrono::steady_clock::now();
    if (threads < 0) threads = threads_;
    EvalCtx cx;
    cx.S = unseen;
    cx.N = total_cards(unseen);
    cx.u = upcard;
    cx.forb = forbidden_hole(upcard);
    cx.root = P.root;
    set_pair(cx.pair, P.pair);
    for (int r = 0; r < NR; ++r)
      if (unseen[r] > P.supply[r]) throw std::invalid_argument("plan does not cover this shoe");

    Result res;
    res.nodes = P.nodes;
    res.values = P.values;
    res.layers = int(P.layers.size());
    res.forward_seconds = P.seconds;
    // The backward pass only needs the values of two adjacent layers, so two
    // buffer sets, sized once for the largest layer, are handed back and forth.
    Vals bufs[2];
    for (auto &b : bufs) {
      b.off.ensure(P.max_nodes + 1);
      b.hoff.ensure(P.max_nodes * NH_);
      b.val.ensure(P.max_vals);
    }
    std::atomic<size_t> devals{0}, dshort{0};
    const int nl = int(P.layers.size());
    for (int k = nl - 1; k >= 0; --k) {
      const Layer &cur = P.layers[k];
      Vals &cv = bufs[k & 1];
      const Layer *child = k + 1 < nl ? &P.layers[k + 1] : nullptr;
      const Vals *chv = child ? &bufs[(k + 1) & 1] : nullptr;
      size_t n = cur.size();
      int T = n >= 1024 ? threads : 1;
      // value offsets: per-chunk counts, then a short serial prefix
      std::vector<uint64_t> chunk_tot(size_t(std::max(T, 1)) + 1, 0);
      parallel_for(n, T, [&](int tid, size_t a, size_t b) {
        uint64_t t = 0;
        for (size_t i = a; i < b; ++i) {
          uint32_t w = cur.w[i];
          int rel = ((w & GNEED) ? 6 : 0) + __builtin_popcount(w & WMASK);
          for (int hc = 0; hc < NH_; ++hc) {
            cv.hoff[i * NH_ + hc] = uint16_t(rel);
            rel += __builtin_popcount(cur.h[i * NH_ + hc]) + __builtin_popcount(cur.f[i * NH_ + hc]);
          }
          cv.off[i] = t;  // relative to the chunk start for now
          t += uint64_t(rel);
        }
        chunk_tot[size_t(tid) + 1] = t;
      });
      for (size_t t = 1; t < chunk_tot.size(); ++t) chunk_tot[t] += chunk_tot[t - 1];
      parallel_for(n, T, [&](int tid, size_t a, size_t b) {
        uint64_t base = chunk_tot[size_t(tid)];
        if (base)
          for (size_t i = a; i < b; ++i) cv.off[i] += base;
        size_t de = 0, ds = 0;
        compute_range(cx, cur, cv, child, chv, k, a, b, de, ds);
        devals += de;
        dshort += ds;
      });
    }
    res.backward_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    res.dealer_evals = devals.load();
    res.dealer_shortcuts = dshort.load();
    res.value = cx.root_value;
    res.stand = cx.root_stand;
    res.hit = cx.root_hit;
    res.dbl = cx.root_dbl;
    return res;
  }

  // Plan and evaluate one position.
  Result solve(const Counts &unseen, int pair_rank, int upcard, const Root *root_in = nullptr) {
    Plan P = plan(unseen, pair_rank, root_in, upcard);
    return evaluate(P, unseen, upcard);
  }

 private:
  static constexpr uint32_t GNEED = 1u << 31;
  static constexpr uint32_t WMASK = ~GNEED;
  // hand-state bits: hard pips x -> bit x (0..21); soft pips x (2..11) -> bit 20 + x
  static constexpr uint32_t HITTABLE = 0x001FFFFFu | 0xFFC00000u;
  static constexpr int SH_[NR] = {0, 8, 15, 21, 27, 33, 38, 43, 48, 53};
  static constexpr int FW_[NR] = {8, 7, 6, 6, 6, 5, 5, 5, 5, 5};
  static constexpr int MAXNH = 28;

  // Uninitialised, reusable storage.
  template <class T>
  struct Buf {
    std::unique_ptr<T[]> p;
    size_t cap = 0;
    void ensure(size_t n) {
      if (n > cap) {
        p.reset();
        p.reset(new T[n]);
        cap = n;
      }
    }
    T &operator[](size_t i) { return p[i]; }
    const T &operator[](size_t i) const { return p[i]; }
  };
  struct Vals {
    Buf<uint64_t> off;   // per node: start of its values
    Buf<uint16_t> hoff;  // NH per node: start of each hand-state group, relative
    Buf<double> val;
  };
  // Facts about the pair being split that both passes need.
  struct PairInfo {
    int p = 0;
    bool locked = false, resplit_ok = true;
    int fbit[NR];
    bool fdbl[NR];
  };
  struct PlanCtx {
    Counts S;
    int N, forb;
    PairInfo pair;
    std::atomic<bool> overflow{false};
  };
  struct EvalCtx {
    Counts S;
    int N, u, forb;
    PairInfo pair;
    Root root;
    double root_value = 0, root_stand = NEG_INF, root_hit = NEG_INF, root_dbl = NEG_INF;
  };

  Rules rules_;
  const DealerTable &dealer_;
  int threads_;
  int L_, NC_, NH_;
  int ms_[8][8], cm_[32], cs_[32], hidx_[32], hcanon_[32], hm_[32], hs_[32];
  int8_t trans_[32][NR];
  uint8_t trans2_[32][NR];  // as trans_, with 32 for a bust
  uint8_t dcls_[32][NR];    // standing class after the draw, 6 for a bust

  void set_pair(PairInfo &pi, int p) const {
    pi.p = p;
    pi.locked = p == ACE && !rules_.hit_split_aces;
    pi.resplit_ok = p != ACE || rules_.resplit_aces;
    for (int c = 0; c < NR; ++c) {
      int pips = p + 1 + c + 1;
      bool soft = p == ACE || c == ACE;
      int total = soft ? pips + 10 : pips;
      pi.fbit[c] = soft ? pips + 20 : pips;
      bool d = rules_.das && total != 21 && !pi.locked;
      if (rules_.double_on == DoubleOn::NineToEleven && (total < 9 || total > 11)) d = false;
      if (rules_.double_on == DoubleOn::TenToEleven && (total < 10 || total > 11)) d = false;
      pi.fdbl[c] = d;
    }
  }

  static int field(uint64_t key, int r) { return int((key >> SH_[r]) & ((1ull << FW_[r]) - 1)); }
  static uint64_t unit(int r) { return 1ull << SH_[r]; }

  // Apply "draw card c" to a mask of hittable hand states.
  static uint32_t shift(uint32_t hit, int c, bool &bust) {
    uint64_t hard = hit & 0x1FFFFFu;
    uint32_t softp = (hit >> 20) & 0xFFCu;  // bit x = soft hand with pip count x
    int v = c + 1;
    uint32_t out;
    if (c == ACE) {
      uint32_t sh = uint32_t(hard << 1);
      out = ((sh & 0xFFEu) << 20) | (sh & 0x3FF000u);
      bust = false;
    } else {
      uint64_t sh = hard << v;
      out = uint32_t(sh & 0x3FFFFFu);
      bust = (sh >> 22) != 0;
    }
    uint32_t ss = softp << v;
    out |= (ss & 0xFFCu) << 20;
    out |= ss & 0x3FF000u;
    return out;
  }

  void close_node(Layer &l, size_t i) const {
    uint32_t w = l.w[i];
    for (int hc = 0; hc < NH_; ++hc)
      if (l.h[i * NH_ + hc] | l.f[i * NH_ + hc]) {
        w |= GNEED;  // standing needs the dealer distribution here
        if (hm_[hc] > 0) w |= 1u << ms_[hm_[hc] - 1][hs_[hc]];  // ... and the next hand
      }
    l.w[i] = w;
  }

  // Marks that node i of layer k pushes onto its child reached by drawing c.
  bool contrib(PlanCtx &px, const Layer &l, size_t i, int c, int k, uint32_t &w, uint32_t *h, uint16_t *f) const {
    const PairInfo &pi = px.pair;
    uint64_t key = l.key[i];
    int drawn = field(key, c);
    if (px.S[c] - drawn <= 0) return false;
    if (px.forb >= 0) {
      int fc = px.S[px.forb] - field(key, px.forb) - (c == px.forb);
      if (px.N - k - 1 - fc <= 0) return false;
    }
    if (drawn == (1 << FW_[c]) - 1) px.overflow.store(true);
    bool any = false;
    uint32_t wi = l.w[i] & WMASK;
    while (wi) {
      int ms = __builtin_ctz(wi);
      wi &= wi - 1;
      f[hcanon_[ms]] |= uint16_t(1u << c);
      any = true;
      if (c == pi.p && cs_[ms] > 0 && pi.resplit_ok) w |= 1u << ms_[cm_[ms] + 1][cs_[ms] - 1];
    }
    for (int hc = 0; hc < NH_; ++hc) {
      uint32_t hm = l.h[i * NH_ + hc];
      uint32_t fm = l.f[i * NH_ + hc];
      if (!(hm | fm)) continue;
      uint32_t all = hm;
      bool dbl = false;
      while (fm) {
        int c0 = __builtin_ctz(fm);
        fm &= fm - 1;
        all |= 1u << pi.fbit[c0];
        dbl |= pi.fdbl[c0];
      }
      bool bust = false;
      uint32_t nh = pi.locked ? 0u : shift(all & HITTABLE, c, bust);
      if (nh) {
        h[hc] |= nh;
        any = true;
      }
      if ((bust || dbl) && hm_[hc] > 0) {
        w |= 1u << ms_[hm_[hc] - 1][hs_[hc]];
        any = true;
      }
      if (dbl) {
        w |= GNEED;
        any = true;
      }
    }
    return any;
  }

  struct Out {
    std::vector<uint64_t> key;
    std::vector<uint32_t> w, h;
    std::vector<uint16_t> f;
  };

  void merge_range(PlanCtx &px, const Layer &l, int k, uint64_t lo, uint64_t hi, Out &out) const {
    const uint64_t MAX = ~0ull;
    size_t n = l.size();
    size_t idx[NR];
    uint64_t head[NR];
    auto set_head = [&](int c) {
      head[c] = idx[c] < n ? l.key[idx[c]] + unit(c) : MAX;
      if (head[c] >= hi) head[c] = MAX;
    };
    for (int c = 0; c < NR; ++c) {
      uint64_t e = unit(c);
      uint64_t target = lo > e ? lo - e : 0;
      idx[c] = size_t(std::lower_bound(l.key.begin(), l.key.end(), target) - l.key.begin());
      set_head(c);
    }
    uint32_t h[MAXNH];
    uint16_t f[MAXNH];
    while (true) {
      uint64_t mn = MAX;
      for (int c = 0; c < NR; ++c) mn = head[c] < mn ? head[c] : mn;
      if (mn == MAX) break;
      uint32_t w = 0;
      for (int j = 0; j < NH_; ++j) h[j] = 0, f[j] = 0;
      bool any = false;
      for (int c = 0; c < NR; ++c)
        if (head[c] == mn) {
          any |= contrib(px, l, idx[c], c, k, w, h, f);
          ++idx[c];
          set_head(c);
        }
      if (any) {
        out.key.push_back(mn);
        out.w.push_back(w);
        out.h.insert(out.h.end(), h, h + NH_);
        out.f.insert(out.f.end(), f, f + NH_);
      }
    }
  }

  void build_next(PlanCtx &px, const Layer &l, int k, Layer &next) const {
    size_t n = l.size();
    int T = n >= 4096 ? threads_ : 1;
    std::vector<uint64_t> bound(T + 1);
    bound[0] = 0;
    bound[T] = ~0ull;
    for (int t = 1; t < T; ++t) bound[t] = l.key[n * t / T];
    std::vector<Out> outs(T);
    parallel_for(size_t(T), T, [&](int, size_t a, size_t b) {
      for (size_t t = a; t < b; ++t) merge_range(px, l, k, bound[t], bound[t + 1], outs[t]);
    });
    if (T == 1) {
      next.key.swap(outs[0].key);
      next.w.swap(outs[0].w);
      next.h.swap(outs[0].h);
      next.f.swap(outs[0].f);
      return;
    }
    size_t tot = 0;
    for (auto &o : outs) tot += o.key.size();
    next.key.reserve(tot);
    next.w.reserve(tot);
    next.h.reserve(tot * NH_);
    next.f.reserve(tot * NH_);
    for (auto &o : outs) {
      next.key.insert(next.key.end(), o.key.begin(), o.key.end());
      next.w.insert(next.w.end(), o.w.begin(), o.w.end());
      next.h.insert(next.h.end(), o.h.begin(), o.h.end());
      next.f.insert(next.f.end(), o.f.begin(), o.f.end());
      o = Out{};
    }
  }

  [[noreturn]] static void missing(const char *what) {
    throw std::logic_error(std::string("layered solver: missing child state: ") + what);
  }

  double wval(const Layer &l, const Vals &v, size_t j, int ms) const {
    uint32_t w = l.w[j];
    if (!((w >> ms) & 1)) missing("W");
    return v.val[v.off[j] + ((w & GNEED) ? 6 : 0) + __builtin_popcount(w & ((1u << ms) - 1))];
  }
  double hval(const Layer &l, const Vals &v, size_t j, int hc, int b) const {
    uint32_t h = l.h[j * NH_ + hc];
    if (!((h >> b) & 1)) missing("H");
    return v.val[v.off[j] + v.hoff[j * NH_ + hc] + __builtin_popcount(h & ((1u << b) - 1))];
  }
  double fval(const Layer &l, const Vals &v, size_t j, int hc, int c) const {
    uint32_t f = l.f[j * NH_ + hc];
    if (!((f >> c) & 1)) missing("F");
    return v.val[v.off[j] + v.hoff[j * NH_ + hc] + __builtin_popcount(l.h[j * NH_ + hc]) +
                 __builtin_popcount(f & ((1u << c) - 1))];
  }
  const double *gval(const Layer &l, const Vals &v, size_t j) const {
    if (!(l.w[j] & GNEED)) missing("G");
    return &v.val[v.off[j]];
  }

  void compute_range(EvalCtx &cx, const Layer &l, Vals &lv, const Layer *ch, const Vals *chv, int k,
                     size_t a, size_t b, size_t &devals, size_t &dshort) const {
    const PairInfo &pi = cx.pair;
    size_t n1 = ch ? ch->size() : 0;
    size_t cur[NR];
    for (int c = 0; c < NR; ++c)
      cur[c] = ch ? size_t(std::lower_bound(ch->key.begin(), ch->key.end(), l.key[a] + unit(c)) -
                           ch->key.begin())
                  : 0;
    const int n = cx.N - k;
    // Nodes are handled in small blocks. Pass 1 locates children and fills in
    // the dealer values, batching the direct dealer evaluations so they can
    // be vectorised; pass 2 computes the state values.
    constexpr int BLOCK = 32, LANES = 4;
    struct Tmp {
      Counts rem;
      double pc[NR];
      ptrdiff_t child[NR];
      bool feasible;
    };
    Tmp tmp[BLOCK];
    struct Pending {
      double *v;
      const Counts *rem;
    };
    Pending pend[LANES];
    int npend = 0;
    auto flush = [&]() {
      if (!npend) return;
      if (!g_dealer_simd) {  // benchmark switch: one shoe at a time
        for (int j = 0; j < npend; ++j) stand_values_for(dealer_, *pend[j].rem, n, cx.u, pend[j].v);
        devals += size_t(npend);
        npend = 0;
        return;
      }
      const Counts *cp[LANES];
      int nn[LANES];
      double out[LANES][6];
      for (int j = 0; j < LANES; ++j) {
        cp[j] = pend[j < npend ? j : 0].rem;
        nn[j] = n;
      }
      dealer_probs_batch<LANES>(dealer_, cp, nn, cx.u, out);
      for (int j = 0; j < npend; ++j) stand_values(out[j], pend[j].v);
      devals += size_t(npend);
      npend = 0;
    };
    for (size_t i0 = a; i0 < b; i0 += BLOCK) {
     const size_t i1 = std::min(b, i0 + BLOCK);
     for (size_t i = i0; i < i1; ++i) {
      Tmp &t = tmp[i - i0];
      uint64_t key = l.key[i];
      Counts &rem = t.rem;
      t.feasible = true;
      for (int r = 0; r < NR; ++r) {
        rem[r] = cx.S[r] - field(key, r);
        t.feasible &= rem[r] >= 0;
      }
      // A shared plan may contain card sets this particular shoe cannot
      // produce; nothing reachable depends on them.
      if (!t.feasible) continue;
      double *pc = t.pc;
      next_card_probs(rem, n, cx.forb, pc);
      ptrdiff_t *child = t.child;
      for (int c = 0; c < NR; ++c) {
        child[c] = -1;
        if (pc[c] <= 0 || !ch) continue;
        uint64_t target = key + unit(c);
        size_t x = cur[c];
        while (x < n1 && ch->key[x] < target) ++x;
        cur[c] = x;
        if (x < n1 && ch->key[x] == target) child[c] = ptrdiff_t(x);
      }
      if (l.w[i] & GNEED) {
        double *v = &lv.val[lv.off[i]];
        // Dealer probabilities are a martingale in the cards drawn, so if
        // every child already has them, average instead of re-enumerating.
        bool all = true;
        for (int c = 0; c < NR && all; ++c)
          if (pc[c] > 0 && (child[c] < 0 || !(ch->w[child[c]] & GNEED))) all = false;
        if (all && ch) {
          for (int j = 0; j < 6; ++j) v[j] = 0;
          for (int c = 0; c < NR; ++c)
            if (pc[c] > 0) {
              const double *gc = &chv->val[chv->off[child[c]]];
              for (int j = 0; j < 6; ++j) v[j] += pc[c] * gc[j];
            }
          ++dshort;
        } else {
          pend[npend++] = {v, &rem};
          if (npend == LANES) flush();
        }
      }
     }
     flush();
     for (size_t i = i0; i < i1; ++i) {
      const Tmp &t = tmp[i - i0];
      if (!t.feasible) continue;
      const double *pc = t.pc;
      const ptrdiff_t *child = t.child;
      double *v = &lv.val[lv.off[i]];
      uint32_t wi = l.w[i];
      const double *g = (wi & GNEED) ? v : nullptr;
      int pos = (wi & GNEED) ? 6 : 0;
      // "needs a card" states
      uint32_t wb = wi & WMASK;
      double *wv = v + pos;
      int widx = 0;
      while (wb) {
        int ms = __builtin_ctz(wb);
        wb &= wb - 1;
        int m = cm_[ms], s = cs_[ms], hc = hcanon_[ms];
        bool rs = s > 0 && pi.resplit_ok;
        double acc = 0;
        for (int c = 0; c < NR; ++c) {
          if (pc[c] <= 0) continue;
          if (child[c] < 0) missing("node");
          double x = fval(*ch, *chv, child[c], hc, c);
          if (c == pi.p && rs) {
            double y = wval(*ch, *chv, child[c], ms_[m + 1][s - 1]);
            if (y > x) x = y;
          }
          acc += pc[c] * x;
        }
        wv[widx++] = acc;
        if (k == 0 && cx.root.lone) cx.root_value = acc;
      }
      // hand states
      for (int hc = 0; hc < NH_; ++hc) {
        uint32_t hm = l.h[i * NH_ + hc];
        uint32_t fm = l.f[i * NH_ + hc];
        if (!(hm | fm)) continue;
        int m = hm_[hc], s = hs_[hc];
        int nms = m > 0 ? ms_[m - 1][s] : -1;
        double nextv = m > 0 ? wval(l, lv, i, nms) : 0.0;
        // Fetch what this group needs from each child once, rather than once
        // per state: its mask and values for the same group, the value of
        // the hands still to come (cn), and its dealer values (cg).
        uint32_t hitmask = 0;
        bool anydbl = false;
        if (!pi.locked) {
          hitmask = hm & HITTABLE;
          for (uint32_t x = fm; x; x &= x - 1) {
            int c0 = __builtin_ctz(x);
            hitmask |= 1u << pi.fbit[c0];
            anydbl |= pi.fdbl[c0];
          }
        }
        // Unpack what this group needs from each child into small dense
        // tables indexed by the child's state, so the loops over states are
        // plain table lookups: dense[c][state] is the value after drawing c
        // into that state (slot 32: the hand busts), dd[c][class] the doubled
        // payoff (slot 6: bust). No popcounts and no bust-or-not branches,
        // both of which cost more than the arithmetic.
        int act[NR], nact = 0;
        double dense[NR][33], dd[NR][7];
        if (hitmask) {
          for (int c = 0; c < NR; ++c) {
            if (pc[c] <= 0) continue;
            bool bust;
            uint32_t need = shift(hitmask, c, bust);
            ptrdiff_t j = child[c];
            double cn = 0.0;  // value of the hands still to come, at the child
            if (m > 0 && (bust || anydbl)) {
              if (j < 0) missing("node");
              cn = wval(*ch, *chv, j, nms);
            }
            if (need) {
              if (j < 0) missing("node");
              uint32_t cm = ch->h[j * NH_ + hc];
              if (need & ~cm) missing("H");
              const double *src = &chv->val[chv->off[j] + chv->hoff[j * NH_ + hc]];
              for (; cm; cm &= cm - 1) dense[c][__builtin_ctz(cm)] = *src++;
            }
            dense[c][32] = cn - 1.0;
            if (anydbl) {
              dd[c][6] = cn - 2.0;
              if (need) {
                if (!(ch->w[j] & GNEED)) missing("G");
                const double *cg = &chv->val[chv->off[j]];
                for (int t = 0; t < 6; ++t) dd[c][t] = 2.0 * cg[t] + cn;
              }
            }
            act[nact++] = c;
          }
        }
        auto hit_value = [&](int bit) -> double {
          const uint8_t *tr = trans2_[bit];
          double h0 = 0, h1 = 0;
          int q = 0;
          for (; q + 2 <= nact; q += 2) {
            int c0 = act[q], c1 = act[q + 1];
            h0 += pc[c0] * dense[c0][tr[c0]];
            h1 += pc[c1] * dense[c1][tr[c1]];
          }
          if (q < nact) h0 += pc[act[q]] * dense[act[q]][tr[act[q]]];
          return h0 + h1;
        };
        double *hv = v + lv.hoff[i * NH_ + hc];
        int idx = 0;
        while (hm) {
          int bit = __builtin_ctz(hm);
          hm &= hm - 1;
          int total = bit < 22 ? bit : bit - 10;
          double stand = g[stand_class(total)] + nextv;
          double best = stand, hit = NEG_INF;
          if (!pi.locked && ((HITTABLE >> bit) & 1)) {
            hit = hit_value(bit);
            if (hit > best) best = hit;
          }
          hv[idx++] = best;
          if (k == 0 && !cx.root.lone && cx.root.second < 0) {
            cx.root_value = best;
            cx.root_stand = stand;
            cx.root_hit = hit;
          }
        }
        while (fm) {
          int c0 = __builtin_ctz(fm);
          fm &= fm - 1;
          int bit = pi.fbit[c0];
          int total = bit < 22 ? bit : bit - 10;
          double stand = g[stand_class(total)] + nextv;
          double best = stand, hit = NEG_INF, dbl = NEG_INF;
          if (!pi.locked) {
            hit = hit_value(bit);
            if (hit > best) best = hit;
            if (pi.fdbl[c0]) {
              dbl = 0;
              for (int q = 0; q < nact; ++q) {
                int c = act[q];
                dbl += pc[c] * dd[c][dcls_[bit][c]];
              }
              if (dbl > best) best = dbl;
            }
          }
          hv[idx++] = best;
          if (k == 0 && !cx.root.lone && cx.root.second >= 0) {
            cx.root_value = best;
            cx.root_stand = stand;
            cx.root_hit = hit;
            cx.root_dbl = dbl;
          }
        }
      }
     }
    }
  }
};

}  // namespace bj
