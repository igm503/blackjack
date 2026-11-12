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

  Result solve(const Counts &unseen, int pair_rank, int upcard, const Root *root_in = nullptr) {
    Root root = root_in ? *root_in : Root{};
    if (root.splits_left < 0) root.splits_left = L_ - 1;
    if (L_ < 1 || root.pending < 0 || root.pending + root.splits_left > L_)
      throw std::invalid_argument("bad split root");
    S_ = unseen;
    N_ = total_cards(unseen);
    p_ = pair_rank;
    u_ = upcard;
    forb_ = forbidden_hole(upcard);
    locked_ = p_ == ACE && !rules_.hit_split_aces;
    resplit_ok_ = p_ != ACE || rules_.resplit_aces;
    for (int c = 0; c < NR; ++c) {
      int pips = p_ + 1 + c + 1;
      bool soft = p_ == ACE || c == ACE;
      int total = soft ? pips + 10 : pips;
      fbit_[c] = soft ? pips + 20 : pips;
      bool d = rules_.das && total != 21 && !locked_;
      if (rules_.double_on == DoubleOn::NineToEleven && (total < 9 || total > 11)) d = false;
      if (rules_.double_on == DoubleOn::TenToEleven && (total < 10 || total > 11)) d = false;
      fdbl_[c] = d;
    }
    overflow_.store(false);

    std::vector<Layer> layers(1);
    {
      Layer &l0 = layers[0];
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
    root_ = root;

    // ---- forward: discover reachable states layer by layer ----
    Result res;
    auto t_start = std::chrono::steady_clock::now();
    for (int k = 0;; ++k) {
      Layer &cur = layers[k];
      parallel_for(cur.size(), cur.size() >= 4096 ? threads_ : 1, [&](int, size_t a, size_t b) {
        for (size_t i = a; i < b; ++i) close_node(cur, i);
      });
      Layer next;
      build_next(cur, k, next);
      if (overflow_.load()) throw std::overflow_error("split key overflow");
      res.nodes += cur.size();
      if (next.size() == 0) break;
      layers.push_back(std::move(next));
    }
    res.layers = int(layers.size());
    auto t_mid = std::chrono::steady_clock::now();
    res.forward_seconds = std::chrono::duration<double>(t_mid - t_start).count();

    // ---- backward: values ----
    std::atomic<size_t> devals{0}, dshort{0};
    // The backward pass only needs the values of two adjacent layers, so two
    // buffer sets, sized once for the largest layer, are handed back and forth.
    struct BufSet {
      Buf<uint64_t> off;
      Buf<uint16_t> hoff;
      Buf<double> val;
    };
    std::vector<BufSet> pool(2);
    {
      size_t max_nodes = 0;
      uint64_t max_vals = 0;
      for (auto &l : layers) {
        max_nodes = std::max(max_nodes, l.size());
        std::atomic<uint64_t> tot{0};
        parallel_for(l.size(), l.size() >= 4096 ? threads_ : 1, [&](int, size_t a, size_t b) {
          uint64_t t = 0;
          for (size_t i = a; i < b; ++i) {
            uint32_t w = l.w[i];
            t += ((w & GNEED) ? 6 : 0) + __builtin_popcount(w & WMASK);
            for (int hc = 0; hc < NH_; ++hc)
              t += __builtin_popcount(l.h[i * NH_ + hc]) + __builtin_popcount(l.f[i * NH_ + hc]);
          }
          tot += t;
        });
        max_vals = std::max<uint64_t>(max_vals, tot.load());
      }
      for (auto &b : pool) {
        b.off.ensure(max_nodes + 1);
        b.hoff.ensure(max_nodes * NH_);
        b.val.ensure(max_vals);
      }
    }
    for (int k = int(layers.size()) - 1; k >= 0; --k) {
      Layer &cur = layers[k];
      const Layer *child = k + 1 < int(layers.size()) ? &layers[k + 1] : nullptr;
      size_t n = cur.size();
      cur.off = std::move(pool.back().off);
      cur.hoff = std::move(pool.back().hoff);
      cur.val = std::move(pool.back().val);
      pool.pop_back();
      cur.off.ensure(n + 1);
      cur.hoff.ensure(n * NH_);
      std::vector<uint16_t> cnt(n);
      parallel_for(n, n >= 4096 ? threads_ : 1, [&](int, size_t a, size_t b) {
        for (size_t i = a; i < b; ++i) {
          uint32_t w = cur.w[i];
          int rel = ((w & GNEED) ? 6 : 0) + __builtin_popcount(w & WMASK);
          for (int hc = 0; hc < NH_; ++hc) {
            cur.hoff[i * NH_ + hc] = uint16_t(rel);
            rel += __builtin_popcount(cur.h[i * NH_ + hc]) + __builtin_popcount(cur.f[i * NH_ + hc]);
          }
          cnt[i] = uint16_t(rel);
        }
      });
      uint64_t tot = 0;
      for (size_t i = 0; i < n; ++i) {
        cur.off[i] = tot;
        tot += cnt[i];
      }
      cur.off[n] = tot;
      std::vector<uint16_t>().swap(cnt);
      cur.val.ensure(tot);
      res.values += tot;
      parallel_for(n, n >= 1024 ? threads_ : 1, [&](int, size_t a, size_t b) {
        size_t de = 0, ds = 0;
        compute_range(cur, child, k, a, b, de, ds);
        devals += de;
        dshort += ds;
      });
      if (child) {
        pool.emplace_back();
        pool.back().off = std::move(layers[k + 1].off);
        pool.back().hoff = std::move(layers[k + 1].hoff);
        pool.back().val = std::move(layers[k + 1].val);
        layers[k + 1] = Layer{};  // free the marks
      }
    }
    res.backward_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - t_mid).count();
    res.dealer_evals = devals.load();
    res.dealer_shortcuts = dshort.load();
    res.value = root_value_;
    res.stand = root_stand_;
    res.hit = root_hit_;
    res.dbl = root_dbl_;
    return res;
  }

 private:
  static constexpr uint32_t GNEED = 1u << 31;
  static constexpr uint32_t WMASK = ~GNEED;
  // hand-state bits: hard pips x -> bit x (0..21); soft pips x (2..11) -> bit 20 + x
  static constexpr uint32_t HITTABLE = 0x001FFFFFu | 0xFFC00000u;
  static constexpr int SH_[NR] = {0, 8, 15, 21, 27, 33, 38, 43, 48, 53};
  static constexpr int FW_[NR] = {8, 7, 6, 6, 6, 5, 5, 5, 5, 5};
  static constexpr int MAXNH = 28;

  // Uninitialised, reusable storage. The backward pass only ever needs the
  // values of two adjacent layers, so two sets of buffers are recycled.
  template <class T>
  struct Buf {
    std::unique_ptr<T[]> p;
    size_t cap = 0;
    Buf() = default;
    Buf(Buf &&o) noexcept : p(std::move(o.p)), cap(o.cap) { o.cap = 0; }
    Buf &operator=(Buf &&o) noexcept {
      p = std::move(o.p);
      cap = o.cap;
      o.cap = 0;
      return *this;
    }
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
  struct Layer {
    std::vector<uint64_t> key;
    std::vector<uint32_t> w;  // one bit per (m,s) "needs a card" state, plus GNEED
    std::vector<uint32_t> h;  // NH per node: 3+ card hand states
    std::vector<uint16_t> f;  // NH per node: two-card hand states, by second card
    Buf<uint64_t> off;
    Buf<uint16_t> hoff;
    Buf<double> val;
    size_t size() const { return key.size(); }
  };

  Rules rules_;
  const DealerTable &dealer_;
  int threads_;
  int L_, NC_, NH_;
  int ms_[8][8], cm_[32], cs_[32], hidx_[32], hcanon_[32], hm_[32], hs_[32];
  int8_t trans_[32][NR];

  Counts S_;
  int N_, p_, u_, forb_;
  bool locked_, resplit_ok_;
  int fbit_[NR];
  bool fdbl_[NR];
  Root root_;
  double root_value_ = 0, root_stand_ = NEG_INF, root_hit_ = NEG_INF, root_dbl_ = NEG_INF;
  std::atomic<bool> overflow_{false};

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
  bool contrib(const Layer &l, size_t i, int c, int k, uint32_t &w, uint32_t *h, uint16_t *f) {
    uint64_t key = l.key[i];
    int drawn = field(key, c);
    if (S_[c] - drawn <= 0) return false;
    if (forb_ >= 0) {
      int fc = S_[forb_] - field(key, forb_) - (c == forb_);
      if (N_ - k - 1 - fc <= 0) return false;
    }
    if (drawn == (1 << FW_[c]) - 1) overflow_.store(true);
    bool any = false;
    uint32_t wi = l.w[i] & WMASK;
    while (wi) {
      int ms = __builtin_ctz(wi);
      wi &= wi - 1;
      f[hcanon_[ms]] |= uint16_t(1u << c);
      any = true;
      if (c == p_ && cs_[ms] > 0 && resplit_ok_) w |= 1u << ms_[cm_[ms] + 1][cs_[ms] - 1];
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
        all |= 1u << fbit_[c0];
        dbl |= fdbl_[c0];
      }
      bool bust = false;
      uint32_t nh = locked_ ? 0u : shift(all & HITTABLE, c, bust);
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

  void merge_range(const Layer &l, int k, uint64_t lo, uint64_t hi, Out &out) {
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
          any |= contrib(l, idx[c], c, k, w, h, f);
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

  void build_next(const Layer &l, int k, Layer &next) {
    size_t n = l.size();
    int T = n >= 4096 ? threads_ : 1;
    std::vector<uint64_t> bound(T + 1);
    bound[0] = 0;
    bound[T] = ~0ull;
    for (int t = 1; t < T; ++t) bound[t] = l.key[n * t / T];
    std::vector<Out> outs(T);
    parallel_for(size_t(T), T, [&](int, size_t a, size_t b) {
      for (size_t t = a; t < b; ++t) merge_range(l, k, bound[t], bound[t + 1], outs[t]);
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

  double wval(const Layer &l, size_t j, int ms) const {
    uint32_t w = l.w[j];
    if (!((w >> ms) & 1)) missing("W");
    return l.val[l.off[j] + ((w & GNEED) ? 6 : 0) + __builtin_popcount(w & ((1u << ms) - 1))];
  }
  double hval(const Layer &l, size_t j, int hc, int b) const {
    uint32_t h = l.h[j * NH_ + hc];
    if (!((h >> b) & 1)) missing("H");
    return l.val[l.off[j] + l.hoff[j * NH_ + hc] + __builtin_popcount(h & ((1u << b) - 1))];
  }
  double fval(const Layer &l, size_t j, int hc, int c) const {
    uint32_t f = l.f[j * NH_ + hc];
    if (!((f >> c) & 1)) missing("F");
    return l.val[l.off[j] + l.hoff[j * NH_ + hc] + __builtin_popcount(l.h[j * NH_ + hc]) +
                 __builtin_popcount(f & ((1u << c) - 1))];
  }
  const double *gval(const Layer &l, size_t j) const {
    if (!(l.w[j] & GNEED)) missing("G");
    return &l.val[l.off[j]];
  }

  void compute_range(Layer &l, const Layer *ch, int k, size_t a, size_t b, size_t &devals,
                     size_t &dshort) {
    size_t n1 = ch ? ch->size() : 0;
    size_t cur[NR];
    for (int c = 0; c < NR; ++c)
      cur[c] = ch ? size_t(std::lower_bound(ch->key.begin(), ch->key.end(), l.key[a] + unit(c)) -
                           ch->key.begin())
                  : 0;
    const int n = N_ - k;
    for (size_t i = a; i < b; ++i) {
      uint64_t key = l.key[i];
      Counts rem;
      for (int r = 0; r < NR; ++r) rem[r] = S_[r] - field(key, r);
      double pc[NR];
      next_card_probs(rem, n, forb_, pc);
      ptrdiff_t child[NR];
      for (int c = 0; c < NR; ++c) {
        child[c] = -1;
        if (pc[c] <= 0 || !ch) continue;
        uint64_t target = key + unit(c);
        size_t x = cur[c];
        while (x < n1 && ch->key[x] < target) ++x;
        cur[c] = x;
        if (x < n1 && ch->key[x] == target) child[c] = ptrdiff_t(x);
      }
      double *v = &l.val[l.off[i]];
      uint32_t wi = l.w[i];
      const double *g = nullptr;
      int pos = 0;
      if (wi & GNEED) {
        // Dealer probabilities are a martingale in the cards drawn, so if
        // every child already has them, average instead of re-enumerating.
        bool all = true;
        for (int c = 0; c < NR && all; ++c)
          if (pc[c] > 0 && (child[c] < 0 || !(ch->w[child[c]] & GNEED))) all = false;
        if (all && ch) {
          for (int t = 0; t < 6; ++t) v[t] = 0;
          for (int c = 0; c < NR; ++c)
            if (pc[c] > 0) {
              const double *gc = &ch->val[ch->off[child[c]]];
              for (int t = 0; t < 6; ++t) v[t] += pc[c] * gc[t];
            }
          ++dshort;
        } else {
          stand_values_for(dealer_, rem, n, u_, v);
          ++devals;
        }
        g = v;
        pos = 6;
      }
      // "needs a card" states
      uint32_t wb = wi & WMASK;
      double *wv = v + pos;
      int widx = 0;
      while (wb) {
        int ms = __builtin_ctz(wb);
        wb &= wb - 1;
        int m = cm_[ms], s = cs_[ms], hc = hcanon_[ms];
        bool rs = s > 0 && resplit_ok_;
        double acc = 0;
        for (int c = 0; c < NR; ++c) {
          if (pc[c] <= 0) continue;
          if (child[c] < 0) missing("node");
          double x = fval(*ch, child[c], hc, c);
          if (c == p_ && rs) {
            double y = wval(*ch, child[c], ms_[m + 1][s - 1]);
            if (y > x) x = y;
          }
          acc += pc[c] * x;
        }
        wv[widx++] = acc;
        if (k == 0 && root_.lone) root_value_ = acc;
      }
      // hand states
      for (int hc = 0; hc < NH_; ++hc) {
        uint32_t hm = l.h[i * NH_ + hc];
        uint32_t fm = l.f[i * NH_ + hc];
        if (!(hm | fm)) continue;
        int m = hm_[hc], s = hs_[hc];
        int nms = m > 0 ? ms_[m - 1][s] : -1;
        double nextv = m > 0 ? wval(l, i, nms) : 0.0;
        auto cnext = [&](int c) -> double {
          if (m == 0) return 0.0;
          if (child[c] < 0) missing("node");
          return wval(*ch, child[c], nms);
        };
        double *hv = v + l.hoff[i * NH_ + hc];
        int idx = 0;
        while (hm) {
          int bit = __builtin_ctz(hm);
          hm &= hm - 1;
          int total = bit < 22 ? bit : bit - 10;
          double stand = g[stand_class(total)] + nextv;
          double best = stand, hit = NEG_INF;
          if (!locked_ && ((HITTABLE >> bit) & 1)) {
            hit = 0;
            for (int c = 0; c < NR; ++c) {
              if (pc[c] <= 0) continue;
              int nb = trans_[bit][c];
              if (nb < 0)
                hit += pc[c] * (cnext(c) - 1.0);
              else {
                if (child[c] < 0) missing("node");
                hit += pc[c] * hval(*ch, child[c], hc, nb);
              }
            }
            if (hit > best) best = hit;
          }
          hv[idx++] = best;
          if (k == 0 && !root_.lone && root_.second < 0) {
            root_value_ = best;
            root_stand_ = stand;
            root_hit_ = hit;
          }
        }
        while (fm) {
          int c0 = __builtin_ctz(fm);
          fm &= fm - 1;
          int bit = fbit_[c0];
          int total = bit < 22 ? bit : bit - 10;
          double stand = g[stand_class(total)] + nextv;
          double best = stand, hit = NEG_INF, dbl = NEG_INF;
          if (!locked_) {
            hit = 0;
            bool d = fdbl_[c0];
            if (d) dbl = 0;
            for (int c = 0; c < NR; ++c) {
              if (pc[c] <= 0) continue;
              int nb = trans_[bit][c];
              if (nb < 0) {
                double cn = cnext(c);
                hit += pc[c] * (cn - 1.0);
                if (d) dbl += pc[c] * (cn - 2.0);
              } else {
                if (child[c] < 0) missing("node");
                hit += pc[c] * hval(*ch, child[c], hc, nb);
                if (d) {
                  int nt = nb < 22 ? nb : nb - 10;
                  dbl += pc[c] * (2.0 * gval(*ch, child[c])[stand_class(nt)] + cnext(c));
                }
              }
            }
            if (hit > best) best = hit;
            if (d && dbl > best) best = dbl;
          }
          hv[idx++] = best;
          if (k == 0 && !root_.lone && root_.second >= 0) {
            root_value_ = best;
            root_stand_ = stand;
            root_hit_ = hit;
            root_dbl_ = dbl;
          }
        }
      }
    }
  }
};

}  // namespace bj
