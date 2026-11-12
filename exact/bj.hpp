// Exact blackjack combinatorial analyzer.
//
// Everything here is exhaustive enumeration over an exact finite shoe:
// no sampling, no infinite-deck shortcuts, no depth limits.
//
// Ranks are indexed 0..9: 0 = Ace, 1 = Two, ..., 8 = Nine, 9 = Ten/J/Q/K.
#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <map>
#include <stdexcept>
#include <thread>
#include <unordered_map>
#include <vector>

namespace bj {

constexpr int NR = 10;
constexpr int ACE = 0;
constexpr int TEN = 9;
constexpr double NEG_INF = -std::numeric_limits<double>::infinity();

enum class DoubleOn { Any = 0, NineToEleven = 1, TenToEleven = 2 };
enum class Surrender { None = 0, Early = 1, Late = 2 };

struct Rules {
  bool h17 = false;            // dealer hits soft 17
  bool das = true;             // double after split
  DoubleOn double_on = DoubleOn::Any;
  int max_splits = 3;          // number of splits allowed (hands = max_splits + 1)
  bool resplit_aces = true;
  bool hit_split_aces = true;  // false: split aces receive exactly one card each
  Surrender surrender = Surrender::None;
  double bj_payout = 1.5;
};

using Counts = std::array<int, NR>;

inline Counts full_shoe(int decks) {
  Counts c;
  c.fill(4 * decks);
  c[TEN] = 16 * decks;
  return c;
}
inline int total_cards(const Counts &c) {
  int n = 0;
  for (int x : c) n += x;
  return n;
}

// ---------------------------------------------------------------------------
// Dealer
// ---------------------------------------------------------------------------
// For each upcard we precompute every multiset of cards (hole card + draws)
// the dealer can finish on with 17..21, together with the number of distinct
// orders in which the dealer can legally draw it. The probability of one such
// multiset M from a shoe with counts C (n cards) is
//     orders(M) * prod_r C_r (C_r - 1) ... (C_r - M_r + 1) / (n (n-1) ... (n - |M| + 1)).
// The multisets are stored as a prefix trie laid out breadth-first: a node's
// numerator is its parent's times one factor, and nodes on the same level do
// not depend on each other, so the CPU can overlap the multiplies instead of
// waiting on one long dependency chain. Bust probability is the complement.
struct DealerProgram {
  std::vector<uint16_t> parent;  // node i > 0: index of its prefix (node 0 = empty)
  std::vector<uint8_t> widx;     // node i > 0: slot of its factor (C_rank - k) in the w table
  struct Group {                 // finishing hands with the same size and total
    uint32_t begin, end;         // range in fin_node / fin_mult
    uint8_t depth, total;
  };
  std::vector<Group> groups;
  std::vector<uint16_t> fin_node;
  std::vector<double> fin_mult;  // number of legal draw orders
  int woff[NR + 1];              // w-table offset per rank; woff[NR] = table size
  int maxdepth = 0;

  // The same trie in depth-first order, for the original stack-based
  // evaluation (kept so the benchmark can time it; see dealer_probs_dfs).
  struct Instr {
    uint8_t depth, rank, k, total;
    uint32_t skip;  // size of this node's subtree
    double mult;
  };
  std::vector<Instr> dfs;
};

// Benchmark switch: evaluate the dealer with the original depth-first walk.
inline bool g_dealer_depth_first = false;
// Benchmark switch: let the layered solver batch dealer evaluations with SIMD.
inline bool g_dealer_simd = true;

struct DealerTable {
  std::array<DealerProgram, NR> prog;
  bool h17;

  explicit DealerTable(bool h17_) : h17(h17_) {
    for (int u = 0; u < NR; ++u) build(u);
  }

 private:
  static bool must_hit(int pips, int aces, bool h17) {
    bool soft = aces > 0 && pips <= 11;
    int total = soft ? pips + 10 : pips;
    return total < 17 || (total == 17 && soft && h17);
  }

