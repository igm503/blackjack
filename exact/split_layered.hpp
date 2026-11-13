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
#include <limits>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>

#include "bj.hpp"

namespace bj {

struct SplitOptions {
  // Report the largest observed value, to the hands still to be played, of
  // seeing one extra card (a diagnostic; costs a little time).
  bool measure_info = false;
};

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
    // Only with Options::measure_info: largest observed value, to the hands
    // still to be played, of seeing one extra card.
    double max_info_value = 0;
  };

  using Options = SplitOptions;

  LayeredSplitSolver(const Rules &rules, const DealerTable &dealer, int threads = 1, Options opt = Options{})
      : rules_(rules), dealer_(dealer), threads_(threads), opt_(opt) {
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
    // Scratch for the forward pass only (see prepare_node); freed afterwards.
    std::vector<uint32_t> thit;  // NH per node: hand states that may take a card
    std::vector<uint32_t> tdbl, tfh, twre;  // per node: bit sets, see prepare_node
    size_t size() const { return key.size(); }
    void drop_scratch() {
      std::vector<uint32_t>().swap(thit);
      std::vector<uint32_t>().swap(tdbl);
      std::vector<uint32_t>().swap(tfh);
      std::vector<uint32_t>().swap(twre);
    }
  };

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
      const size_t CH = 8192, ncur = cur.size();
      cur.thit.resize(ncur * NH_);
      cur.tdbl.resize(ncur);
      cur.tfh.resize(ncur);
      cur.twre.resize(ncur);
      parallel_chunks((ncur + CH - 1) / CH, ncur >= 4096 ? threads_ : 1, [&](size_t ci) {
        size_t a = ci * CH, b = std::min(ncur, a + CH);
        uint64_t t = 0;
        for (size_t i = a; i < b; ++i) {
          close_node(cur, i);
          prepare_node(pc.pair, cur, i);
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
      cur.drop_scratch();
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
    Stats total;
    std::mutex total_mutex;
    const int nl = int(P.layers.size());
    for (int k = nl - 1; k >= 0; --k) {
      const Layer &cur = P.layers[k];
      Vals &cv = bufs[k & 1];
      const Layer *child = k + 1 < nl ? &P.layers[k + 1] : nullptr;
      const Vals *chv = child ? &bufs[(k + 1) & 1] : nullptr;
      size_t n = cur.size();
      int T = n >= 1024 ? threads : 1;
      // value offsets: per-chunk counts, then a short serial prefix
      const size_t CH = 2048, nch = (n + CH - 1) / CH;
      std::vector<uint64_t> chunk_base(nch + 1, 0);
      parallel_chunks(nch, T, [&](size_t ci) {
        size_t a = ci * CH, b = std::min(n, a + CH);
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
        chunk_base[ci + 1] = t;
      });
      for (size_t ci = 1; ci <= nch; ++ci) chunk_base[ci] += chunk_base[ci - 1];
      parallel_chunks(nch, T, [&](size_t ci) {
        size_t a = ci * CH, b = std::min(n, a + CH);
        uint64_t base = chunk_base[ci];
        if (base)
          for (size_t i = a; i < b; ++i) cv.off[i] += base;
        Stats st;
        compute_range(cx, cur, cv, child, chv, k, a, b, st);
        std::lock_guard<std::mutex> lock(total_mutex);
        total.merge(st);
      });
    }
    res.backward_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    res.dealer_evals = total.devals;
    res.dealer_shortcuts = total.dshort;
    res.max_info_value = total.max_info;
    res.value = cx.root_value;
    res.stand = cx.root_stand;
    res.hit = cx.root_hit;
    res.dbl = cx.root_dbl;
    return res;
  }

  // Backward pass for several upcards at once (fresh-split roots only).
  // Everything that does not depend on the upcard - decoding the node,
  // locating its children, which states exist and where their values live -
  // is done once per node; the values of all upcards sit side by side, so
  // the arithmetic runs over them as short vectors.
  static constexpr int BMAX = 10;
  size_t batch_bytes(const Plan &P, int nb) const {
    size_t stride = size_t(nb + 1) & ~size_t(1);
    return 2 * (8 * stride * size_t(P.max_vals) + (8 + 2 * size_t(NH_)) * (P.max_nodes + 1));
  }
  // Value buffers of a batched evaluation. Passing the same Workspace to
  // successive calls reuses the memory instead of mapping (and page-faulting)
  // gigabytes afresh each time.
  struct Workspace {
    Vals bufs[2];
  };
  std::vector<Result> evaluate_batch(const Plan &P, const std::vector<Counts> &unseen,
                                     const std::vector<int> &upcards, int threads = -1,
                                     Workspace *ws = nullptr) const {
    auto t0 = std::chrono::steady_clock::now();
    if (threads < 0) threads = threads_;
    const int B = int(upcards.size());
    if (B < 1 || B > BMAX || !P.root.lone) throw std::invalid_argument("bad batch");
    BatchCtx bx;
    bx.B = B;
    bx.BB = (B + 1) & ~1;
    set_pair(bx.pair, P.pair);
    bx.supply = P.supply;
    for (int b = 0; b < B; ++b) {
      Counts want = P.supply;
      want[upcards[b]]--;
      if (want != unseen[b]) bx.exact_supply = false;
      bx.S[b] = unseen[b];
      bx.N[b] = total_cards(unseen[b]);
      bx.u[b] = upcards[b];
      bx.forb[b] = forbidden_hole(upcards[b]);
      if (bx.N[b] != bx.N[0]) throw std::invalid_argument("batch shoes differ in size");
      for (int r = 0; r < NR; ++r)
        if (unseen[b][r] > P.supply[r]) throw std::invalid_argument("plan does not cover this shoe");
    }
    Workspace local;
    Vals *bufs = (ws ? ws : &local)->bufs;
    for (int j = 0; j < 2; ++j) {
      bufs[j].off.ensure(P.max_nodes + 1);
      bufs[j].hoff.ensure(P.max_nodes * NH_);
      bufs[j].val.ensure(P.max_vals * size_t(bx.BB));
    }
    BatchStats total;
    std::mutex total_mutex;
    const int nl = int(P.layers.size());
    for (int k = nl - 1; k >= 0; --k) {
      const Layer &cur = P.layers[k];
      Vals &cv = bufs[k & 1];
      const Layer *child = k + 1 < nl ? &P.layers[k + 1] : nullptr;
      const Vals *chv = child ? &bufs[(k + 1) & 1] : nullptr;
      size_t n = cur.size();
      int T = n >= 512 ? threads : 1;
      const size_t CH = 1024, nch = (n + CH - 1) / CH;
      std::vector<uint64_t> chunk_base(nch + 1, 0);
      parallel_chunks(nch, T, [&](size_t ci) {
        size_t a = ci * CH, b = std::min(n, a + CH);
        uint64_t t = 0;
        for (size_t i = a; i < b; ++i) {
          uint32_t w = cur.w[i];
          int rel = ((w & GNEED) ? 6 : 0) + __builtin_popcount(w & WMASK);
          for (int hc = 0; hc < NH_; ++hc) {
            cv.hoff[i * NH_ + hc] = uint16_t(rel);
            rel += __builtin_popcount(cur.h[i * NH_ + hc]) + __builtin_popcount(cur.f[i * NH_ + hc]);
          }
          cv.off[i] = t;
          t += uint64_t(rel);
        }
        chunk_base[ci + 1] = t;
      });
      for (size_t ci = 1; ci <= nch; ++ci) chunk_base[ci] += chunk_base[ci - 1];
      parallel_chunks(nch, T, [&](size_t ci) {
        size_t a = ci * CH, b = std::min(n, a + CH);
        uint64_t base = chunk_base[ci];
        if (base)
          for (size_t i = a; i < b; ++i) cv.off[i] += base;
        BatchStats st;
        compute_range_batch(bx, cur, cv, child, chv, k, a, b, st);
        std::lock_guard<std::mutex> lock(total_mutex);
        total.merge(st);
      });
    }
    double sec = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    std::vector<Result> out;
    out.resize(size_t(B));
    for (int b = 0; b < B; ++b) {
      Result &r = out[size_t(b)];
      r.nodes = P.nodes;
      r.values = P.values;
      r.layers = nl;
      r.forward_seconds = P.seconds;
      r.backward_seconds = sec / B;
      r.dealer_evals = total.devals / size_t(B);
      r.dealer_shortcuts = total.dshort / size_t(B);
      r.value = bx.root_value[b];
      r.max_info_value = total.max_info[b];
    }
    return out;
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
  // Packed card counts, tens in the lowest bits and aces in the highest: in
  // a sorted layer, neighbours then share their low-rank counts, which is
  // what the dealer evaluation reuses (see DealerUnion).
  static constexpr int SH_[NR] = {50, 43, 37, 31, 25, 20, 15, 10, 5, 0};
  static constexpr int FW_[NR] = {8, 7, 6, 6, 6, 5, 5, 5, 5, 5};
  static constexpr int MAXNH = 28;

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
  Options opt_;
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

  // Everything a node pushes to its children that does not depend on which
  // card is drawn, worked out once per node instead of once per card:
  //   thit[hc]  hand states of group hc that may take another card
  //   tdbl      groups holding a two-card hand that may double
  //   tfh       groups that receive a new two-card hand (from "needs a card" states)
  //   twre      "needs a card" states created if the card is another pair card (resplit)
  void prepare_node(const PairInfo &pi, Layer &l, size_t i) const {
    uint32_t fh = 0, wre = 0, dblm = 0;
    for (uint32_t wi = l.w[i] & WMASK; wi; wi &= wi - 1) {
      int ms = __builtin_ctz(wi);
      fh |= 1u << hcanon_[ms];
      if (cs_[ms] > 0 && pi.resplit_ok) wre |= 1u << ms_[cm_[ms] + 1][cs_[ms] - 1];
    }
    for (int hc = 0; hc < NH_; ++hc) {
      uint32_t all = l.h[i * NH_ + hc];
      bool dbl = false;
      for (uint32_t fm = l.f[i * NH_ + hc]; fm; fm &= fm - 1) {
        int c0 = __builtin_ctz(fm);
        all |= 1u << pi.fbit[c0];
        dbl |= pi.fdbl[c0];
      }
      l.thit[i * NH_ + hc] = pi.locked ? 0u : (all & HITTABLE);
      if (dbl) dblm |= 1u << hc;
    }
    l.tfh[i] = fh;
    l.twre[i] = wre;
    l.tdbl[i] = dblm;
  }

  // Marks that node i of layer k pushes onto its child reached by drawing c.
  bool contrib(PlanCtx &px, const Layer &l, size_t i, int c, int k, uint32_t &w, uint32_t *h, uint16_t *f) const {
    uint64_t key = l.key[i];
    int drawn = field(key, c);
    if (px.S[c] - drawn <= 0) return false;
    if (px.forb >= 0) {
      int fc = px.S[px.forb] - field(key, px.forb) - (c == px.forb);
      if (px.N - k - 1 - fc <= 0) return false;
    }
    if (drawn == (1 << FW_[c]) - 1) px.overflow.store(true);
    bool any = false;
    if (uint32_t fh = l.tfh[i]) {
      any = true;
      for (; fh; fh &= fh - 1) f[__builtin_ctz(fh)] |= uint16_t(1u << c);
      if (c == px.pair.p) w |= l.twre[i];
    }
    const uint32_t dblm = l.tdbl[i];
    const uint32_t *hit = &l.thit[i * NH_];
    for (int hc = 0; hc < NH_; ++hc) {
      if (!hit[hc]) continue;  // (a hand that may double may also hit)
      bool bust = false;
      uint32_t nh = shift(hit[hc], c, bust);
      bool dbl = (dblm >> hc) & 1;
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
    // Split the key space into ranges of roughly equal work (by quantiles of
    // this layer's keys); more ranges than threads so they balance out.
    size_t R = T == 1 ? 1 : g_static_schedule ? size_t(T) : std::min<size_t>(n / 1024, size_t(T) * 8);
    std::vector<uint64_t> bound(R + 1);
    bound[0] = 0;
    bound[R] = ~0ull;
    for (size_t r = 1; r < R; ++r) bound[r] = l.key[n * r / R];
    std::vector<Out> outs(R);
    parallel_chunks(R, T, [&](size_t r) { merge_range(px, l, k, bound[r], bound[r + 1], outs[r]); });
    if (R == 1) {
      next.key.swap(outs[0].key);
      next.w.swap(outs[0].w);
      next.h.swap(outs[0].h);
      next.f.swap(outs[0].f);
      return;
    }
    std::vector<size_t> at(R + 1, 0);
    for (size_t r = 0; r < R; ++r) at[r + 1] = at[r] + outs[r].key.size();
    size_t tot = at[R];
    next.key.resize(tot);
    next.w.resize(tot);
    next.h.resize(tot * NH_);
    next.f.resize(tot * NH_);
    parallel_chunks(R, T, [&](size_t r) {
      Out &o = outs[r];
      std::copy(o.key.begin(), o.key.end(), next.key.begin() + at[r]);
      std::copy(o.w.begin(), o.w.end(), next.w.begin() + at[r]);
      std::copy(o.h.begin(), o.h.end(), next.h.begin() + at[r] * NH_);
      std::copy(o.f.begin(), o.f.end(), next.f.begin() + at[r] * NH_);
      o = Out{};
    });
  }

  struct BatchCtx {
    int B = 0, BB = 0;  // upcards in the batch; stride (B rounded up to even)
    Counts S[BMAX];
    int N[BMAX], u[BMAX], forb[BMAX];
    PairInfo pair;
    Counts supply;  // cards unseen before the upcard is removed
    bool exact_supply = true;  // every batch shoe is supply minus its upcard
    double root_value[BMAX] = {0};
  };
  struct BatchStats {
    size_t devals = 0, dshort = 0;
    double max_info[BMAX];
    BatchStats() {
      for (int b = 0; b < BMAX; ++b) max_info[b] = 0;
    }
    void merge(const BatchStats &o) {
      devals += o.devals;
      dshort += o.dshort;
      for (int b = 0; b < BMAX; ++b) max_info[b] = std::max(max_info[b], o.max_info[b]);
    }
  };

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

  struct Tmp {  // per-node scratch of the backward pass
    Counts rem;
    double pc[NR];
    ptrdiff_t child[NR];
    bool feasible;
  };
  struct Stats {
    size_t devals = 0, dshort = 0;
    double max_info = 0;
    void merge(const Stats &o) {
      devals += o.devals;
      dshort += o.dshort;
      max_info = std::max(max_info, o.max_info);
    }
  };

  void compute_range(EvalCtx &cx, const Layer &l, Vals &lv, const Layer *ch, const Vals *chv, int k,
                     size_t a, size_t b, Stats &stats) const {
    size_t &devals = stats.devals, &dshort = stats.dshort;
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
        if (opt_.measure_info) {
          // E[value of these hands after one more card is seen] - value now
          bool all = true;
          double after = 0;
          for (int c = 0; c < NR && all; ++c) {
            if (pc[c] <= 0) continue;
            if (child[c] < 0 || !((ch->w[child[c]] >> ms) & 1)) all = false;
            else after += pc[c] * wval(*ch, *chv, child[c], ms);
          }
          if (all) stats.max_info = std::max(stats.max_info, after - acc);
        }
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
          hitmask = hm;
          for (uint32_t x = fm; x; x &= x - 1) {
            int c0 = __builtin_ctz(x);
            hitmask |= 1u << pi.fbit[c0];
            anydbl |= pi.fdbl[c0];
          }
          hitmask &= HITTABLE;
        }
        // Unpack what this group needs from each child into small dense
        // tables indexed by the child's state, so the loops over states are
        // plain table lookups: dense[c][state] is the value after drawing c
        // into that state (slot 32: the hand busts), dd[c][class] the doubled
        // payoff (slot 6: bust). No popcounts and no bust-or-not branches,
        // both of which cost more than the arithmetic.
        int act[NR], nact = 0;
        double dense[NR][33], dd[NR][7];
        if (hitmask || anydbl) {
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

  // Two-lane vector view of the batched values (lanes are upcards). BB is
  // always even, and nothing here needs more than 8-byte alignment.
  typedef double V2 __attribute__((vector_size(16), aligned(8), may_alias));
  static void vzero(double *o, size_t bb) {
    for (size_t b = 0; b < bb; ++b) o[b] = 0.0;
  }
  static void vaxpy(double *o, const double *p, const double *x, size_t bb) {  // o += p * x
    for (size_t b = 0; b < bb; b += 2) *(V2 *)(o + b) += *(const V2 *)(p + b) * *(const V2 *)(x + b);
  }
  static void vaxpy_max(double *o, const double *p, const double *x, const double *y, size_t bb) {  // o += p * max(x, y)
    for (size_t b = 0; b < bb; b += 2) {
      V2 xv = *(const V2 *)(x + b), yv = *(const V2 *)(y + b);
      V2 m = xv > yv ? xv : yv;
      *(V2 *)(o + b) += *(const V2 *)(p + b) * m;
    }
  }
  static void vmax_into(double *o, const double *x, size_t bb) {  // o = max(o, x)
    for (size_t b = 0; b < bb; b += 2) {
      V2 ov = *(V2 *)(o + b), xv = *(const V2 *)(x + b);
      *(V2 *)(o + b) = ov > xv ? ov : xv;
    }
  }
  static void vadd(double *o, const double *x, const double *y, size_t bb) {  // o = x + y
    for (size_t b = 0; b < bb; b += 2) *(V2 *)(o + b) = *(const V2 *)(x + b) + *(const V2 *)(y + b);
  }

  struct PendingU {
    size_t i;   // node
    double *v;  // its value block
  };
  // Dealer values of every upcard in the batch for `cnt` (<= W) nodes that
  // share the stable part of the dealer trie.
  template <int W>
  void union_lanes(const DealerUnion &U, const double *sval, const double *gstable, const Counts *base, size_t cnt,
                   int n, const BatchCtx &bx, const PendingU *p, size_t BB) const {
    const Counts *bp[W];
    for (size_t q = 0; q < size_t(W); ++q) bp[q] = &base[q < cnt ? q : 0];
    double out[W][NR][6];
    U.varying<W>(sval, gstable, bp, n, bx.u, bx.B, out);
    for (size_t q = 0; q < cnt; ++q)
      for (int b = 0; b < bx.B; ++b) {
        if (base[q][bx.u[b]] < 1) continue;  // this upcard cannot occur here
        for (size_t t = 0; t < 6; ++t) p[q].v[t * BB + size_t(b)] = out[q][b][t];
      }
  }
  struct PendingB {
    double *v;  // node's value block (6 * BB dealer slots first)
    const Counts *rem;
  };
  // Dealer values for `cnt` (<= W) pending nodes of one upcard, W shoes per pass.
  template <int W>
  void dealer_lanes(const PendingB *p, size_t cnt, int n, int upcard, size_t lane, size_t BB) const {
    const Counts *cp[W];
    int nn[W];
    double out[W][6], g6[6];
    for (size_t j = 0; j < size_t(W); ++j) {
      cp[j] = p[j < cnt ? j : 0].rem;
      nn[j] = n;
    }
    dealer_probs_batch<W>(dealer_, cp, nn, upcard, out);
    for (size_t j = 0; j < cnt; ++j) {
      stand_values(out[j], g6);
      for (size_t q = 0; q < 6; ++q) p[j].v[q * BB + lane] = g6[q];
    }
  }

  // The batched counterpart of compute_range. Value slot `q` of node i for
  // batch lane b lives at val[(off[i] + q) * BB + b].
  void compute_range_batch(BatchCtx &bx, const Layer &l, Vals &lv, const Layer *ch, const Vals *chv, int k,
                           size_t a, size_t b_end, BatchStats &stats) const {
    const PairInfo &pi = bx.pair;
    const int B = bx.B;
    const size_t BB = size_t(bx.BB);
    size_t n1 = ch ? ch->size() : 0;
    size_t cur[NR];
    for (int c = 0; c < NR; ++c)
      cur[c] = ch ? size_t(std::lower_bound(ch->key.begin(), ch->key.end(), l.key[a] + unit(c)) -
                           ch->key.begin())
                  : 0;
    const int n = bx.N[0] - k;
    constexpr int BLOCK = 32;
    struct TmpB {
      Counts rem[BMAX];
      double pc[NR][BMAX];  // pc[c][lane]
      ptrdiff_t child[NR];
      bool any[NR];         // some lane can draw c
      bool feasible[BMAX];
    };
    TmpB tmp[BLOCK];
    std::vector<PendingB> pend[BMAX];
    for (int b = 0; b < B; ++b) pend[b].reserve(BLOCK);
    const DealerUnion &U = *dealer_.uni;
    const bool use_union = g_dealer_union && bx.exact_supply;
    std::vector<PendingU> upend;
    upend.reserve(BLOCK);
    std::unique_ptr<double[]> sval(new double[DealerUnion::MAX_UNION_NODES]), gstable(new double[U.groups.size() + 1]);
    uint64_t cached_sig = 0;
    bool have_sig = false;
    static const double kZeros[BMAX + 1] = {0};

    for (size_t i0 = a; i0 < b_end; i0 += BLOCK) {
      const size_t i1 = std::min(b_end, i0 + BLOCK);
      // ---- pass 1: children and dealer values ----
      for (size_t i = i0; i < i1; ++i) {
        TmpB &t = tmp[i - i0];
        uint64_t key = l.key[i];
        int drawn[NR];
        for (int r = 0; r < NR; ++r) drawn[r] = field(key, r);
        for (int c = 0; c < NR; ++c) {
          t.any[c] = false;
          for (size_t b = 0; b < BB; ++b) t.pc[c][b] = 0.0;
        }
        for (int b = 0; b < B; ++b) {
          bool ok = true;
          for (int r = 0; r < NR; ++r) {
            t.rem[b][r] = bx.S[b][r] - drawn[r];
            ok &= t.rem[b][r] >= 0;
          }
          t.feasible[b] = ok;
          if (!ok) continue;
          double p1[NR];
          next_card_probs(t.rem[b], n, bx.forb[b], p1);
          for (int c = 0; c < NR; ++c) {
            t.pc[c][b] = p1[c];
            t.any[c] |= p1[c] > 0;
          }
        }
        for (int c = 0; c < NR; ++c) {
          t.child[c] = -1;
          if (!t.any[c] || !ch) continue;
          uint64_t target = key + unit(c);
          size_t x = cur[c];
          while (x < n1 && ch->key[x] < target) ++x;
          cur[c] = x;
          if (x < n1 && ch->key[x] == target) t.child[c] = ptrdiff_t(x);
        }
        double *v = &lv.val[lv.off[i] * BB];
        if (l.w[i] & GNEED) {
          vzero(v, 6 * BB);
          // Children that can supply dealer values for the martingale average.
          bool have[NR], all_have = true;
          for (int c = 0; c < NR; ++c) {
            have[c] = t.child[c] >= 0 && (ch->w[t.child[c]] & GNEED);
            if (t.any[c] && !have[c]) all_have = false;
          }
          if (all_have && ch) {
            for (int c = 0; c < NR; ++c) {
              if (!t.any[c]) continue;
              const double *gc = &chv->val[chv->off[t.child[c]] * BB];
              const double *p = t.pc[c];
              for (size_t q = 0; q < 6; ++q) vaxpy(v + q * BB, p, gc + q * BB, BB);
            }
            stats.dshort += size_t(B);
          } else if (use_union) {
            upend.push_back({i, v});
          } else {
            for (int b = 0; b < B; ++b) {
              if (!t.feasible[b]) continue;
              bool okb = ch != nullptr;
              for (int c = 0; c < NR && okb; ++c)
                if (t.pc[c][b] > 0 && !have[c]) okb = false;
              if (okb) {
                for (int c = 0; c < NR; ++c) {
                  double p = t.pc[c][b];
                  if (p <= 0) continue;
                  const double *gc = &chv->val[chv->off[t.child[c]] * BB];
                  for (int q = 0; q < 6; ++q) v[q * BB + size_t(b)] += p * gc[q * BB + size_t(b)];
                }
                ++stats.dshort;
              } else {
                pend[b].push_back({v, &t.rem[b]});
              }
            }
          }
        }
      }
      // Direct dealer evaluations. All upcards share one trie walk, and runs
      // of nodes with the same low-rank counts share its stable part.
      for (size_t j0 = 0; j0 < upend.size();) {
        uint64_t sig = l.key[upend[j0].i] >> SH_[U.cut];
        size_t j1 = j0 + 1;
        while (j1 < upend.size() && j1 - j0 < 8 && (l.key[upend[j1].i] >> SH_[U.cut]) == sig) ++j1;
        size_t cnt = j1 - j0;
        Counts base[8];
        for (size_t q = 0; q < cnt; ++q) {
          uint64_t key = l.key[upend[j0 + q].i];
          for (int r = 0; r < NR; ++r) base[q][r] = bx.supply[r] - field(key, r);
        }
        if (!have_sig || sig != cached_sig) {
          U.stable(base[0], sval.get(), gstable.get());
          cached_sig = sig;
          have_sig = true;
        }
        if (cnt > 4) union_lanes<8>(U, sval.get(), gstable.get(), base, cnt, n + 0, bx, upend.data() + j0, BB);
        else if (cnt > 2) union_lanes<4>(U, sval.get(), gstable.get(), base, cnt, n, bx, upend.data() + j0, BB);
        else union_lanes<2>(U, sval.get(), gstable.get(), base, cnt, n, bx, upend.data() + j0, BB);
        stats.devals += cnt * size_t(B);
        j0 = j1;
      }
      upend.clear();
      // (benchmark variant) direct dealer evaluations per upcard
      for (int b = 0; b < B; ++b) {
        auto &pl = pend[b];
        size_t j0 = 0;
        for (; j0 + 8 <= pl.size(); j0 += 8) dealer_lanes<8>(pl.data() + j0, 8, n, bx.u[b], size_t(b), BB);
        for (; j0 < pl.size(); j0 += 4) dealer_lanes<4>(pl.data() + j0, std::min<size_t>(4, pl.size() - j0), n, bx.u[b], size_t(b), BB);
        stats.devals += pl.size();
        pl.clear();
      }
      // ---- pass 2: state values ----
      for (size_t i = i0; i < i1; ++i) {
        const TmpB &t = tmp[i - i0];
        const ptrdiff_t *child = t.child;
        double *v = &lv.val[lv.off[i] * BB];
        uint32_t wi = l.w[i];
        const double *g = (wi & GNEED) ? v : nullptr;
        int pos = (wi & GNEED) ? 6 : 0;
        auto wptr = [&](const Layer &ly, const Vals &vl, size_t j, int ms) -> const double * {
          uint32_t w = ly.w[j];
          if (!((w >> ms) & 1)) missing("W");
          return &vl.val[(vl.off[j] + ((w & GNEED) ? 6 : 0) + __builtin_popcount(w & ((1u << ms) - 1))) * BB];
        };
        // "needs a card" states
        uint32_t wb = wi & WMASK;
        int widx = 0;
        while (wb) {
          int ms = __builtin_ctz(wb);
          wb &= wb - 1;
          int m = cm_[ms], s = cs_[ms], hc = hcanon_[ms];
          bool rs = s > 0 && pi.resplit_ok;
          double *acc = v + size_t(pos + widx) * BB;
          vzero(acc, BB);
          for (int c = 0; c < NR; ++c) {
            if (!t.any[c]) continue;
            size_t j = size_t(child[c]);
            if (child[c] < 0) missing("node");
            uint32_t f = ch->f[j * NH_ + hc];
            if (!((f >> c) & 1)) missing("F");
            const double *x = &chv->val[(chv->off[j] + chv->hoff[j * NH_ + hc] +
                                         __builtin_popcount(ch->h[j * NH_ + hc]) +
                                         __builtin_popcount(f & ((1u << c) - 1))) * BB];
            const double *p = t.pc[c];
            if (c == pi.p && rs) {
              const double *y = wptr(*ch, *chv, j, ms_[m + 1][s - 1]);
              vaxpy_max(acc, p, x, y, BB);
            } else {
              vaxpy(acc, p, x, BB);
            }
          }
          if (k == 0)
            for (int b = 0; b < B; ++b) bx.root_value[b] = acc[b];
          if (opt_.measure_info) {
            for (int b = 0; b < B; ++b) {
              bool all = t.feasible[b];
              double after = 0;
              for (int c = 0; c < NR && all; ++c) {
                if (t.pc[c][b] <= 0) continue;
                if (child[c] < 0 || !((ch->w[child[c]] >> ms) & 1)) all = false;
                else after += t.pc[c][b] * wptr(*ch, *chv, size_t(child[c]), ms)[b];
              }
              if (all) stats.max_info[b] = std::max(stats.max_info[b], after - acc[b]);
            }
          }
          ++widx;
        }
        // hand states
        for (int hc = 0; hc < NH_; ++hc) {
          uint32_t hm = l.h[i * NH_ + hc];
          uint32_t fm = l.f[i * NH_ + hc];
          if (!(hm | fm)) continue;
          int m = hm_[hc], s = hs_[hc];
          int nms = m > 0 ? ms_[m - 1][s] : -1;
          const double *nextv = m > 0 ? wptr(l, lv, i, nms) : kZeros;
          uint32_t hitmask = 0;
          bool anydbl = false;
          if (!pi.locked) {
            hitmask = hm;
            for (uint32_t x = fm; x; x &= x - 1) {
              int c0 = __builtin_ctz(x);
              hitmask |= 1u << pi.fbit[c0];
              anydbl |= pi.fdbl[c0];
            }
            hitmask &= HITTABLE;
          }
          // Per child: where each of its states' values sit (dptr), the value
          // if the hand busts on that card (cbust), the hands still to come
          // (cnp) and its dealer values (cgp).
          int act[NR], nact = 0;
          const double *dptr[NR][33], *cnp[NR], *cgp[NR];
          double cbust[NR][BMAX];
          if (hitmask || anydbl) {
            for (int c = 0; c < NR; ++c) {
              if (!t.any[c]) continue;
              bool bust;
              uint32_t need = shift(hitmask, c, bust);
              ptrdiff_t j = child[c];
              cnp[c] = kZeros;
              cgp[c] = nullptr;
              if (m > 0 && (bust || anydbl)) {
                if (j < 0) missing("node");
                cnp[c] = wptr(*ch, *chv, size_t(j), nms);
              }
              if (need) {
                if (j < 0) missing("node");
                uint32_t cm = ch->h[size_t(j) * NH_ + hc];
                if (need & ~cm) missing("H");
                const double *src = &chv->val[(chv->off[j] + chv->hoff[size_t(j) * NH_ + hc]) * BB];
                for (; cm; cm &= cm - 1, src += BB) dptr[c][__builtin_ctz(cm)] = src;
                if (anydbl) {
                  if (!(ch->w[j] & GNEED)) missing("G");
                  cgp[c] = &chv->val[chv->off[j] * BB];
                }
              }
              for (size_t b = 0; b < BB; ++b) cbust[c][b] = cnp[c][b] - 1.0;
              dptr[c][32] = cbust[c];
              act[nact++] = c;
            }
          }
          auto hit_value = [&](int bit, double *out) {
            const uint8_t *tr = trans2_[bit];
            vzero(out, BB);
            for (int q = 0; q < nact; ++q) {
              int c = act[q];
              vaxpy(out, t.pc[c], dptr[c][tr[c]], BB);
            }
          };
          double *hv = v + size_t(lv.hoff[i * NH_ + hc]) * BB;
          double hit[BMAX];
          while (hm) {
            int bit = __builtin_ctz(hm);
            hm &= hm - 1;
            int total = bit < 22 ? bit : bit - 10;
            const double *gs = g + size_t(stand_class(total)) * BB;
            vadd(hv, gs, nextv, BB);
            if (!pi.locked && ((HITTABLE >> bit) & 1)) {
              hit_value(bit, hit);
              vmax_into(hv, hit, BB);
            }
            hv += BB;
          }
          while (fm) {
            int c0 = __builtin_ctz(fm);
            fm &= fm - 1;
            int bit = pi.fbit[c0];
            int total = bit < 22 ? bit : bit - 10;
            const double *gs = g + size_t(stand_class(total)) * BB;
            vadd(hv, gs, nextv, BB);
            if (!pi.locked) {
              hit_value(bit, hit);
              vmax_into(hv, hit, BB);
              if (pi.fdbl[c0]) {
                double dbl[BMAX], term[BMAX];
                vzero(dbl, BB);
                for (int q = 0; q < nact; ++q) {
                  int c = act[q];
                  const double *cn = cnp[c];
                  int cls = dcls_[bit][c];
                  if (cls == 6) {
                    for (size_t b = 0; b < BB; ++b) term[b] = cn[b] - 2.0;
                  } else {
                    if (!cgp[c]) missing("G");
                    const double *cg = cgp[c] + size_t(cls) * BB;
                    for (size_t b = 0; b < BB; ++b) term[b] = 2.0 * cg[b] + cn[b];
                  }
                  vaxpy(dbl, t.pc[c], term, BB);
                }
                vmax_into(hv, dbl, BB);
              }
            }
            hv += BB;
          }
        }
      }
    }
  }
};

}  // namespace bj
