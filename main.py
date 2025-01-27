from collections import defaultdict
import argparse
from copy import deepcopy

from deck import Deck, DoubleOn, Hand
from counter import Counter, NoneCounter, HighLowCounter, PerfectCounter


class CountingType:
    NONE = 0
    HIGH_LOW = 1
    PERFECT = 2


class BlackJackPayout:
    THREE_TWO = 3 / 2
    SIX_FIVE = 6 / 5


class Surrender:
    NONE = 0
    EARLY = 1
    LATE = 2


class Move:
    STAND = 0
    HIT = 1
    DOUBLE = 2
    SPLIT = 3


def main(
    min_bet: int,
    bankroll: int,
    num_decks: int,
    blackjack_payout: float,
    hit_soft_17: bool,
    double_after_split: bool,
    double_on: int,
    resplit_limit: int,
    resplit_aces: bool,
    hit_split_aces: bool,
    original_bet_only: bool,
    surrender: int,
):
    Hand.set_rules(hit_soft_17, double_after_split, double_on, hit_split_aces, resplit_aces)
    deck = Deck(num_decks)
    deck.shuffle()

    counter = PerfectCounter(num_decks)

    num_hands = 0
    num_doubles = 0
    initial_bankroll = bankroll

    while bankroll > 0:
        doubling = False
        if deck.must_shuffle:
            counter = NoneCounter(num_decks)
            deck.shuffle()
            print("Shuffling")

        num_hands += 1

        print(f"\n\n Hand {num_hands}, Bankroll: {bankroll}")
        # print(f"Bet change per hand: {(bankroll - initial_bankroll) / (min_bet * num_hands)}")
        bankroll -= min_bet

        dealer = deck.deal_hand()
        player = deck.deal_hand()

        counter.count(player.cards)
        counter.count(dealer.cards[0])

        # print("Player:", player)
        # print("Dealer:", dealer)

        dealer_face = dealer.cards[0]

        if dealer_face in [10, 11]:
            if dealer_face == 11:
                # do insurance
                pass
            if dealer.is_blackjack:
                if player.is_blackjack:
                    # print("Dealer Blackjack, Push")
                    bankroll += min_bet
                else:
                    pass
                    # print("Dealer Blackjack, Player Loses")
                counter.count(dealer.cards[1])
                continue

        if player.is_blackjack:
            # print("Player Blackjack, Player Wins")
            assert int((1 + blackjack_payout) * min_bet) == (1 + blackjack_payout) * min_bet
            bankroll += int((1 + blackjack_payout) * min_bet)
            counter.count(dealer.cards[1])
            continue

        while not player.is_bust:
            move = get_move(player, dealer_face, counter)
            if move == Move.HIT:
                new_card = deck.deal_card()
                player.add(new_card)
                counter.count(new_card)
            elif move == Move.DOUBLE:
                new_card = deck.deal_card()
                print("DOUBLING")
                doubling = True
                player.double(new_card)
                counter.count(new_card)
                bankroll -= min_bet
                num_doubles += 1
                break
            else:
                break

            # print("Player:", player)

        if player.is_bust:
            # print("Player Bust, Player Loses")
            counter.count(dealer.cards[1])
            continue

        counter.count(dealer.cards[1])
        while dealer.must_hit:
            new_card = deck.deal_card()
            dealer.add(new_card)
            counter.count(new_card)

        # print("Player:", player)
        # print("Dealer:", dealer)

        if dealer.is_bust or player.value > dealer.value:
            # print("Player Wins")
            bankroll += 2 * min_bet
            if player.is_double:
                bankroll += 2 * min_bet
        elif player.value < dealer.value:
            # print("Player Loses")
            pass
        else:
            # print("Push")
            bankroll += min_bet
            if player.is_double:
                bankroll += min_bet

        if doubling:
            assert player.is_double

    print(f"Played {num_hands} hands")
    print(f"Number of doubles: {num_doubles}")
    print(f"Bet change per hand: {-initial_bankroll / (min_bet * num_hands)}")


