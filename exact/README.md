# Exact blackjack engine

Computes blackjack expected values by enumerating every possible playout from an
exact, finite shoe: no sampling, no infinite-deck shortcut, no cap on split
depth. It works on real shoe sizes (1 to 8 decks, or any leftover composition
mid-shoe) and is independent of the Python simulator in the parent directory.

```
make                 # builds ./bjca (C++17, no dependencies)
./bjca hand --decks 6 --player 8,8 --up T
./bjca round --decks 6 --table
```

## What it computes

**One situation** (`bjca hand`): the exact EV of stand, hit, double, split and
surrender for a hand against an upcard, given exactly which cards are unseen.

```
$ ./bjca hand --decks 6 --player 8,8 --up T
player 8,8 vs dealer T, unseen (A,2..9,T): 24 24 24 24 24 24 24 22 24 95  [309 cards]
exact EV per unit bet, given the dealer has no natural:
  stand     -0.5368532992
  hit       -0.5353610384
  double    -1.0707220767
  split     -0.4748783315
best: split
```

**A hand in the middle of a split** (`--split-pair`): the same, for a hand that
came from a split, with other hands still waiting. Values are this hand plus all
hands not yet played.

```
./bjca hand --decks 6 --player 8,3 --up T --split-pair 8 --pending 1 --splits-left 2 --seen 8
```

**The whole next round** (`bjca round`): exact EV of the round with perfect
play, and the best action for every starting hand against every upcard.

Shoe: `--decks N` plus `--seen C,C,...` for cards already gone, or `--unseen`
with ten counts (A,2,...,9,T) for an arbitrary composition.
Rules: `--h17`, `--no-das`, `--double-on any|9-11|10-11`, `--max-splits N`,
`--resplit-aces`, `--hit-split-aces`, `--surrender none|early|late`,
`--bj-payout X`. `--json` on any command.

From Python, `bjexact.py` takes this repo's `PerfectCounter` and `GameConfig`:

```python
from exact.bjexact import round_ev, hand_evs
ev = round_ev(counter, config)
evs = hand_evs(counter, config, [8, 8], 6)   # cards in this repo's 2..11 convention
```

## What "exact" means here

* Dealer outcome probabilities come from enumerating every dealer draw
  sequence against the actual remaining cards.
* Hit and double values account for every card the player could draw and the
  best play after each, with the shoe updated card by card.
* **Splits are solved as the full game tree.** Every hand is played out, every
  resplit is followed, and at every decision the player may use every card seen
  so far, across all split hands, to maximise the total return of all hands.
  Nothing is pruned.
* The dealer peeks for a natural (US hole-card rule). Action values are
  conditional on the dealer not having one; the round EV accounts for it. The
  information "the hole card is not an ace/ten" is carried exactly through
  every later draw.

The only inexactness is double-precision rounding, about 1e-15.

Conventions that affect the numbers:

* After a split, each hand receives its second card when its turn comes, and
  the decision to resplit is made as soon as that card is seen.
* A hand may be hit on any total below 21 and on soft 21, not on hard 21.
* A 21 made after splitting is not a blackjack.
* Insurance is not modelled.
* The shoe must be large enough that it cannot run out mid-round.

## Why it is fast

The first exact solver here was a memoised recursion over the game tree. With
three resplits at 6 to 8 decks it needs hundreds of millions of states per
pair. The speedups, in the order they were made:

1. **Finished hands settle immediately.** Once a split hand is finished, its
   expected payoff given the cards seen so far cannot be changed by how the
   remaining hands are played: the cards the dealer will use are exchangeable
   with the rest of the shoe, so the dealer's outcome distribution is a
   martingale in the cards seen and optional stopping applies. Finished hands
   therefore drop out of the state. `split_ref.hpp` keeps a solver that does
   not use this, and the two agree to 1e-15.
2. **Layer-by-layer streaming.** States are grouped by the multiset of cards
   drawn since the split. Every move draws one card, so states with k cards
   drawn depend only on those with k+1. Layers are kept sorted; "add one card"
   is monotone on the sort key, so both the reachability pass and the value
   pass find children with merge cursors. No hash table, and one dealer
   evaluation is shared by every state with the same cards.
3. **Dealer evaluation laid out breadth-first.** The dealer's possible final
   hands form a trie; walking it depth-first makes every multiply wait for the
   previous one. Level by level they overlap. Where every child of a card set
   already has dealer values, the parent's are their average (the same
   martingale), with no enumeration at all.
4. **Threads.** Each layer is split across cores in both passes.
5. **One reachability pass per pair.** Which states are reachable does not
   depend on the upcard, so a pair is planned once and its ten upcards are
   evaluated side by side.
6. **SIMD dealer evaluation.** Four (or eight) card compositions go through
   the dealer trie per pass.
7. **All upcards of a pair in one pass** (big whole-round problems only): the
   node is decoded and its children located once, and the values for the
   upcards sit side by side.
8. **One dealer walk for all upcards.** The ten upcards' dealer tries have
   6,148 nodes between them; their union has 1,695, and an upcard's numerators
   differ from the shared ones by a single factor. Ordered low ranks first,
   38% of the union depends only on the counts of A to 5. Layers are sorted
   with tens in the low bits, so runs of neighbouring card sets share those
   counts and that part is computed once per run.

