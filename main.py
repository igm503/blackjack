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
    initial_bankroll = bankroll

    while bankroll > 0:
        if deck.must_shuffle:
            counter = PerfectCounter(num_decks)
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

        finished_hands = []
        current_hands = [player]
        while True:
            if not current_hands:
                break
            for hand in current_hands[::-1]:
                while not hand.is_bust:
                    move = get_move(hand, dealer_face, counter)
                    if move == Move.HIT:
                        new_card = deck.deal_card()
                        hand.add(new_card)
                        counter.count(new_card)
                    elif move == Move.DOUBLE:
                        new_card = deck.deal_card()
                        hand.double(new_card)
                        counter.count(new_card)
                        bankroll -= min_bet
                        finished_hands.append(hand)
                        current_hands.remove(hand)
                        break
                    elif move == Move.SPLIT:
                        new_card_1 = deck.deal_card()
                        new_card_2 = deck.deal_card()
                        new_hands = hand.split(new_card_1, new_card_2)
                        bankroll -= min_bet
                        current_hands.remove(hand)
                        current_hands.extend(new_hands)
                        break
                    else:
                        finished_hands.append(hand)
                        current_hands.remove(hand)
                        break
                if hand.is_bust:
                    current_hands.remove(hand)

            # print("Player:", player)

        counter.count(dealer.cards[1])
        all_bust = all(hand.is_bust for hand in finished_hands)

        if all_bust:
            continue

        while dealer.must_hit:
            new_card = deck.deal_card()
            dealer.add(new_card)
            counter.count(new_card)

        for hand in finished_hands:
            if hand.is_bust:
                # print("Player Bust, Player Loses")
                continue

            # print("Player:", player)
            # print("Dealer:", dealer)

            if dealer.is_bust or hand.value > dealer.value:
                # print("Player Wins")
                bankroll += 2 * min_bet
                if hand.is_double:
                    bankroll += 2 * min_bet
            elif hand.value == dealer.value:
                # print("Push")
                bankroll += min_bet
                if player.is_double:
                    bankroll += min_bet

    print(f"Played {num_hands} hands")
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
    if dealer_face not in [10, 11]:
        assert sum(dealer_probs.values()) - 1 < 1e-6
    else:
        if dealer_face == 11:
            missing_prob = counter.probability(10)
        else:
            missing_prob = counter.probability(11)
        norm_factor = 1 / (1 - missing_prob)
        for value, prob in dealer_probs.items():
            dealer_probs[value] = norm_factor * prob
        assert sum(dealer_probs.values()) - 1 < 1e-6
    return dealer_probs


def get_hand_evs(dealer_probs: dict[int, float], counter: Counter):
    soft_states = [(v, True) for v in range(12, 22)]
    hard_states = [(v, False) for v in range(4, 22)]
    states = soft_states + hard_states

    stand_evs = defaultdict(float)

    for value, is_soft in states:
        for dealer_value, prob in dealer_probs.items():
            if value > dealer_value:
                stand_evs[(value, is_soft)] += prob
            elif value < dealer_value:
                stand_evs[(value, is_soft)] -= prob

    hit_evs = {}
    double_evs = {}

    needs_processing = True
    while needs_processing:
        needs_processing = False
        for value, is_soft in states:
            hit_ev = 0
            double_ev = 0
            next_states_ready = True
            for card in range(2, 12):
                card_prob = counter.probability(card)

                new_value = value + card
                new_soft = is_soft or card == 11
                if new_value > 21 and new_soft:
                    new_value -= 10
                    new_soft = False

                if new_value > 21:
                    hit_ev -= card_prob
                    double_ev -= 2 * card_prob
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


def get_split_ev(
    hand: Hand,
    stand_ev: dict[tuple[int, bool], float],
    hit_ev: dict[tuple[int, bool], float],
    double_ev: dict[tuple[int, bool], float],
    counter: Counter,
    num_splits: int,
) -> float:
    split_card = hand.cards[0]

    terminal_split_ev = 0.0
    split_hand = Hand([split_card])
    split_card_ev = None
    for card in range(2, 12):
        card_prob = counter.probability(card)
        split_hand.add(card)
        value = split_hand.value
        is_soft = split_hand.is_soft
        evs = [stand_ev[(value, is_soft)]]
        if hand.hit_split_aces or split_card != 11:
            evs.append(hit_ev[(value, is_soft)])
            if hand.double_after_split:
                evs.append(double_ev[(value, is_soft)])
        ev = max(evs)
        if card == split_card:
            split_card_ev = ev
        terminal_split_ev += 2 * card_prob * ev
        split_hand.remove(card)

    split_ev = terminal_split_ev
    if num_splits > 1 and (hand.resplit_aces or split_card != 11):
        num_splits -= 1
        resplit_prob = counter.probability(split_card)
        assert split_card_ev is not None
        if split_ev > split_card_ev:
            split_ev -= (2 - num_splits) * resplit_prob * split_card_ev
            split_ev += (num_splits) * resplit_prob * split_ev

    return split_ev


def get_move(hand: Hand, dealer_face: int, counter: Counter, num_splits: int = 3) -> int:
    assert not hand.is_bust

    dealer_probs = dealer_rollout(dealer_face, counter)
    stand_evs, hit_evs, double_evs = get_hand_evs(dealer_probs, counter)
    stand_ev = stand_evs[(hand.value, hand.is_soft)]
    hit_ev = hit_evs[(hand.value, hand.is_soft)]
    double_ev = double_evs[(hand.value, hand.is_soft)]
    split_ev = float("-inf")
    if hand.can_split and num_splits > 0:
        split_ev = get_split_ev(hand, stand_evs, hit_evs, double_evs, counter, num_splits)
    max_ev = max((stand_ev, hit_ev, double_ev, split_ev))
    # print(
    #     f"{hand}: stand: {stand_ev:.2f} hit: {hit_ev:.2f} double: {double_ev:.2f} split: {split_ev:.2f}"
    # )
    if hand.can_double and max_ev == double_ev:
        return Move.DOUBLE
    elif hand.can_hit and max_ev == hit_ev:
        return Move.HIT
    elif hand.can_split and max_ev == split_ev:
        return Move.SPLIT
    else:
        return Move.STAND


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Configure blackjack game rules")

    config = {
        "decks": 8,
        "dealer_hits_soft_17": True,
        "double_after_split": True,
        "double_on": DoubleOn.ANY,
        "resplit_limit": 4,
        "resplit_aces": True,
        "hit_split_aces": True,
        "original_bet_only": False,
        "surrender": "none",
        "blackjack_payout": BlackJackPayout.THREE_TWO,
    }

    main(
        2,
        1000,
        config["decks"],
        config["blackjack_payout"],
        config["dealer_hits_soft_17"],
        config["double_after_split"],
        config["double_on"],
        config["resplit_limit"],
        config["resplit_aces"],
        config["hit_split_aces"],
        config["original_bet_only"],
        config["surrender"],
    )
