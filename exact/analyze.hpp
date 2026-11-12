// Whole-hand and whole-round exact analysis built on the dealer table, the
// non-split hand solver and the split solver.
#pragma once

#include <atomic>
#include <chrono>
#include <string>

#include "bj.hpp"
#include "split_layered.hpp"
#include "split_ref.hpp"
#include "split_td.hpp"

namespace bj {

// ---------------------------------------------------------------------------
// Every multiset of cards totalling 21 or less (3,072 of them including the
// empty hand), ordered so that a hand's children (hand + one card) precede it.
// ---------------------------------------------------------------------------
struct HandTable {
  struct Node {
    Counts cnt;
    int pips;    // ace = 1
    int ncards;
    int child[NR];  // -1: bust
    bool soft() const { return cnt[ACE] > 0 && pips <= 11; }
    int total() const { return soft() ? pips + 10 : pips; }
  };
  std::vector<Node> nodes;
  std::map<Counts, int> index;

  HandTable() {
    std::vector<Counts> all;
    Counts c{};
    gen(0, 0, c, all);
    auto pips = [](const Counts &x) {
      int s = 0;
      for (int r = 0; r < NR; ++r) s += x[r] * (r + 1);
      return s;
    };
    std::stable_sort(all.begin(), all.end(),
                     [&](const Counts &a, const Counts &b) { return pips(a) > pips(b); });
    for (auto &x : all) {
      Node n;
      n.cnt = x;
      n.pips = pips(x);
      n.ncards = total_cards(x);
      index[x] = int(nodes.size());
      nodes.push_back(n);
    }
    for (auto &n : nodes)
      for (int r = 0; r < NR; ++r) {
        Counts x = n.cnt;
        x[r]++;
        auto it = index.find(x);
        n.child[r] = it == index.end() ? -1 : it->second;
      }
  }
  int find(const Counts &c) const {
    auto it = index.find(c);
    return it == index.end() ? -1 : it->second;
  }

 private:
  static void gen(int r, int pips, Counts &c, std::vector<Counts> &out) {
    if (r == NR) {
      out.push_back(c);
      return;
    }
    for (int k = 0; pips + k * (r + 1) <= 21; ++k) {
      c[r] = k;
      gen(r + 1, pips + k * (r + 1), c, out);
    }
    c[r] = 0;
  }
};

// ---------------------------------------------------------------------------
// Exact stand / hit / double values for every player hand against one upcard,
// with optimal composition-dependent play after a hit. All values are
// conditional on the dealer not having a natural (hole card already peeked).
// ---------------------------------------------------------------------------
class HandSolver {
 public:
  std::vector<double> stand, hit, dbl;
  std::vector<uint8_t> valid;

  HandSolver(const DealerTable &dealer, const HandTable &hands) : dealer_(dealer), hands_(hands) {
    size_t n = hands.nodes.size();
    stand.assign(n, NEG_INF);
    hit.assign(n, NEG_INF);
    dbl.assign(n, NEG_INF);
    valid.assign(n, 0);
  }

  // shoe_u: every card except the upcard (the hole card and the player's own
  // cards are still in it). If `within` is given, only hands containing it
  // are solved.
  void solve(const Counts &shoe_u, int upcard, const Counts *within = nullptr) {
    const int forb = forbidden_hole(upcard);
    const int nu = total_cards(shoe_u);
    const auto &nodes = hands_.nodes;
    for (size_t i = 0; i < nodes.size(); ++i) {
      const auto &h = nodes[i];
      valid[i] = 0;
      stand[i] = hit[i] = dbl[i] = NEG_INF;
      if (h.ncards < 2) continue;
      bool ok = true;
      Counts rem;
      for (int r = 0; r < NR && ok; ++r) {
        rem[r] = shoe_u[r] - h.cnt[r];
        if (rem[r] < 0 || (within && h.cnt[r] < (*within)[r])) ok = false;
      }
      if (!ok) continue;
      valid[i] = 1;
      int n = nu - h.ncards;
      double g[6];
      stand_values_for(dealer_, rem, n, upcard, g);
      int total = h.total();
      stand[i] = g[stand_class(total)];
      bool can_hit = total < 21 || h.soft();
      if (!can_hit && h.ncards != 2) continue;
      double pc[NR];
      next_card_probs(rem, n, forb, pc);
      double hv = 0, dv = 0;
      for (int c = 0; c < NR; ++c) {
        if (pc[c] <= 0) continue;
        int ch = h.child[c];
        if (ch < 0) {
          hv -= pc[c];
          dv -= 2 * pc[c];
        } else {
          hv += pc[c] * std::max(stand[ch], hit[ch]);
          dv += 2 * pc[c] * stand[ch];
        }
      }
      if (can_hit) hit[i] = hv;
      if (h.ncards == 2) dbl[i] = dv;
    }
  }

