"""Brute-force reference for the exact analyzer.

Deliberately naive and written independently of the C++ engine:

  * cards are 1..10 (1 = ace) and hands are explicit tuples of cards;
  * the dealer's hole card is an explicit hidden variable. The player's
    knowledge about it is tracked with Bayes' rule one drawn card at a time,
    and the dealer is played out card by card for every possible hole card;
  * split hands are NOT settled early: the bets riding on finished hands are
    carried in the state and settled against the dealer only when every hand
    is complete, so the engine's "finished hands are a martingale" shortcut is
    tested rather than assumed;
  * arithmetic is exact (fractions.Fraction) unless number=float is passed.

Only usable on small shoes. The shoe must be large enough that it cannot run
out of cards mid-round.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class Rules:
    h17: bool = False
    das: bool = True
    double_on: str = "any"  # "any" | "9-11" | "10-11"
    max_splits: int = 3
    resplit_aces: bool = False
    hit_split_aces: bool = False
    surrender: str = "none"  # "none" | "early" | "late"
    bj_payout: Fraction = Fraction(3, 2)


def value(cards):
    pips = sum(cards)
    soft = 1 in cards and pips <= 11
    return (pips + 10 if soft else pips), soft


class TooBig(Exception):
    """The brute force exceeded its state budget."""


class Reference:
    state_limit = None  # optional cap on memoised split states

    def __init__(self, rules: Rules, unseen: tuple, up: int, number=Fraction):
        """unseen: counts for cards 1..10 (index 0 = ace) the player cannot see,
        hole card included. up: dealer upcard 1..10."""
        self.r = rules
        self.unseen0 = tuple(unseen)
        self.n0 = sum(unseen)
        self.up = up
        self.num = number
        self.forbidden = 1 if up == 10 else 10 if up == 1 else None
        self._joint = {}
        self._dealer = {}
        self._dealer_play = {}
        self._hand = {}
        self._split = {}

    # ---- knowledge about the hole card -------------------------------------
    def joint(self, drawn: tuple):
        """joint[h-1] = P(hole = h, hole is not a natural, player drew `drawn`).

        `drawn` is a count vector; the cards are drawn in ascending order
        (the probability of a sequence does not depend on its order)."""
        if drawn in self._joint:
            return self._joint[drawn]
        if sum(drawn) == 0:
            j = []
            for h in range(1, 11):
                if h == self.forbidden or self.unseen0[h - 1] == 0:
                    j.append(self.num(0))
                else:
                    j.append(self.num(self.unseen0[h - 1]) / self.num(self.n0))
        else:
            # peel off the highest drawn card
            c = max(i for i in range(10) if drawn[i]) + 1
            prev = list(drawn)
            prev[c - 1] -= 1
            prev = tuple(prev)
            jp = self.joint(prev)
            n_before = self.n0 - sum(prev)  # unseen incl. hole
            j = []
            for h in range(1, 11):
                avail = self.unseen0[c - 1] - prev[c - 1] - (1 if h == c else 0)
                if jp[h - 1] == 0 or avail <= 0 or n_before <= 1:
                    j.append(self.num(0))
                else:
                    j.append(jp[h - 1] * self.num(avail) / self.num(n_before - 1))
        self._joint[drawn] = tuple(j)
        return self._joint[drawn]

    def next_probs(self, drawn: tuple):
        """P(next card = c | everything the player has seen)."""
        tot = sum(self.joint(drawn))
        out = []
        for c in range(1, 11):
            d = list(drawn)
            d[c - 1] += 1
            out.append(sum(self.joint(tuple(d))) / tot if tot else self.num(0))
        return out

    # ---- dealer ------------------------------------------------------------
    def dealer_play(self, cards: tuple, shoe: tuple):
        """Outcome distribution {17..21 or 'bust': prob} from a dealer hand."""
        key = (tuple(sorted(cards)), shoe)
        if key in self._dealer_play:
            return self._dealer_play[key]
        total, soft = value(cards)
        if total > 21:
            res = {"bust": self.num(1)}
        elif total > 17 or (total == 17 and not (soft and self.r.h17)):
            res = {total: self.num(1)}
        else:
            res = {}
            n = sum(shoe)
            for c in range(1, 11):
                if shoe[c - 1] == 0:
                    continue
                p = self.num(shoe[c - 1]) / self.num(n)
                s2 = list(shoe)
                s2[c - 1] -= 1
                for k, v in self.dealer_play(cards + (c,), tuple(s2)).items():
                    res[k] = res.get(k, self.num(0)) + p * v
        self._dealer_play[key] = res
        return res

    def dealer_dist(self, drawn: tuple):
        if drawn in self._dealer:
            return self._dealer[drawn]
        j = self.joint(drawn)
        tot = sum(j)
        res = {}
        for h in range(1, 11):
            if j[h - 1] == 0:
                continue
            shoe = [self.unseen0[i] - drawn[i] for i in range(10)]
            shoe[h - 1] -= 1
            for k, v in self.dealer_play((self.up, h), tuple(shoe)).items():
                res[k] = res.get(k, self.num(0)) + j[h - 1] / tot * v
        self._dealer[drawn] = res
        return res

    def settle(self, done: tuple, drawn: tuple):
        """done: tuple of (total, bet) for finished, unbusted hands."""
        if not done:
            return self.num(0)
        ev = self.num(0)
        for outcome, p in self.dealer_dist(drawn).items():
            for total, bet in done:
                if outcome == "bust" or total > outcome:
                    ev += p * bet
                elif total < outcome:
                    ev -= p * bet
        return ev

    # ---- helpers -----------------------------------------------------------
    @staticmethod
    def add(drawn, c):
        d = list(drawn)
        d[c - 1] += 1
        return tuple(d)

    def can_double(self, cards, after_split: bool):
        if len(cards) != 2:
            return False
        if after_split and not self.r.das:
            return False
        total, _ = value(cards)
        if total == 21:
            return False
        if self.r.double_on == "9-11":
            return 9 <= total <= 11
        if self.r.double_on == "10-11":
            return 10 <= total <= 11
        return True

    # ---- a hand that has not been split ------------------------------------
    def hand_actions(self, cards: tuple, drawn: tuple):
        """(stand, hit, double) for an unsplit hand; None where not allowed."""
        key = (tuple(sorted(cards)), drawn)
        if key in self._hand:
            return self._hand[key]
        total, soft = value(cards)
        stand = self.settle(((total, 1),), drawn)
        hit = dbl = None
        probs = self.next_probs(drawn)
        if total < 21 or soft:
            hit = self.num(0)
            for c in range(1, 11):
                p = probs[c - 1]
                if p == 0:
                    continue
                nc = cards + (c,)
                if value(nc)[0] > 21:
                    hit -= p
                else:
                    s, h, _ = self.hand_actions(nc, self.add(drawn, c))
                    hit += p * (s if h is None else max(s, h))
        if self.can_double(cards, after_split=False):
            dbl = self.num(0)
            for c in range(1, 11):
                p = probs[c - 1]
                if p == 0:
                    continue
                t2 = value(cards + (c,))[0]
                if t2 > 21:
                    dbl -= 2 * p
                else:
                    dbl += p * self.settle(((t2, 2),), self.add(drawn, c))
        self._hand[key] = (stand, hit, dbl)
        return self._hand[key]

    # ---- split play --------------------------------------------------------
    def split_ev(self, pair: int):
        """EV of splitting [pair, pair]; unseen0 must exclude both pair cards."""
        if self.r.max_splits < 1:
            return None
        zero = (0,) * 10
        return self.lone(zero, (), pair, 1, self.r.max_splits - 1)

    def after(self, drawn, done, pair, pending, s):
        if pending > 0:
            return self.lone(drawn, done, pair, pending - 1, s)
        return self.settle(done, drawn)

    def lone(self, drawn, done, pair, pending, s):
        """Current hand is a single `pair` card about to receive a card."""
        key = ("L", drawn, done, pending, s)
        if key in self._split:
            return self._split[key]
        ev = self.num(0)
        probs = self.next_probs(drawn)
        for c in range(1, 11):
            p = probs[c - 1]
            if p == 0:
                continue
            d2 = self.add(drawn, c)
            best = self.split_hand(d2, done, pair, (pair, c), pending, s)
            if c == pair and s > 0 and (pair != 1 or self.r.resplit_aces):
                best = max(best, self.lone(d2, done, pair, pending + 1, s - 1))
            ev += p * best
        self._split[key] = ev
        return ev

    def split_hand(self, drawn, done, pair, cards, pending, s):
        cards = tuple(sorted(cards))
        key = ("H", drawn, done, cards, pending, s)
        if key in self._split:
            return self._split[key]
        if self.state_limit and len(self._split) > self.state_limit:
            raise TooBig()
        total, soft = value(cards)
        best = self.after(drawn, tuple(sorted(done + ((total, 1),))), pair, pending, s)
        locked = pair == 1 and not self.r.hit_split_aces
        probs = self.next_probs(drawn)
        if not locked and (total < 21 or soft):
            hit = self.num(0)
            for c in range(1, 11):
                p = probs[c - 1]
                if p == 0:
                    continue
                d2 = self.add(drawn, c)
                nc = cards + (c,)
                if value(nc)[0] > 21:
                    hit += p * (self.after(d2, done, pair, pending, s) - 1)
                else:
                    hit += p * self.split_hand(d2, done, pair, nc, pending, s)
            best = max(best, hit)
        if not locked and self.can_double(cards, after_split=True):
            dbl = self.num(0)
            for c in range(1, 11):
                p = probs[c - 1]
                if p == 0:
                    continue
                d2 = self.add(drawn, c)
                t2 = value(cards + (c,))[0]
                if t2 > 21:
                    dbl += p * (self.after(d2, done, pair, pending, s) - 2)
                else:
                    dbl += p * self.after(d2, tuple(sorted(done + ((t2, 2),))), pair, pending, s)
            best = max(best, dbl)
        self._split[key] = best
        return best


def analyze_hand(rules: Rules, unseen, hand, up, number=Fraction, state_limit=None):
    """Action values for a fresh hand. unseen excludes the hand and upcard."""
    ref = Reference(rules, tuple(unseen), up, number)
    ref.state_limit = state_limit
    zero = (0,) * 10
    stand, hit, dbl = ref.hand_actions(tuple(hand), zero)
    split = None
    if len(hand) == 2 and hand[0] == hand[1]:
        split = ref.split_ev(hand[0])
    n = sum(unseen)
    q = number(unseen[ref.forbidden - 1]) / number(n) if ref.forbidden else number(0)
    natural = len(hand) == 2 and value(hand)[0] == 21
    if natural:
        return dict(stand=stand, hit=hit, double=dbl, split=split, natural=True, p_dealer_natural=q,
                    ev_play=number(rules.bj_payout), ev=(1 - q) * number(rules.bj_payout))
    options = [x for x in (stand, hit, dbl, split) if x is not None]
    play = max(options)
    first_two = len(hand) == 2
    if rules.surrender == "late" and first_two:
        play = max(play, -number(1) / 2)
        ev = -q + (1 - q) * play
    elif rules.surrender == "early" and first_two:
        ev = max(-number(1) / 2, -q + (1 - q) * play)
    else:
        ev = -q + (1 - q) * play
    return dict(stand=stand, hit=hit, double=dbl, split=split, natural=False, p_dealer_natural=q,
                ev_play=play, ev=ev)


def round_ev(rules: Rules, shoe, number=Fraction):
    """EV of a round dealt from `shoe` (counts for 1..10) with optimal play."""
    shoe = list(shoe)
    n = sum(shoe)
    total = number(0)
    for up in range(1, 11):
        if shoe[up - 1] == 0:
            continue
        pu = number(shoe[up - 1]) / number(n)
        s1 = shoe[:]
        s1[up - 1] -= 1
        for c1 in range(1, 11):
            if s1[c1 - 1] == 0:
                continue
            p1 = number(s1[c1 - 1]) / number(n - 1)
            s2 = s1[:]
            s2[c1 - 1] -= 1
            for c2 in range(1, 11):  # ordered deal
                if s2[c2 - 1] == 0:
                    continue
                p2 = number(s2[c2 - 1]) / number(n - 2)
                s3 = s2[:]
                s3[c2 - 1] -= 1
                res = analyze_hand(rules, s3, (c1, c2), up, number)
                total += pu * p1 * p2 * res["ev"]
    return total
