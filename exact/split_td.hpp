#pragma once
#include "bj.hpp"

namespace bj {

// ---------------------------------------------------------------------------
// Split solver: full-information optimal play of every split hand.
// ---------------------------------------------------------------------------
// After a split the hands are played in order. The player's decision at every
// point may depend on every card seen so far, and is chosen to maximise the
// TOTAL expected return over all hands.
//
// Key fact used: once a hand is finished on total t, its expected payoff is
// g_t(R) where R is the set of cards seen at that moment, *no matter how the
// remaining hands are played*. (The cards the dealer will eventually use are
// exchangeable with the ones still in the shoe, so P(dealer outcome | seen) is
// a martingale and optional stopping applies.) So finished hands can be
// settled immediately and dropped from the state. The reference solver in
// split_ref.hpp does not use this and gives identical numbers.
//
// This is the simple top-down memoised version (hash table keyed on the
// state). LayeredSplitSolver below computes the same thing much faster.
//
// State: R  = multiset of cards drawn since the split,
//        m  = hands still waiting for their second card (besides the current),
//        s  = splits still allowed,
//        current hand (pips, soft, first-two-cards flag) or "needs a card".
class SplitSolverTD {
 public:
  SplitSolverTD(const Rules &rules, const DealerTable &dealer) : rules_(rules), dealer_(dealer) {}

  // unseen: cards not visible to the player (hole card included), i.e. the
  // shoe after removing the upcard and both pair cards. Returns the expected
  // total return of splitting, in units of the original bet.
  double solve(const Counts &unseen, int pair_rank, int upcard) {
    rem_ = unseen;
    n_ = total_cards(unseen);
    p_ = pair_rank;
    u_ = upcard;
    forb_ = upcard == TEN ? ACE : upcard == ACE ? TEN : -1;
    ra_ = 0;
    memo_.clear();
    gmemo_.clear();
    if (rules_.max_splits < 1) return NEG_INF;
    return split_value(1, rules_.max_splits - 1);
  }

  void set_state_limit(size_t n) { memo_.max_size = n; }
  size_t states() const { return memo_.size(); }
  size_t terminals() const { return gmemo_.size(); }

 private:
  Rules rules_;
  const DealerTable &dealer_;
  Counts rem_;
  int n_, p_, u_, forb_;
  uint64_t ra_;  // packed R
  FlatMap<double> memo_;
  FlatMap<G6> gmemo_;

  static constexpr int RSHIFT[NR] = {0, 7, 13, 19, 25, 31, 37, 43, 49, 55};

  void draw(int c) {
    rem_[c]--;
    n_--;
    ra_ += 1ull << RSHIFT[c];
  }
  void undraw(int c) {
    rem_[c]++;
    n_++;
    ra_ -= 1ull << RSHIFT[c];
  }
  void card_probs(double pc[NR]) const { next_card_probs(rem_, n_, forb_, pc); }

  const double *gvals() {
    Key k{ra_, 0};
    if (G6 *g = gmemo_.find(k)) return g->g;
    double dp[6];
    G6 g;
    dealer_probs(dealer_, rem_, n_, u_, dp);
    stand_values(dp, g.g);
    return gmemo_.insert(k, g)->g;
  }

  // Value of the hands that have not started yet.
  double next(int m, int s) { return m > 0 ? split_value(m - 1, s) : 0.0; }

  // Current hand holds a single pair card and needs its second card.
  double split_value(int m, int s) {
    Key key{ra_, (uint64_t(m) << 24) | (uint64_t(s) << 27) | (1ull << 40)};
    if (double *v = memo_.find(key)) return *v;
    double pc[NR];
    card_probs(pc);
    double v = 0;
    bool can_resplit = s > 0 && (p_ != ACE || rules_.resplit_aces);
    for (int c = 0; c < NR; ++c) {
      if (pc[c] <= 0) continue;
      draw(c);
      int pips = p_ + 1 + c + 1;
      bool soft = (p_ == ACE || c == ACE);  // two cards: pips <= 11 always if an ace
      double x = hand_value(m, s, pips, soft, true);
      if (c == p_ && can_resplit) {
        double y = split_value(m + 1, s - 1);
        if (y > x) x = y;
      }
      undraw(c);
      v += pc[c] * x;
    }
    memo_.insert(key, v);
    return v;
  }

  double hand_value(int m, int s, int pips, bool soft, bool first2) {
    int total = soft ? pips + 10 : pips;
    int cls = stand_class(total);
    bool split_ace_locked = p_ == ACE && !rules_.hit_split_aces;
    bool can_hit = !split_ace_locked && (total < 21 || soft);
    if (!can_hit) return gvals()[cls] + next(m, s);

    // With no hands waiting, the number of splits left no longer matters.
    Key key{ra_, (uint64_t(m) << 24) | (uint64_t(m ? s : 0) << 27) | (uint64_t(pips) << 30) |
                     (uint64_t(soft) << 35)};
    // A two-card hand has exactly one parent, so it is never looked up twice.
    if (!first2)
      if (double *v = memo_.find(key)) return *v;

    double best = gvals()[cls] + next(m, s);  // stand

    bool can_double = first2 && rules_.das && total != 21;
    if (rules_.double_on == DoubleOn::NineToEleven && (total < 9 || total > 11)) can_double = false;
    if (rules_.double_on == DoubleOn::TenToEleven && (total < 10 || total > 11)) can_double = false;

    double pc[NR];
    card_probs(pc);
    double hit = 0, dbl = 0;
    for (int c = 0; c < NR; ++c) {
      if (pc[c] <= 0) continue;
      draw(c);
      int np = pips + c + 1;
      bool nsoft = (soft || c == ACE) && np <= 11;
      if (np > 21) {
        double d = next(m, s);
        hit += pc[c] * (d - 1.0);
        if (can_double) dbl += pc[c] * (d - 2.0);
      } else {
        hit += pc[c] * hand_value(m, s, np, nsoft, false);
        if (can_double) {
          int nt = nsoft ? np + 10 : np;
          dbl += pc[c] * (2.0 * gvals()[stand_class(nt)] + next(m, s));
        }
      }
      undraw(c);
    }
    if (hit > best) best = hit;
    if (can_double && dbl > best) best = dbl;
    if (!first2) memo_.insert(key, best);
    return best;
  }
};

}  // namespace bj