  void enumerate(int pips, int aces, int ncards, Counts &m,
                 std::map<Counts, double> &finals) const {
    // (pips, aces) include the upcard; m is the multiset drawn after it.
    for (int r = 0; r < NR; ++r) {
      int np = pips + r + 1, na = aces + (r == ACE);
      if (np > 21) break;  // ranks ascend in value, so every later one busts too
      m[r]++;
      if (must_hit(np, na, h17)) {
        enumerate(np, na, ncards + 1, m, finals);
      } else {
        bool natural = ncards == 1 && np == 11 && na == 1;
        if (!natural) finals[m] += 1.0;
      }
      m[r]--;
    }
  }

  void build(int u) {
    std::map<Counts, double> finals;
    Counts m{};
    enumerate(u + 1, u == ACE, 1, m, finals);
    // Trie over sorted card sequences (high ranks first: they repeat least,
    // which keeps the trie small).
    struct TNode {
      int parent, depth, rank, k, total;
      double mult;
    };
    std::vector<TNode> trie{{-1, 0, -1, 0, 0, 0.0}};
    std::map<std::pair<int, int>, int> edge;  // (parent, rank) -> node
    Counts maxk{};
    for (auto &[cnt, mult] : finals) {
      int node = 0, depth = 0;
      for (int r = NR - 1; r >= 0; --r)
        for (int i = 0; i < cnt[r]; ++i) {
          auto key = std::make_pair(node, r);
          auto it = edge.find(key);
          if (it == edge.end()) {
            trie.push_back({node, depth + 1, r, i, 0, 0.0});
            it = edge.emplace(key, int(trie.size()) - 1).first;
          }
          node = it->second;
          ++depth;
          maxk[r] = std::max(maxk[r], i + 1);
        }
      int pips = u + 1, aces = u == ACE;
      for (int r = 0; r < NR; ++r) pips += cnt[r] * (r + 1), aces += (r == ACE) * cnt[r];
      trie[node].total = (aces > 0 && pips <= 11) ? pips + 10 : pips;
      trie[node].mult = mult;
    }
    // breadth-first order
    std::vector<int> order(trie.size());
    for (size_t i = 0; i < order.size(); ++i) order[i] = int(i);
    std::stable_sort(order.begin(), order.end(), [&](int a, int b) { return trie[a].depth < trie[b].depth; });
    std::vector<int> pos(trie.size());
    for (size_t i = 0; i < order.size(); ++i) pos[order[i]] = int(i);
    DealerProgram &P = prog[u];
    P.woff[0] = 0;
    for (int r = 0; r < NR; ++r) P.woff[r + 1] = P.woff[r] + maxk[r];
    P.parent.assign(trie.size(), 0);
    P.widx.assign(trie.size(), 0);
    std::map<std::pair<int, int>, std::vector<std::pair<int, double>>> groups;
    for (size_t i = 1; i < order.size(); ++i) {
      const TNode &t = trie[order[i]];
      P.parent[i] = uint16_t(pos[t.parent]);
      P.widx[i] = uint8_t(P.woff[t.rank] + t.k);
      P.maxdepth = std::max(P.maxdepth, t.depth);
      if (t.total) groups[{t.depth, t.total}].push_back({int(i), t.mult});
    }
    // depth-first copy
    std::vector<std::vector<int>> kids(trie.size());
    for (size_t i = 1; i < trie.size(); ++i) kids[trie[i].parent].push_back(int(i));
    struct Walk {
      const std::vector<TNode> &trie;
      const std::vector<std::vector<int>> &kids;
      std::vector<DealerProgram::Instr> &out;
      void go(int node) {
        size_t at = out.size();
        const TNode &t = trie[node];
        out.push_back({uint8_t(t.depth - 1), uint8_t(t.rank), uint8_t(t.k), uint8_t(t.total), 0, t.mult});
        for (int c : kids[node]) go(c);
        out[at].skip = uint32_t(out.size() - at - 1);
      }
    } walk{trie, kids, P.dfs};
    for (int c : kids[0]) walk.go(c);
    for (auto &[key, v] : groups) {
      DealerProgram::Group g;
      g.begin = uint32_t(P.fin_node.size());
      for (auto &[node, mult] : v) {
        P.fin_node.push_back(uint16_t(node));
        P.fin_mult.push_back(mult);
      }
      g.end = uint32_t(P.fin_node.size());
      g.depth = uint8_t(key.first);
      g.total = uint8_t(key.second);
      P.groups.push_back(g);
    }
  }
};

constexpr int MAX_DEALER_NODES = 2048;
constexpr int MAX_DEALER_W = 96;

// Probabilities of the dealer finishing on 17,18,19,20,21 given the unseen
// cards `c` (which include the hole card) and upcard `u`, conditional on the
// dealer not holding a natural. out[5] receives the bust probability.
inline void dealer_probs_dfs(const DealerTable &t, const Counts &c, int n, int u, double out[6]);

inline void dealer_probs(const DealerTable &t, const Counts &c, int n, int u, double out[6]) {
  if (g_dealer_depth_first) return dealer_probs_dfs(t, c, n, u, out);
  const DealerProgram &P = t.prog[u];
  double w[MAX_DEALER_W];
  for (int r = 0; r < NR; ++r) {
    int k = 0;
    for (int i = P.woff[r]; i < P.woff[r + 1]; ++i, ++k) w[i] = c[r] > k ? double(c[r] - k) : 0.0;
  }
  double val[MAX_DEALER_NODES];
  val[0] = 1.0;
  const size_t nn = P.parent.size();
  const uint16_t *par = P.parent.data();
  const uint8_t *wi = P.widx.data();
  for (size_t i = 1; i < nn; ++i) val[i] = val[par[i]] * w[wi[i]];
  // 1 / (n (n-1) ... (n-d+1))
  double invff[32];
  invff[0] = 1.0;
  for (int d = 1; d <= P.maxdepth; ++d) invff[d] = n - d + 1 > 0 ? invff[d - 1] / double(n - d + 1) : 0.0;
  double acc[5] = {0, 0, 0, 0, 0};
  const uint16_t *fn = P.fin_node.data();
  const double *fm = P.fin_mult.data();
  for (const auto &g : P.groups) {
    double s0 = 0, s1 = 0, s2 = 0, s3 = 0;
    uint32_t j = g.begin;
    for (; j + 4 <= g.end; j += 4) {
      s0 += fm[j] * val[fn[j]];
      s1 += fm[j + 1] * val[fn[j + 1]];
      s2 += fm[j + 2] * val[fn[j + 2]];
      s3 += fm[j + 3] * val[fn[j + 3]];
    }
    for (; j < g.end; ++j) s0 += fm[j] * val[fn[j]];
    acc[g.total - 17] += ((s0 + s1) + (s2 + s3)) * invff[g.depth];
  }
  // condition on no natural
  double omega = 1.0;
  if (u == TEN) omega = 1.0 - double(c[ACE]) / n;
  if (u == ACE) omega = 1.0 - double(c[TEN]) / n;
  double s = 0;
  for (int i = 0; i < 5; ++i) {
    out[i] = omega > 0 ? acc[i] / omega : 0.0;
    s += out[i];
  }
  out[5] = 1.0 - s;
}

// Vectorised dealer evaluation: W shoes at once against the same upcard.
// The trie walk is identical for every shoe (only the factors differ), so
// each step becomes one SIMD multiply. Lane results do not depend on what the
// other lanes hold.
template <int W>
inline void dealer_probs_batch(const DealerTable &t, const Counts *const c[W], const int n[W], int u,
                               double out[W][6]) {
  typedef double vec __attribute__((vector_size(8 * W)));
  const DealerProgram &P = t.prog[u];
  vec w[MAX_DEALER_W];
  for (int r = 0; r < NR; ++r) {
    int k = 0;
    for (int i = P.woff[r]; i < P.woff[r + 1]; ++i, ++k)
      for (int l = 0; l < W; ++l) w[i][l] = (*c[l])[r] > k ? double((*c[l])[r] - k) : 0.0;
  }
  vec val[MAX_DEALER_NODES];
  for (int l = 0; l < W; ++l) val[0][l] = 1.0;
  const size_t nn = P.parent.size();
  const uint16_t *par = P.parent.data();
  const uint8_t *wi = P.widx.data();
  for (size_t i = 1; i < nn; ++i) val[i] = val[par[i]] * w[wi[i]];
  vec invff[32];
  for (int l = 0; l < W; ++l) {
    invff[0][l] = 1.0;
    for (int d = 1; d <= P.maxdepth; ++d)
      invff[d][l] = n[l] - d + 1 > 0 ? invff[d - 1][l] / double(n[l] - d + 1) : 0.0;
  }
  vec acc[5];
  for (auto &a : acc)
    for (int l = 0; l < W; ++l) a[l] = 0.0;
  const uint16_t *fn = P.fin_node.data();
  const double *fm = P.fin_mult.data();
  for (const auto &g : P.groups) {
    vec s0, s1;
    for (int l = 0; l < W; ++l) s0[l] = 0.0, s1[l] = 0.0;
    uint32_t j = g.begin;
    for (; j + 2 <= g.end; j += 2) {
      s0 += fm[j] * val[fn[j]];
      s1 += fm[j + 1] * val[fn[j + 1]];
    }
    for (; j < g.end; ++j) s0 += fm[j] * val[fn[j]];
    acc[g.total - 17] += (s0 + s1) * invff[g.depth];
  }
  for (int l = 0; l < W; ++l) {
    double omega = 1.0;
    if (u == TEN) omega = 1.0 - double((*c[l])[ACE]) / n[l];
    if (u == ACE) omega = 1.0 - double((*c[l])[TEN]) / n[l];
    double s = 0;
    for (int i = 0; i < 5; ++i) {
      out[l][i] = omega > 0 ? acc[i][l] / omega : 0.0;
      s += out[l][i];
    }
    out[l][5] = 1.0 - s;
  }
}

// Original evaluation: walk the trie depth-first carrying a stack of prefix
// probabilities. Same arithmetic, but every step waits on the previous one.
inline void dealer_probs_dfs(const DealerTable &t, const Counts &c, int n, int u, double out[6]) {
  const DealerProgram &P = t.prog[u];
  double w[NR][32];
  for (int r = 0; r < NR; ++r)
    for (int k = 0; k < P.woff[r + 1] - P.woff[r]; ++k) w[r][k] = c[r] > k ? double(c[r] - k) : 0.0;
  double inv[32];
  for (int d = 0; d <= P.maxdepth; ++d) inv[d] = n - d > 0 ? 1.0 / double(n - d) : 0.0;
  double st[33];
  st[0] = 1.0;
  double acc[5] = {0, 0, 0, 0, 0};
  const size_t sz = P.dfs.size();
  for (size_t i = 0; i < sz;) {
    const auto &in = P.dfs[i];
    double p = st[in.depth] * w[in.rank][in.k] * inv[in.depth];
    if (p == 0.0) {
      i += in.skip + 1;
      continue;
    }
    st[in.depth + 1] = p;
    if (in.total) acc[in.total - 17] += in.mult * p;
    ++i;
  }
  double omega = 1.0;
  if (u == TEN) omega = 1.0 - double(c[ACE]) / n;
  if (u == ACE) omega = 1.0 - double(c[TEN]) / n;
  double s = 0;
  for (int i = 0; i < 5; ++i) {
    out[i] = omega > 0 ? acc[i] / omega : 0.0;
    s += out[i];
  }
  out[5] = 1.0 - s;
}

// Expected value of a one-unit bet standing in class `cls`
// (0: any total <= 16, 1..5: 17..21) from dealer probabilities.
inline void stand_values(const double dp[6], double g[6]) {
  double bust = dp[5];
  g[0] = bust - (1.0 - bust);
  double below = bust;  // P(dealer bust or dealer total < t)
  for (int t = 0; t < 5; ++t) {
    double above = 1.0 - below - dp[t];
    g[t + 1] = below - above;
    below += dp[t];
  }
}
inline int stand_class(int total) { return total <= 16 ? 0 : total - 16; }

// Probability weight for "hole card is not the rank that would make a
// natural", given unseen counts c (n cards, hole card included).
inline double omega(const Counts &c, int n, int u) {
  if (n <= 0) return 0.0;
  if (u == TEN) return 1.0 - double(c[ACE]) / n;
  if (u == ACE) return 1.0 - double(c[TEN]) / n;
  return 1.0;
}

inline int forbidden_hole(int u) { return u == TEN ? ACE : u == ACE ? TEN : -1; }

// Probability of each rank being the next card drawn, given the unseen cards
// `rem` (n of them, hole card included) and that the hole card is not `forb`
// (-1: no constraint). Derivation: P(c | hole != f) =
//   P(c) * P(hole != f | c drawn) / P(hole != f)
// with all three terms plain draws without replacement.
inline void next_card_probs(const Counts &rem, int n, int forb, double pc[NR]) {
  if (n <= 0) {
    for (int c = 0; c < NR; ++c) pc[c] = 0;
    return;
  }
  if (forb < 0) {
    double inv = 1.0 / n;
    for (int c = 0; c < NR; ++c) pc[c] = rem[c] * inv;
    return;
  }
  int f = rem[forb];
  double denom = double(n - 1) * double(n - f);
  if (denom <= 0) {
    for (int c = 0; c < NR; ++c) pc[c] = 0;
    return;
  }
  for (int c = 0; c < NR; ++c) {
    int f2 = f - (c == forb);
    pc[c] = double(rem[c]) * double(n - 1 - f2) / denom;
  }
}

struct G6 {
  double g[6];
};

// Stand values for every class given unseen cards (hole included) and upcard.
inline void stand_values_for(const DealerTable &t, const Counts &rem, int n, int u, double g[6]) {
  double dp[6];
  dealer_probs(t, rem, n, u, dp);
  stand_values(dp, g);
}

// Simple fork-join helper: f(thread_id, begin, end) over [0, n).
template <class F>
inline void parallel_for(size_t n, int threads, F &&f) {
  if (threads <= 1 || n < 2) {
    f(0, size_t(0), n);
    return;
  }
  size_t T = std::min<size_t>(threads, n);
  std::vector<std::thread> th;
  th.reserve(T);
  for (size_t t = 0; t < T; ++t) th.emplace_back([&, t] { f(int(t), n * t / T, n * (t + 1) / T); });
  for (auto &x : th) x.join();
}

// ---------------------------------------------------------------------------
// Flat open-addressing memo table
// ---------------------------------------------------------------------------
struct Key {
  uint64_t a, b;
  bool operator==(const Key &o) const { return a == o.a && b == o.b; }
};
inline uint64_t hash_key(const Key &k) {
  uint64_t h = k.a * 0x9E3779B97F4A7C15ull;
  h ^= h >> 29;
  h += k.b * 0xC2B2AE3D27D4EB4Full;
  h ^= h >> 32;
  h *= 0xD6E8FEB86659FD93ull;
  h ^= h >> 29;
  return h;
}

template <class V>
class FlatMap {
  struct Slot {
    Key k;
    V v;
  };
  std::vector<Slot> slots_;
  size_t mask_ = 0, size_ = 0;
  static constexpr uint64_t EMPTY = ~0ull;

