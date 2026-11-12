"""Python front end for the exact engine (calls the bjca binary).

Works with the objects used elsewhere in this repo:

    from exact.bjexact import round_ev, hand_evs
    ev = round_ev(counter, config)                 # PerfectCounter + GameConfig
    evs = hand_evs(counter, config, [8, 8], 6)     # counter already has those cards counted

Cards use this repo's convention: 2..10, with 11 for an ace.
`counter` may be a models.counter.PerfectCounter or a plain list where
remaining[card] is the number of unseen cards of that value (index 2..11).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

BJCA = Path(__file__).resolve().parent / "bjca"


def _counts(counter) -> list[int]:
    """Unseen cards as the engine's A,2,...,9,T order."""
    rem = getattr(counter, "remaining", counter)
    return [int(rem[11])] + [int(rem[c]) for c in range(2, 11)]


def _card(card: int) -> str:
    return "A" if card in (1, 11) else "T" if card == 10 else str(card)


def _rule_flags(config) -> list[str]:
    double_on = {0: "any", 1: "9-11", 2: "10-11"}[int(config.double_on)]
    surrender = {0: "none", 1: "early", 2: "late"}[int(config.surrender)]
    flags = ["--max-splits", str(int(config.resplit_limit)), "--double-on", double_on,
             "--surrender", surrender, "--bj-payout", repr(float(config.blackjack_payout))]
    if config.dealer_hits_soft_17:
        flags.append("--h17")
    if not config.double_after_split:
        flags.append("--no-das")
    if config.resplit_aces:
        flags.append("--resplit-aces")
    if config.hit_split_aces:
        flags.append("--hit-split-aces")
    return flags


def _run(args: list[str]) -> dict:
    if not BJCA.exists():
        raise RuntimeError(f"{BJCA} not built: run `make` in {BJCA.parent}")
    out = subprocess.run([str(BJCA)] + args + ["--json"], check=True, capture_output=True, text=True)
    return json.loads(out.stdout)


def round_analysis(counter, config, threads: int = 0) -> dict:
    """Exact EV of the next round plus action values for every starting hand."""
    args = ["round", "--unseen", ",".join(map(str, _counts(counter)))] + _rule_flags(config)
    if threads:
        args += ["--threads", str(threads)]
    return _run(args)


def round_ev(counter, config, threads: int = 0) -> float:
    """Exact expected value of the next round with perfect play."""
    return round_analysis(counter, config, threads)["ev"]


def hand_evs(counter, config, player_cards, dealer_card, split_pair=None, pending=0, splits_left=0) -> dict:
    """Exact EV of every action for one situation.

    `counter` must already exclude the player's cards and the dealer's upcard
    (i.e. they have been counted). For a hand that came from a split, pass the
    pair card as `split_pair`, the number of hands still waiting for their
    second card as `pending`, and how many more splits are allowed.
    Returns {"stand", "hit", "double", "split", "surrender", "best", "ev", ...};
    actions that are not allowed are None.
    """
    args = ["hand", "--unseen", ",".join(map(str, _counts(counter))),
            "--player", ",".join(_card(c) for c in player_cards), "--up", _card(dealer_card)] + _rule_flags(config)
    if split_pair is not None:
        args += ["--split-pair", _card(split_pair), "--pending", str(pending), "--splits-left", str(splits_left)]
    return _run(args)
