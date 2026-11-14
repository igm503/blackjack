// Reference split solver that does NOT use the martingale decomposition:
// it carries the bets riding on every finished hand in the state and only
// settles them against the dealer once all hands are complete. It is far
// slower than bj::SplitSolver and exists to verify it.
#pragma once
#include "bj.hpp"
namespace bj {
// ---------------------------------------------------------------------------
// Split solver: full-information optimal play of every split hand.
// ---------------------------------------------------------------------------
// After a split the hands are played in order. The player's decision at every
// point may depend on everything seen so far, and is chosen to maximise the
// TOTAL expected return over all hands (finished hands still care about what
// the dealer ends up with, so later decisions account for them).
//
// State: R  = multiset of cards drawn since the split,
//        w  = total bet riding on each standing class (<=16,17,...,21),
//        m  = hands still waiting for their second card (besides the current),
//        s  = splits still allowed,
//        current hand (pips, soft, first-two-cards flag) or "needs a card".
class SplitSolverW {
 public:
  SplitSolverW(const Rules &rules, const DealerTable &dealer) : rules_(rules), dealer_(dealer) {}

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
    for (int r = 0; r < NR; ++r) rcount_[r] = 0;
    memo_.clear();
    gmemo_.clear();
    if (rules_.max_splits < 1) return NEG_INF;
    return split_value(0, 1, rules_.max_splits - 1);
  }

  void set_state_limit(size_t n) { memo_.max_size = n; }
  size_t states() const { return memo_.size(); }
  size_t terminals() const { return gmemo_.size(); }

 private:
  Rules rules_;
  const DealerTable &dealer_;
  Counts rem_;
  int n_, p_, u_, forb_;
  uint64_t ra_;       // packed R
  int rcount_[NR];
  FlatMap<double> memo_;
  FlatMap<G6> gmemo_;

  static constexpr int RSHIFT[NR] = {0, 7, 13, 19, 25, 31, 37, 43, 49, 55};

  double om() const { return forb_ < 0 ? 1.0 : 1.0 - double(rem_[forb_]) / n_; }
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

  static uint64_t wadd(uint64_t w, int cls, int bet) { return w + (uint64_t(bet) << (4 * cls)); }

  const double *gvals() {
    Key k{ra_, 0};
    if (G6 *g = gmemo_.find(k)) return g->g;
    double dp[6];
    G6 g;
    dealer_probs(dealer_, rem_, n_, u_, dp);
    stand_values(dp, g.g);
    return gmemo_.insert(k, g)->g;
  }

  double done(uint64_t w, int m, int s) {
    if (m > 0) return split_value(w, m - 1, s);
    if (w == 0) return 0.0;
    const double *g = gvals();
    double v = 0;
    for (int cls = 0; cls < 6; ++cls) {
      int wt = (w >> (4 * cls)) & 15;
      if (wt) v += wt * g[cls];
    }
    return v;
  }

  // Current hand holds a single pair card and needs its second card.
  double split_value(uint64_t w, int m, int s) {
    Key key{ra_, w | (uint64_t(m) << 24) | (uint64_t(s) << 27) | (1ull << 40)};
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
      double x = hand_value(w, m, s, pips, soft, true);
      if (c == p_ && can_resplit) {
        double y = split_value(w, m + 1, s - 1);
        if (y > x) x = y;
      }
      undraw(c);
      v += pc[c] * x;
    }
    memo_.insert(key, v);
    return v;
  }

  double hand_value(uint64_t w, int m, int s, int pips, bool soft, bool first2) {
    int total = soft ? pips + 10 : pips;
    int cls = stand_class(total);
    bool split_ace_locked = p_ == ACE && !rules_.hit_split_aces;
    bool can_hit = !split_ace_locked && (total < 21 || soft);
    if (!can_hit) return done(wadd(w, cls, 1), m, s);

    Key key{ra_, w | (uint64_t(m) << 24) | (uint64_t(s) << 27) | (uint64_t(pips) << 30) |
                     (uint64_t(soft) << 35) | (uint64_t(first2) << 36)};
    if (double *v = memo_.find(key)) return *v;

    double best = done(wadd(w, cls, 1), m, s);  // stand

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
        double d = done(w, m, s);
        hit += pc[c] * (d - 1.0);
        if (can_double) dbl += pc[c] * (d - 2.0);
      } else {
        hit += pc[c] * hand_value(w, m, s, np, nsoft, false);
        if (can_double) {
          int nt = nsoft ? np + 10 : np;
          dbl += pc[c] * done(wadd(w, stand_class(nt), 2), m, s);
        }
      }
      undraw(c);
    }
    if (hit > best) best = hit;
    if (can_double && dbl > best) best = dbl;
    memo_.insert(key, best);
    return best;
  }
};

}  // namespace bj