def dealer_rollout_exact(dealer_hand: Hand, counter: Counter) -> dict[int, float]:
    if dealer_hand.must_hit:
        dealer_probs = defaultdict(float)
        for card in range(2, 12):
            card_prob = counter.probability(card)
            temp_counter = deepcopy(counter)
            temp_counter.count(card)
            temp_dealer_hand = deepcopy(dealer_hand)
            temp_dealer_hand.add(card)
            probs = dealer_rollout_exact(temp_dealer_hand, temp_counter)
            for value, prob in probs.items():
                dealer_probs[value] += card_prob * prob
        return dealer_probs
    else:
        value = dealer_hand.value
        value = 0 if dealer_hand.is_bust else value
        return {value: 1.0}


def dealer_rollout_approximate(dealer_hand: Hand, counter: Counter) -> dict[int, float]:
    if not dealer_hand.must_hit:
        return {dealer_hand.value: 1.0}

    hard_states = [(v, False) for v in range(4, 22)]
    soft_states = [(v, True) for v in range(12, 22)]
    states = hard_states + soft_states
    probs = {}

    for value, soft in states:
        if value >= 17 and value <= 21:
            probs[(value, soft)] = {value: 1.0}

    if dealer_hand.hit_soft_17:
        del probs[(17, True)]

    need_processing = True
    while need_processing:
        need_processing = False

        for value, soft in states:
            if (value, soft) in probs or value > 21:
                continue

            next_states_ready = True
            dealer_probs = defaultdict(float)

            for card in range(2, 12):
                card_prob = counter.probability(card)
                if card_prob <= 0:
                    continue

                new_value = value + card
                new_soft = soft or card == 11

                if new_value > 21 and new_soft:
                    new_value -= 10
                    new_soft = False

                if new_value <= 21 and (new_value, new_soft) not in probs:
                    next_states_ready = False
                    break

                if new_value > 21:
                    dealer_probs[0] += card_prob
                    continue

                next_probs = probs[(new_value, new_soft)]
                for val, prob in next_probs.items():
                    dealer_probs[val] += card_prob * prob

            if next_states_ready:
                probs[(value, soft)] = dict(dealer_probs)
            else:
                need_processing = True

    return probs[(dealer_hand.value, dealer_hand.is_soft)]


def dealer_rollout(
    dealer_face: int, counter: Counter, no_blackjack: bool = True
) -> dict[int, float]:
    dealer_probs = defaultdict(float)
    for card in range(2, 12):
        hand = Hand([dealer_face, card])
        if no_blackjack and hand.is_blackjack:
            continue
        card_prob = counter.probability(card)
        probs = dealer_rollout_approximate(hand, counter)
        for value, prob in probs.items():
            dealer_probs[value] += card_prob * prob
    return dealer_probs


def get_hand_evs(dealer_probs: dict[int, float], counter: Counter):
    soft_states = [(v, True) for v in range(12, 22)]
    hard_states = [(v, False) for v in range(4, 22)]
    states = soft_states + hard_states

    stand_evs = defaultdict(float)

    for value, is_soft in states:
        for dealer_value, prob in dealer_probs.items():
            if value > dealer_value:
                stand_evs[(value, is_soft)] += 2 * prob
            elif value == dealer_value:
                stand_evs[(value, is_soft)] += prob

    hit_evs = {}
    double_evs = {}

    needs_processing = True
    while needs_processing:
        needs_processing = False
        for value, is_soft in states:
            hit_ev = 0
            double_ev = -1
            next_states_ready = True
            for card in range(2, 12):
                card_prob = counter.probability(card)
                if card_prob <= 0:
                    continue

                new_value = value + card
                new_soft = is_soft or card == 11
                if new_value > 21 and new_soft:
                    new_value -= 10
                    new_soft = False

                if new_value > 21:
                    continue

                if (new_value, new_soft) not in hit_evs:
                    needs_processing = True
                    next_states_ready = False
                    break

                next_hit_ev = hit_evs[(new_value, new_soft)]
                next_stand_ev = stand_evs[(new_value, new_soft)]
                hit_ev += card_prob * max(next_hit_ev, next_stand_ev)
                double_ev += 2 * card_prob * next_stand_ev

            if next_states_ready:
                hit_evs[(value, is_soft)] = hit_ev
                double_evs[(value, is_soft)] = double_ev

    return stand_evs, hit_evs, double_evs