 private:
  const DealerTable &dealer_;
  const HandTable &hands_;
};

enum Action { STAND = 0, HIT = 1, DOUBLE = 2, SPLIT = 3, SURRENDER = 4 };
inline const char *action_name(int a) {
  static const char *n[] = {"stand", "hit", "double", "split", "surrender"};
  return n[a];
}

struct HandResult {
  // Expected values per unit bet. stand/hit/dbl/split are conditional on the
  // dealer not having a natural (the peek has happened). NEG_INF = not allowed.
  double stand = NEG_INF, hit = NEG_INF, dbl = NEG_INF, split = NEG_INF, surrender = NEG_INF;
  double p_dealer_natural = 0;  // before the peek, given the visible cards
  bool natural = false;         // player holds a two-card 21
  int best = STAND;
  double ev_play = 0;  // value of best action given no dealer natural
  double ev = 0;       // value before the dealer peeks
  double prob = 0;     // (round analysis) probability of being dealt this
  size_t split_nodes = 0;
};

inline bool double_allowed(const Rules &r, int total) {
  if (total == 21) return false;
  if (r.double_on == DoubleOn::NineToEleven) return total >= 9 && total <= 11;
  if (r.double_on == DoubleOn::TenToEleven) return total >= 10 && total <= 11;
  return true;
}

// Combine action values into a decision, applying surrender and naturals.
inline void finish(HandResult &h, const Rules &rules) {
  if (h.natural) {
    h.best = STAND;
    h.ev_play = rules.bj_payout;
    h.ev = (1.0 - h.p_dealer_natural) * rules.bj_payout;
    return;
  }
  h.best = STAND;
  h.ev_play = h.stand;
  auto consider = [&](int a, double v) {
    if (v > h.ev_play) h.ev_play = v, h.best = a;
  };
  consider(HIT, h.hit);
  consider(DOUBLE, h.dbl);
  consider(SPLIT, h.split);
  double q = h.p_dealer_natural;
  if (rules.surrender == Surrender::Late) {
    h.surrender = -0.5;
    consider(SURRENDER, -0.5);
    h.ev = -q + (1 - q) * h.ev_play;
  } else if (rules.surrender == Surrender::Early) {
    // Early surrender happens before the peek, so compare unconditional values.
    h.surrender = -0.5;
    double play = -q + (1 - q) * h.ev_play;
    if (-0.5 > play) {
      h.best = SURRENDER;
      h.ev = -0.5;
    } else {
      h.ev = play;
    }
  } else {
    h.ev = -q + (1 - q) * h.ev_play;
  }
}

// Which split solver to use. All three compute the same number; the slower
// ones are kept as cross-checks and so the benchmark can time every iteration.
enum class SplitAlgo {
  Layered,   // layer-by-layer streaming solver (default)
  TopDown,   // hash-memoised recursion, finished hands settled early
  CarryAll,  // hash-memoised recursion carrying every finished hand
};

struct Engine {
  Rules rules;
  DealerTable dealer;
  HandTable hands;
  int threads;
  SplitAlgo algo = SplitAlgo::Layered;
  size_t state_limit = ~size_t(0);  // TopDown / CarryAll only
  size_t eval_memory_budget = size_t(6) << 30;  // working memory for concurrent upcards
  bool share_plans = true;  // one forward pass per pair, shared by all upcards

  explicit Engine(const Rules &r, int threads_ = 0)
      : rules(r), dealer(r.h17), threads(threads_ > 0 ? threads_ : default_threads()) {}