On one thread the remaining time is now spread evenly: state values about
40%, dealer evaluation 25%, locating children and bookkeeping the rest.

## How much faster could it get?

Whole round, 6 decks, resplit to four hands is about 2 billion (card set,
upcard) evaluations, each costing about 200 nanoseconds of CPU.

About two thirds of those states exist only to confirm that hitting a hard 17
to 20 in a split hand is bad. Skipping them and bounding the skipped moves
instead gives the same numbers three times faster, but the bound needs an
assumption about how much one extra seen card can be worth to the hands still
to be played. That is not full enumeration, so it is not in this code (it is in
the history of this branch, commit 149d1c4). Other things that did not help:

* Dropping card sets too unlikely to matter: probability is not concentrated.
  Removing every set with probability under 1e-22 removes only 20% of them.
* Proving bad hits bad on the last hand only, where no assumption is needed:
  the extra states come from bad hits in the earlier hands.

Every one of those card sets is a different situation the player could be in,
with its own exact answer, so full enumeration has to visit them all. One
second would need about 4 ns of CPU per evaluation on this machine.

## Benchmark history

`bench/run.py` times a fixed suite with any of these iterations (all are still
selectable: `bjca bench-case --case ID --variant V`) and writes
`bench/history.json`. `bench/server.py` serves a page that charts it and can
build and time the current tree as a new iteration:

```
python3 bench/server.py          # prints the address, http://127.0.0.1:8377/ by default
python3 bench/run.py record --label "what changed"
python3 bench/run.py show
```

| # | Iteration | 8,8 v 10 (6D, 4 hands) | 2,2 v 6 (8D, 4 hands) | Whole round, 1 split (6D) | Whole round, 4 hands (6D) |
|---|---|---|---|---|---|
| 0 | Original code (main branch) | cannot run | cannot run | cannot run | cannot run |
| 1 | Memoised game tree | stopped at 60M states | stopped at 60M states | 35.3 s | stopped at 60M states |
| 2 | Settle finished hands early | 29.0 s | stopped at 60M states | 31.3 s | stopped at 60M states |
| 3 | Layered streaming solver | 4.38 s | 1m 19s | 28.7 s | 1h 03m |
| 4 | Breadth-first dealer evaluation | 3.42 s | 42.6 s | 5.53 s | 29m 22s |
| 5 | Multi-threaded layers | 0.97 s | 6.84 s | 0.79 s | 4m 25s |
| 6 | One forward pass per pair | 0.73 s | 6.27 s | 0.54 s | 2m 42s |
| 7 | SIMD dealer evaluation | 0.61 s | 5.08 s | 0.41 s | 2m 03s |
| 8 | Dynamic scheduling | 0.58 s | 4.87 s | 0.37 s | 1m 59s |
| 9 | All upcards in one pass | 0.55 s | 5.04 s | 0.35 s | 1m 55s |
| 10 | One dealer walk for all upcards | 0.57 s | 4.82 s | 0.34 s | 1m 33s |

Apple M2 Max, 12 threads; iterations 1 to 4 are single-threaded, and 1 and 2 are stopped once their hash table passes 60 million states (about 5 GB). The machine was in use while these were taken and timings of the long case varied by 20 to 40% between runs; the last two rows are the best of three. Every iteration returns the same EV for each case to 15 digits.

## Checking it

```
make test        # about 6 minutes
make test-full   # about 20 minutes
```

* `test_exact.cpp`: the dealer trie against a naive card-by-card dealer; three
  independent split solvers against each other (carry every finished hand /
  settle early / layered, single and multi-threaded) over random rules, shoes,
  pairs and upcards; mid-split positions recombined into the value of the
  split; and batched against one-at-a-time evaluation.
* `reference.py` + `test_reference.py`: a deliberately naive Python brute force
  with exact rational arithmetic. It tracks the hole card with Bayes' rule,
  plays the dealer out for every possible hole card, and settles split hands
  only when all are finished. The engine is compared with it on small shoes
  for non-split hands, pairs with 1 to 3 splits, and whole-round EV.

## Files

| File | |
|---|---|
| `bj.hpp` | rules, shoe, dealer probabilities, card probabilities given the peek |
| `split_layered.hpp` | the fast split solver (`plan` + `evaluate`) |
| `split_td.hpp`, `split_ref.hpp` | the two slower split solvers, kept as cross-checks |
| `analyze.hpp` | non-split hand solver, single-situation and whole-round analysis |
| `bjca.cpp` | command line |
| `bjexact.py` | Python front end |
| `bench/` | benchmark runner, history, web page |

## Limits

Cost is driven by resplits: each extra split multiplies the work by roughly 20
to 30. Low pairs are the worst case because they make the longest hands. The
split solver's working memory peaks at about 5 GB for 2,2 with three resplits
at 8 decks. `--max-splits` above 3 is supported up to 6 but is only practical
on small shoes. Mid-split queries (`--split-pair`) always use full enumeration.