def get_move(hand: Hand, dealer_face: int, counter: Counter) -> int:
    assert not hand.is_bust

    dealer_probs = dealer_rollout(dealer_face, counter)
    stand_evs, hit_evs, double_evs = get_hand_evs(dealer_probs, counter)

    stand_ev = stand_evs[(hand.value, hand.is_soft)]
    hit_ev = hit_evs[(hand.value, hand.is_soft)]
    double_ev = double_evs[(hand.value, hand.is_soft)]
    max_ev = max((hit_ev, double_ev, stand_ev))
    if hand.can_double and max_ev == double_ev:
        return Move.DOUBLE
    elif hand.can_hit and (max_ev == hit_ev or hand.value <= 10):
        return Move.HIT
    else:
        return Move.STAND


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Configure blackjack game rules")

    # Number of decks
    parser.add_argument(
        "--decks",
        type=int,
        choices=[1, 2, 4, 5, 6, 8],
        default=6,
        help="Number of decks used in the game (1, 2, 4, 5, 6, or 8)",
    )

    # Dealer hit/stand on soft 17
    parser.add_argument(
        "--dealer-soft-17",
        choices=["hits", "stands"],
        default="stands",
        help="Dealer hits or stands on soft 17",
    )

    # Double after split
    parser.add_argument(
        "--double-after-split", action="store_true", help="Allow doubling after split"
    )

    # Double down restrictions
    parser.add_argument(
        "--double-on",
        choices=["any", "9-11", "10-11"],
        default="any",
        help="Restrictions on when player can double (any first two cards, 9-11 only, or 10-11 only)",
    )

    # Resplit limit
    parser.add_argument(
        "--resplit-limit",
        type=int,
        choices=[2, 3, 4],
        default=3,
        help="Maximum number of hands player can split to (2, 3, or 4)",
    )

    # Resplit aces
    parser.add_argument("--resplit-aces", action="store_true", help="Allow resplitting of aces")

    # Hit split aces
    parser.add_argument("--hit-split-aces", action="store_true", help="Allow hitting split aces")

    # Original bet only against dealer blackjack
    parser.add_argument(
        "--original-bet-only",
        action="store_true",
        help="Player loses only original bet against dealer blackjack",
    )

    # Surrender
    parser.add_argument(
        "--surrender",
        choices=["none", "late", "early"],
        default="none",
        help="Surrender rules (none or late)",
    )

    # Blackjack payout
    parser.add_argument(
        "--blackjack-payout",
        choices=["3:2", "6:5"],
        default="3:2",
        help="Blackjack payout ratio (3:2 or 6:5)",
    )
    args = parser.parse_args()
    print("Game Rules Configuration:")
    print(f"Number of decks: {args.decks}")
    print(f"Dealer on soft 17: {args.dealer_soft_17}")
    print(f"Double after split: {args.double_after_split}")
    print(f"Double on: {args.double_on}")
    print(f"Resplit limit: {args.resplit_limit}")
    print(f"Resplit aces: {args.resplit_aces}")
    print(f"Hit split aces: {args.hit_split_aces}")
    print(f"Original bet only: {args.original_bet_only}")
    print(f"Surrender: {args.surrender}")
    print(f"Blackjack payout: {args.blackjack_payout}")

    blackjack_payout = (
        BlackJackPayout.THREE_TWO if args.blackjack_payout == "3:2" else BlackJackPayout.SIX_FIVE
    )

    if args.double_on == "any":
        double_on = DoubleOn.ANY
    elif args.double_on == "9-11":
        double_on = DoubleOn.NINE_TO_ELEVEN
    elif args.double_on == "10-11":
        double_on = DoubleOn.TEN_TO_ELEVEN

    if args.surrender == "none":
        surrender = Surrender.NONE
    elif args.surrender == "late":
        surrender = Surrender.LATE
    elif args.surrender == "early":
        surrender = Surrender.EARLY

    main(
        2,
        1000,
        args.decks,
        blackjack_payout,
        args.dealer_soft_17 == "hits",
        args.double_after_split,
        double_on,
        args.resplit_limit,
        args.resplit_aces,
        args.hit_split_aces,
        args.original_bet_only,
        surrender,
    )