 public:
  size_t max_size = ~size_t(0);  // insert throws std::length_error beyond this
  explicit FlatMap(size_t cap = 1 << 12) { init(cap); }
  size_t size() const { return size_; }
  void clear() {
    for (auto &s : slots_) s.k.a = EMPTY;
    size_ = 0;
  }
  V *find(const Key &k) {
    size_t i = hash_key(k) & mask_;
    while (true) {
      Slot &s = slots_[i];
      if (s.k.a == EMPTY) return nullptr;
      if (s.k == k) return &s.v;
      i = (i + 1) & mask_;
    }
  }
  V *insert(const Key &k, const V &v) {
    if (size_ >= max_size) throw std::length_error("state limit exceeded");
    if ((size_ + 1) * 10 > slots_.size() * 6) grow();
    size_t i = hash_key(k) & mask_;
    while (true) {
      Slot &s = slots_[i];
      if (s.k.a == EMPTY) {
        s.k = k;
        s.v = v;
        ++size_;
        return &s.v;
      }
      if (s.k == k) {
        s.v = v;
        return &s.v;
      }
      i = (i + 1) & mask_;
    }
  }

 private:
  void init(size_t cap) {
    slots_.assign(cap, Slot{{EMPTY, 0}, V{}});
    mask_ = cap - 1;
    size_ = 0;
  }
  void grow() {
    std::vector<Slot> old;
    old.swap(slots_);
    init(old.size() * 2);
    for (auto &s : old)
      if (s.k.a != EMPTY) insert(s.k, s.v);
  }
};

}  // namespace bj
