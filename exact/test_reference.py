"""Check the C++ engine against the brute-force reference on small shoes."""
import json
import random
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path

from reference import Rules, analyze_hand, round_ev

HERE = Path(__file__).resolve().parent
BJCA = str(HERE / "bjca")
NAMES = "A23456789T"
TOL = 2e-13


def rule_flags(r: Rules):
    f = ["--max-splits", str(r.max_splits), "--double-on", r.double_on, "--surrender", r.surrender,
         "--bj-payout", str(float(r.bj_payout))]
    if r.h17:
        f.append("--h17")
    if not r.das:
        f.append("--no-das")
    if r.resplit_aces:
        f.append("--resplit-aces")
    if r.hit_split_aces:
        f.append("--hit-split-aces")
    return f


def engine_hand(r, unseen, hand, up):
    cmd = [BJCA, "hand", "--json", "--unseen", ",".join(map(str, unseen)),
           "--player", ",".join(NAMES[c - 1] for c in hand), "--up", NAMES[up - 1]] + rule_flags(r)
    return json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)


def engine_round(r, shoe):
    cmd = [BJCA, "round", "--json", "--unseen", ",".join(map(str, shoe))] + rule_flags(r)
    return json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)


def random_rules(rng, max_splits):
    return Rules(
        h17=rng.random() < 0.5,
        das=rng.random() < 0.7,
        double_on=rng.choice(["any", "any", "9-11", "10-11"]),
        max_splits=max_splits,
        resplit_aces=rng.random() < 0.5,
        hit_split_aces=rng.random() < 0.5,
        surrender=rng.choice(["none", "early", "late"]),
        bj_payout=rng.choice([Fraction(3, 2), Fraction(6, 5)]),
    )


def random_shoe(rng, min_pips):
    while True:
        shoe = [rng.randint(0, 5) for _ in range(9)] + [rng.randint(2, 12)]
        if sum((i + 1) * c for i, c in enumerate(shoe)) >= min_pips:
            return shoe


worst = 0.0
checked = 0


def compare(label, got, want):
    global worst, checked
    for k in ("stand", "hit", "double", "split", "ev_play", "ev", "p_dealer_natural"):
        w = want[k]
        g = got[k]
        if w is None:
            if k in ("double", "split", "hit") and g is not None:
                raise SystemExit(f"{label}: {k} should be unavailable, engine says {g}")
            continue
        if g is None:
            raise SystemExit(f"{label}: {k} missing from engine, reference {float(w)}")
        d = abs(g - float(w))
        worst = max(worst, d)
        checked += 1
        if d > TOL:
            raise SystemExit(f"{label}: {k} engine {g!r} reference {float(w)!r} diff {d:.3e}")


def main():
    rng = random.Random(20251111)
    t0 = time.time()

    # 1. hands that cannot be split (2 and 3 cards), exact rational reference
    for i in range(60):
        r = random_rules(rng, rng.randint(0, 3))
        shoe = random_shoe(rng, 90)
        up = rng.choice([c for c in range(1, 11) if shoe[c - 1]])
        shoe[up - 1] -= 1
        while True:
            k = rng.choice([2, 2, 3])
            hand = [rng.choice([c for c in range(1, 11)]) for _ in range(k)]
            ok = all(hand.count(c) <= shoe[c - 1] for c in set(hand)) and sum(hand) <= 21
            if ok and not (k == 2 and hand[0] == hand[1]):
                break
        for c in hand:
            shoe[c - 1] -= 1
        want = analyze_hand(r, shoe, tuple(hand), up)
        compare(f"hand#{i} {hand} v {up} shoe {shoe} {r}", engine_hand(r, shoe, hand, up), want)
    print(f"non-split hands ok ({time.time() - t0:.1f}s, worst diff {worst:.2e})", flush=True)

    # 2. pairs, with 1..3 splits allowed, exact rational reference
    for i in range(30):
        max_splits = 1 + i % 3
        r = random_rules(rng, max_splits)
        min_pips = 31 * (max_splits + 1) + 40
        while True:
            shoe = random_shoe(rng, min_pips)
            pairs = [c for c in range(1, 11) if shoe[c - 1] >= 2 + (i % 2)]
            if pairs:
                break
        p = rng.choice(pairs) if i % 4 else (1 if 1 in pairs else pairs[0])
        shoe[p - 1] -= 2
        ups = [c for c in range(1, 11) if shoe[c - 1]]
        up = rng.choice(ups)
        shoe[up - 1] -= 1
        # keep the brute force tractable: thin the shoe (but never so far that
        # it could run out of cards mid-round)
        cap = {1: 32, 2: 26, 3: 22}[max_splits]
        while sum(shoe) > cap and sum((j + 1) * c for j, c in enumerate(shoe)) > min_pips + 12:
            j = rng.choice([j for j in range(10) if shoe[j] > (1 if j == p - 1 else 0)])
            shoe[j] -= 1
        t1 = time.time()
        want = analyze_hand(r, shoe, (p, p), up)
        got = engine_hand(r, shoe, [p, p], up)
        compare(f"pair#{i} {p},{p} v {up} shoe {shoe} {r}", got, want)
        print(f"  pair {p},{p} v {up} splits={max_splits} n={sum(shoe)}: split EV {got['split']:+.12f} "
              f"ok ({time.time() - t1:.1f}s)", flush=True)
    print(f"pairs ok ({time.time() - t0:.1f}s, worst diff {worst:.2e})", flush=True)

    # 3. whole-round EV on small shoes
    for i, max_splits in enumerate([0, 1, 1, 2]):
        r = random_rules(rng, max_splits)
        shoe = random_shoe(rng, 31 * (max_splits + 1) + 60)
        if max_splits == 2:
            shoe = [min(c, 3) for c in shoe[:9]] + [min(shoe[9], 7)]
        t1 = time.time()
        want = round_ev(r, shoe, number=float if max_splits else Fraction)
        got = engine_round(r, shoe)["ev"]
        d = abs(got - float(want))
        if d > 1e-12:
            raise SystemExit(f"round#{i} shoe {shoe} {r}: engine {got!r} reference {float(want)!r} diff {d:.3e}")
        print(f"  round shoe {shoe} splits={max_splits}: EV {got:+.12f} ok (diff {d:.1e}, {time.time() - t1:.1f}s)",
              flush=True)
    print(f"ALL OK: {checked} values compared, worst abs diff {worst:.2e}, {time.time() - t0:.1f}s")


if __name__ == "__main__":
    sys.exit(main())