  static int default_threads() {
    unsigned n = std::thread::hardware_concurrency();
    return n ? int(n) : 4;
  }

  // Exact EV of splitting a pair, given the unseen cards (hole card included,
  // the pair and the upcard already removed).
  double split_ev(const Counts &unseen, int pair_rank, int upcard, int inner_threads,
                  size_t *nodes = nullptr) const {
    if (rules.max_splits < 1) return NEG_INF;
    if (algo == SplitAlgo::TopDown) {
      SplitSolverTD s(rules, dealer);
      s.set_state_limit(state_limit);
      double v = s.solve(unseen, pair_rank, upcard);
      if (nodes) *nodes = s.states();
      return v;
    }
    if (algo == SplitAlgo::CarryAll) {
      SplitSolverW s(rules, dealer);
      s.set_state_limit(state_limit);
      double v = s.solve(unseen, pair_rank, upcard);
      if (nodes) *nodes = s.states();
      return v;
    }
    LayeredSplitSolver s(rules, dealer, inner_threads);
    auto res = s.solve(unseen, pair_rank, upcard);
    if (nodes) *nodes = res.nodes;
    return res.value;
  }

  // A fresh (not previously split) hand against an upcard. `unseen` is every
  // card the player cannot see: the shoe plus the hole card.
  HandResult analyze_hand(const Counts &unseen, const Counts &hand, int upcard) const {
    HandResult out;
    int id = hands.find(hand);
    if (id < 0) throw std::invalid_argument("hand is bust or empty");
    const auto &node = hands.nodes[id];
    if (node.ncards < 2) throw std::invalid_argument("hand needs at least two cards");
    Counts shoe_u;
    for (int r = 0; r < NR; ++r) shoe_u[r] = unseen[r] + hand[r];
    HandSolver hs(dealer, hands);
    hs.solve(shoe_u, upcard, &hand);
    int n = total_cards(unseen);
    int forb = forbidden_hole(upcard);
    out.p_dealer_natural = forb >= 0 && n > 0 ? double(unseen[forb]) / n : 0.0;
    out.natural = node.ncards == 2 && node.total() == 21;
    out.stand = hs.stand[id];
    out.hit = hs.hit[id];
    if (node.ncards == 2) {
      if (double_allowed(rules, node.total())) out.dbl = hs.dbl[id];
      for (int r = 0; r < NR; ++r)
        if (hand[r] == 2) out.split = split_ev(unseen, r, upcard, threads, &out.split_nodes);
    }
    HandResult tmp = out;
    if (node.ncards != 2) {
      // surrender is only offered on the first two cards
      Rules r2 = rules;
      r2.surrender = Surrender::None;
      finish(tmp, r2);
    } else {
      finish(tmp, rules);
    }
    return tmp;
  }

  // A hand created by a split, part-way through the round. `unseen` is every
  // card the player cannot see (all cards of all split hands and the upcard
  // already removed). `pending` hands are still waiting for their second card
  // and `splits_left` more splits are allowed. The returned values are the
  // expected return of this hand PLUS all hands not yet played; hands already
  // finished are unaffected by anything done from here on and are excluded.
  HandResult analyze_split_hand(const Counts &unseen, int pair_rank, const Counts &hand, int upcard,
                                int pending, int splits_left) const {
    int id = hands.find(hand);
    if (id < 0) throw std::invalid_argument("hand is bust or empty");
    const auto &node = hands.nodes[id];
    if (node.ncards < 2 || hand[pair_rank] < 1)
      throw std::invalid_argument("split hand must hold the pair card and at least two cards");
    if (pending < 0 || splits_left < 0 || pending + splits_left > rules.max_splits)
      throw std::invalid_argument("pending + splits_left exceeds max_splits");
    HandResult out;
    LayeredSplitSolver solver(rules, dealer, threads);
    LayeredSplitSolver::Root root;
    root.lone = false;
    root.pending = pending;
    root.splits_left = splits_left;
    if (node.ncards == 2) {
      Counts rest = hand;
      rest[pair_rank]--;
      for (int r = 0; r < NR; ++r)
        if (rest[r]) root.second = r;
    } else {
      root.pips = node.pips;
      root.soft = node.soft();
    }
    auto res = solver.solve(unseen, pair_rank, upcard, &root);
    out.split_nodes = res.nodes;
    out.stand = res.stand;
    out.hit = res.hit;
    out.dbl = res.dbl;
    if (node.ncards == 2 && root.second == pair_rank && splits_left > 0 &&
        (pair_rank != ACE || rules.resplit_aces)) {
      LayeredSplitSolver::Root rs;
      rs.pending = pending + 1;
      rs.splits_left = splits_left - 1;
      auto r2 = solver.solve(unseen, pair_rank, upcard, &rs);
      out.split = r2.value;
      out.split_nodes += r2.nodes;
    }
    Rules r2 = rules;
    r2.surrender = Surrender::None;
    finish(out, r2);
    return out;
  }

