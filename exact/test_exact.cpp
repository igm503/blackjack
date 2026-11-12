// Internal consistency tests for the exact engine.
//   1. dealer trie vs. naive card-by-card dealer enumeration
//   2. three split solvers against each other:
//        SplitSolverW   - carries finished hands in the state (no shortcut)
//        SplitSolverTD  - top-down memoised, settles finished hands early
//        LayeredSplitSolver - the fast one, single- and multi-threaded
//   3. mid-split roots of the layered solver recombine to the fresh-split value
#include <cstdio>
#include <cstdlib>
#include <random>

#include "analyze.hpp"
#include "split_ref.hpp"
#include "split_td.hpp"

using namespace bj;

static int failures = 0;
static double worst = 0;
static void check(const char *what, double a, double b, double tol = 1e-12) {
  double d = std::fabs(a - b);
  if (d > worst) worst = d;
  if (!(d <= tol)) {
    printf("FAIL %s: %.15f vs %.15f (diff %.3e)\n", what, a, b, d);
    ++failures;
  }
}

// --- naive dealer: explicit hole card, one card at a time --------------------
static void naive_dealer_rec(Counts &c, int n, int pips, int aces, bool h17, double p, double out[6]) {
  bool soft = aces > 0 && pips <= 11;
  int total = soft ? pips + 10 : pips;
  if (total > 21) {
    out[5] += p;
    return;
  }
  if (total > 17 || (total == 17 && !(soft && h17))) {
    out[total - 17] += p;
    return;
  }
  for (int r = 0; r < NR; ++r) {
    if (c[r] == 0) continue;
    double q = p * c[r] / n;
    c[r]--;
    naive_dealer_rec(c, n - 1, pips + r + 1, aces + (r == ACE), h17, q, out);
    c[r]++;
  }
}
static void naive_dealer(Counts c, int u, bool h17, double out[6]) {
  int n = total_cards(c);
  int forb = forbidden_hole(u);
  for (int i = 0; i < 6; ++i) out[i] = 0;
  int allowed = n - (forb >= 0 ? c[forb] : 0);
  for (int h = 0; h < NR; ++h) {
    if (h == forb || c[h] == 0) continue;
    double p = double(c[h]) / allowed;
    c[h]--;
    naive_dealer_rec(c, n - 1, u + 1 + h + 1, (u == ACE) + (h == ACE), h17, p, out);
    c[h]++;
  }
}

int main(int argc, char **argv) {
  bool quick = argc > 1 && std::string(argv[1]) == "quick";
  std::mt19937 rng(12345);
  auto rnd = [&](int lo, int hi) { return int(lo + rng() % (hi - lo + 1)); };

  // 1. dealer
  for (int h17 = 0; h17 < 2; ++h17) {
    DealerTable dt(h17);
    for (int trial = 0; trial < 40; ++trial) {
      Counts c;
      if (trial == 0) c = full_shoe(1);
      else if (trial == 1) c = full_shoe(8);
      else {
        for (int r = 0; r < NR; ++r) c[r] = rnd(0, 9);
        c[TEN] = rnd(3, 30);
        c[rnd(0, 8)] += 6;  // make sure the shoe cannot run dry
        c[rnd(4, 8)] += 6;
      }
      int n = total_cards(c);
      for (int u = 0; u < NR; ++u) {
        double a[6], b[6];
        dealer_probs(dt, c, n, u, a);
        naive_dealer(c, u, h17, b);
        for (int i = 0; i < 6; ++i) check("dealer", a[i], b[i], 1e-13);
      }
    }
  }
  printf("dealer ok (worst %.2e)\n", worst);

  // 2. split solvers
  int cases = quick ? 40 : 160, ran = 0, triple = 0;
  for (int trial = 0; trial < cases; ++trial) {
    Rules r;
    r.h17 = rnd(0, 1);
    r.das = rnd(0, 3) != 0;
    r.double_on = DoubleOn(rnd(0, 3) % 3);
    r.max_splits = 1 + trial % 3;
    r.resplit_aces = rnd(0, 1);
    r.hit_split_aces = rnd(0, 1);
    DealerTable dt(r.h17);
    int decks = (trial % 4 == 0) ? rnd(2, 8) : 1;
    Counts c = full_shoe(decks);
    // random depletion
    int remove = rnd(0, decks == 1 ? 14 : 60);
    for (int i = 0; i < remove; ++i) {
      int x = rnd(0, 12);
      x = x > 9 ? 9 : x;
      if (c[x] > 2) c[x]--;
    }
    int p = rnd(0, 9);
    // keep the slow reference solvers tractable: long hands (low pairs, or
    // aces that may be hit) only with shallow resplits or a single deck
    bool long_hands = p < 5 && !(p == ACE && !r.hit_split_aces);
    if (long_hands && (r.max_splits == 3 || (r.max_splits == 2 && decks > 1))) p += 5;
    if (r.max_splits == 3 && decks > 1 && p < 7) p = rnd(7, 9);
    int u = rnd(0, 9);
    if (c[p] < 3 || c[u] < 2) continue;
    c[p] -= 2;
    c[u]--;
    ++ran;
    SplitSolverTD td(r, dt);
    double v_td = td.solve(c, p, u);
    LayeredSplitSolver l1(r, dt, 1), l4(r, dt, 4);
    auto r1 = l1.solve(c, p, u);
    auto r4 = l4.solve(c, p, u);
    check("layered(1) vs top-down", r1.value, v_td);
    check("layered(4) vs layered(1)", r4.value, r1.value, 0.0);
    bool small = td.states() < (quick ? 60000u : 200000u);
    double v_w = 0;
    if (small) {
      ++triple;
      SplitSolverW w(r, dt);
      v_w = w.solve(c, p, u);
      check("top-down vs carry-everything", v_td, v_w);
    }
    // 3. recombine mid-split roots: value after the split equals the average,
    // over the first hand's second card, of the best of (play it, resplit).
    if (trial % 3 == 0) {
      double pc[NR];
      next_card_probs(c, total_cards(c), forbidden_hole(u), pc);
      double acc = 0;
      for (int x = 0; x < NR; ++x) {
        if (pc[x] <= 0) continue;
        Counts c2 = c;
        c2[x]--;
        LayeredSplitSolver::Root root;
        root.lone = false;
        root.second = x;
        root.pending = 1;
        root.splits_left = r.max_splits - 1;
        auto hr = l1.solve(c2, p, u, &root);
        double best = hr.value;
        double m = std::max(hr.stand, std::max(hr.hit, hr.dbl));
        check("root actions", m, hr.value, 0.0);
        if (x == p && r.max_splits > 1 && (p != ACE || r.resplit_aces)) {
          LayeredSplitSolver::Root rs;
          rs.pending = 2;
          rs.splits_left = r.max_splits - 2;
          best = std::max(best, l1.solve(c2, p, u, &rs).value);
        }
        acc += pc[x] * best;
      }
      check("mid-split roots recombine", acc, r1.value);
    }
    if (trial % 10 == 0)
      printf("  case %d: SPL%d %dD pair %d v %d  EV %+.12f  (%zu states%s)\n", trial, r.max_splits, decks, p, u,
             r1.value, td.states(), small ? ", 3 solvers" : ", 2 solvers");
    fflush(stdout);
  }
  printf("split solvers ok: %d cases (%d with all three solvers), worst diff %.2e\n", ran, triple, worst);

  if (failures) {
    printf("%d FAILURES\n", failures);
    return 1;
  }
  printf("ALL OK\n");
  return 0;
}
