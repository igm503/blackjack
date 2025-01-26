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

    while bankroll > 0:
        if deck.must_shuffle:
            deck.shuffle()
            print("Shuffling")

        print(f"\n\nBankroll: {bankroll}")
        bet = min_bet
        bankroll -= bet

        dealer = deck.deal_hand()
        player = deck.deal_hand()

        print("Player:", player)
        print("Dealer:", dealer)

        dealer_face = dealer.cards[0]

        if dealer_face in [10, 11]:
            if dealer_face == 11:
                # do insurance
                pass
            if dealer.is_blackjack:
                if player.is_blackjack:
                    print("Dealer Blackjack, Push")
                    bankroll += bet
                else:
                    print("Dealer Blackjack, Player Loses")
                continue

        if player.is_blackjack:
            print("Player Blackjack, Player Wins")
            assert int((1 + blackjack_payout) * bet) == (1 + blackjack_payout) * bet
            bankroll += int((1 + blackjack_payout) * bet)
            continue

        while should_hit(player, dealer_face, counter):
            player.add(deck.deal_card())
            print("Player:", player)

        if player.is_bust:
            print("Player Bust, Player Loses")
            continue

        while dealer.must_hit:
            dealer.add(deck.deal_card())

        print("Player:", player)
        print("Dealer:", dealer)

        if dealer.is_bust or player.value > dealer.value:
            print("Player Wins")
            bankroll += 2 * bet
        elif player.value < dealer.value:
            print("Player Loses")
        else:
            print("Push")
            bankroll += bet


def dealer_rollout(dealer_hand: Hand, counter: Counter) -> dict[int, float]:
    if dealer_hand.must_hit:
        dealer_probs = defaultdict(float)
        for card in range(2, 11):
            card_prob = counter.probability(card)
            temp_counter = deepcopy(counter)
            temp_counter.count(card)
            temp_dealer_hand = deepcopy(dealer_hand)
            temp_dealer_hand.add(card)
            probs = dealer_rollout(temp_dealer_hand, temp_counter)
            for value, prob in probs.items():
                dealer_probs[value] += card_prob * prob
        return dealer_probs
    else:
        value = dealer_hand.value
        value = 0 if dealer_hand.is_bust else value
        return {value: 1.0}


def hit_probs(counter: Counter) -> dict[int, float]:
    probs = {card: counter.probability(card) for card in range(2, 12)}
    assert sum(probs.values()) == 1.0
    return probs


def should_hit(hand: Hand, dealer_face: int, counter: Counter):
    if hand.value <= 10:
        return True
    if hand.is_bust:
        return False
    dealer_probs = dealer_rollout(Hand([dealer_face]), counter)
    win_prob = sum([prob for value, prob in dealer_probs.items() if hand.value > value])
    push_prob = dealer_probs[hand.value]
    no_hit_ev = 2 * win_prob + push_prob

    hit_card_probs = hit_probs(counter)
    win_prob = 0.0
    push_prob = 0.0
    for card, card_prob in hit_card_probs.items():
        temp_hand = deepcopy(hand)
        temp_hand.add(card)
        if temp_hand.is_bust:
            continue
        temp_counter = deepcopy(counter)
        temp_counter.count(card)
        dealer_probs = dealer_rollout(Hand([dealer_face]), temp_counter)
        win_prob += card_prob * sum(
            [prob for value, prob in dealer_probs.items() if temp_hand.value > value]
        )
        push_prob += card_prob * dealer_probs[card + hand.value]

    hit_ev = 2 * win_prob + push_prob
    # print(f"Hit EV: {hit_ev:.3f}, No Hit EV: {no_hit_ev:.3f}")
    return hit_ev > no_hit_ev


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
        10,
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