  struct Round {
    double ev = 0;
    // cell[upcard][c1][c2] with c1 <= c2
    HandResult cell[NR][NR][NR];
    double seconds = 0, split_seconds = 0;
    size_t split_nodes = 0;
  };

  // Exact expected value of the next round dealt from `shoe`, with optimal
  // play, plus the full table of action values for every starting hand.
  Round analyze_round(const Counts &shoe) const {
    auto t0 = std::chrono::steady_clock::now();
    Round out;
    const int n = total_cards(shoe);
    if (n < 4) throw std::invalid_argument("shoe too small");
    // 1. non-split values for every upcard
    std::vector<HandSolver> hs;
    for (int u = 0; u < NR; ++u) hs.emplace_back(dealer, hands);
    std::atomic<int> nextu{0};
    parallel_for(size_t(std::min(threads, NR)), threads, [&](int, size_t, size_t) {
      for (int u; (u = nextu++) < NR;) {
        if (shoe[u] == 0) continue;
        Counts su = shoe;
        su[u]--;
        hs[u].solve(su, u);
      }
    });
    // 2. cells
    struct Task {
      int u, p;
    };
    std::vector<Task> tasks;
    for (int u = 0; u < NR; ++u) {
      if (shoe[u] == 0) continue;
      Counts su = shoe;
      su[u]--;
      double pu = double(shoe[u]) / n;
      for (int c1 = 0; c1 < NR; ++c1)
        for (int c2 = c1; c2 < NR; ++c2) {
          HandResult &cell = out.cell[u][c1][c2];
          Counts hand{};
          hand[c1]++;
          hand[c2]++;
          bool ok = true;
          for (int r = 0; r < NR; ++r) ok &= su[r] >= hand[r];
          if (!ok) continue;
          double ph = double(su[c1]) / (n - 1);
          Counts s2 = su;
          s2[c1]--;
          ph *= double(s2[c2]) / (n - 2);
          if (c1 != c2) ph *= 2;
          s2[c2]--;
          cell.prob = pu * ph;
          int forb = forbidden_hole(u);
          cell.p_dealer_natural = forb >= 0 ? double(s2[forb]) / (n - 3) : 0.0;
          int id = hands.find(hand);
          const auto &node = hands.nodes[id];
          cell.natural = node.total() == 21;
          cell.stand = hs[u].stand[id];
          cell.hit = hs[u].hit[id];
          if (double_allowed(rules, node.total())) cell.dbl = hs[u].dbl[id];
          if (c1 == c2 && rules.max_splits >= 1) tasks.push_back({u, c1});
        }
    }
    // 3. splits
    auto ts = std::chrono::steady_clock::now();
    std::atomic<size_t> nodes{0};
    if (algo == SplitAlgo::Layered && share_plans) {
      // Which states are reachable does not depend on the upcard, so each
      // pair gets one forward pass (plan) shared by all of its upcards.
      std::vector<int> ups[NR];
      for (auto &t : tasks) ups[t.p].push_back(t.u);
      auto supply_for = [&](int p) {
        Counts s = shoe;
        s[p] -= 2;
        return s;
      };
      auto eval = [&](const LayeredSplitSolver &solver, const LayeredSplitSolver::Plan &P, int p, int u, int inner) {
        Counts unseen = supply_for(p);
        unseen[u]--;
        auto res = solver.evaluate(P, unseen, u, inner);
        out.cell[u][p][p].split = res.value;
        out.cell[u][p][p].split_nodes = res.nodes;
        nodes += res.nodes;
      };
      bool big = rules.max_splits >= 3 && n > 60;
      if (!big) {
        // Small problems: every plan, then every (pair, upcard) evaluation,
        // side by side on one thread each.
        LayeredSplitSolver solver(rules, dealer, 1);
        std::vector<LayeredSplitSolver::Plan> plans(NR);
        std::atomic<int> nextp{0};
        parallel_for(size_t(std::min(threads, NR)), threads, [&](int, size_t, size_t) {
          for (int p; (p = nextp++) < NR;)
            if (!ups[p].empty()) plans[p] = solver.plan(supply_for(p), p);
        });
        std::stable_sort(tasks.begin(), tasks.end(), [&](const Task &x, const Task &y) {
          return plans[x.p].values > plans[y.p].values;
        });
        std::atomic<size_t> next{0};
        parallel_for(size_t(threads), threads, [&](int, size_t, size_t) {
          for (size_t i; (i = next++) < tasks.size();) eval(solver, plans[tasks[i].p], tasks[i].p, tasks[i].u, 1);
        });
      } else {
        // Big problems: one pair at a time to bound memory. The plan uses all
        // threads; its upcards are then evaluated as many at a time as fit in
        // the memory budget, sharing the threads between them.
        LayeredSplitSolver solver(rules, dealer, threads);
        for (int p = 0; p < NR; ++p) {
          if (ups[p].empty()) continue;
          auto P = solver.plan(supply_for(p), p);
          size_t need = std::max<size_t>(P.eval_bytes(solver.hand_combos()), 1);
          size_t kmax = std::min<size_t>(size_t(threads), std::max<size_t>(1, eval_memory_budget / need));
          // waves of upcards; each wave shares all the threads between its members
          for (size_t done = 0; done < ups[p].size();) {
            size_t k = std::min(kmax, ups[p].size() - done);
            int inner = int((size_t(threads) + k - 1) / k);
            parallel_for(k, int(k), [&](int, size_t a, size_t b) {
              for (size_t i = a; i < b; ++i) eval(solver, P, p, ups[p][done + i], inner);
            });
            done += k;
          }
        }
      }
    } else {
      auto run = [&](const Task &t, int inner) {
        Counts unseen = shoe;
        unseen[t.u]--;
        unseen[t.p] -= 2;
        size_t nn = 0;
        out.cell[t.u][t.p][t.p].split = split_ev(unseen, t.p, t.u, inner, &nn);
        out.cell[t.u][t.p][t.p].split_nodes = nn;
        nodes += nn;
      };
      // largest problems first: low pairs make the longest hands
      std::stable_sort(tasks.begin(), tasks.end(), [](const Task &x, const Task &y) { return x.p < y.p; });
      if (algo == SplitAlgo::Layered && rules.max_splits >= 3 && n > 60) {
        // big problems one at a time, each using every thread
        for (auto &t : tasks) run(t, threads);
      } else {
        std::atomic<size_t> next{0};
        std::atomic<bool> failed{false};
        std::string err;
        parallel_for(size_t(threads), threads, [&](int, size_t, size_t) {
          for (size_t i; !failed.load() && (i = next++) < tasks.size();) {
            try {
              run(tasks[i], 1);
            } catch (const std::exception &e) {
              if (!failed.exchange(true)) err = e.what();
            }
          }
        });
        if (failed.load()) throw std::runtime_error(err);
      }
    }
    out.split_nodes = nodes.load();
    out.split_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - ts).count();
    // 4. decisions and total
    double ev = 0;
    for (int u = 0; u < NR; ++u)
      for (int c1 = 0; c1 < NR; ++c1)
        for (int c2 = c1; c2 < NR; ++c2) {
          HandResult &cell = out.cell[u][c1][c2];
          if (cell.prob <= 0) continue;
          finish(cell, rules);
          ev += cell.prob * cell.ev;
        }
    out.ev = ev;
    out.seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    return out;
  }
};

}  // namespace bj
