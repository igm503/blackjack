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


def reshuffle_deck(deck: Deck, counter: Counter):
    print("Shuffling")
    assert counter.total_remaining == len(deck.cards)
    deck.shuffle()
    counter.reset()


def main(
    min_bet: int,
    bankroll: float,
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
    running_ev = 0.0

    while bankroll > 0:
        if deck.must_shuffle:
            reshuffle_deck(deck, counter)

        current_bankroll = bankroll
        hand_ev = get_hand_ev(counter, resplit_limit, blackjack_payout, surrender)
        max_bet_multiple = 1 + resplit_limit
        max_bet_multiple *= 2 if double_after_split else 1
        max_bet_multiple = max(max_bet_multiple, 2)
        bet = get_kelly_bet(hand_ev, current_bankroll, min_bet, factor=(1 / max_bet_multiple))
        if bet == 0:
            for i in range(6):
                card = deck.deal_card()
                assert card is not None
                counter.count(card)
            continue
        print(f"Hand {num_hands}, Bankroll: {bankroll}, Hand EV: {hand_ev}, Bet: {bet}")
        running_ev += hand_ev

        num_hands += 1
        num_splits = 0

        bankroll -= bet

        dealer = deck.deal_hand()
        player = deck.deal_hand()

        counter.count(player.cards)
        counter.count(dealer.cards[0])

        dealer_face = dealer.cards[0]

        if surrender == Surrender.EARLY:
            player_surrender = should_surrender(
                player, dealer_face, counter, resplit_limit, early=False
            )
            if player_surrender:
                bankroll += bet / 2
                counter.count(dealer.cards[1])
                continue

        if dealer_face in [10, 11]:
            if dealer_face == 11:
                # do insurance
                pass
            if dealer.is_blackjack:
                if player.is_blackjack:
                    bankroll += bet
                else:
                    pass
                counter.count(dealer.cards[1])
                continue

        if surrender == Surrender.LATE:
            player_surrender = should_surrender(player, dealer_face, counter, resplit_limit)
            if player_surrender:
                bankroll += bet / 2
                counter.count(dealer.cards[1])
                continue

        if player.is_blackjack:
            bankroll += int((1 + blackjack_payout) * bet)
            counter.count(dealer.cards[1])
            continue

        finished_hands = []
        current_hands = [player]
        while True:
            if not current_hands:
                break
            for hand in current_hands[::-1]:
                if hand.is_bust:
                    current_hands.remove(hand)
                    continue
                while not hand.is_bust:
                    move = get_move(
                        hand,
                        dealer_face,
                        counter,
                        num_splits=resplit_limit - num_splits,
                    )
                    if move == Move.HIT:
                        new_card = deck.deal_card()
                        if new_card is None:
                            reshuffle_deck(deck, counter)
                            new_card = deck.deal_card()
                        assert new_card is not None
                        hand.add(new_card)
                        counter.count(new_card)
                    elif move == Move.DOUBLE:
                        new_card = deck.deal_card()
                        if new_card is None:
                            reshuffle_deck(deck, counter)
                            new_card = deck.deal_card()
                        assert new_card is not None
                        hand.double(new_card)
                        counter.count(new_card)
                        bankroll -= bet
                        finished_hands.append(hand)
                        current_hands.remove(hand)
                        break
                    elif move == Move.SPLIT:
                        new_card_1 = deck.deal_card()
                        new_card_2 = deck.deal_card()
                        if new_card_1 is None or new_card_2 is None:
                            reshuffle_deck(deck, counter)
                            new_card_1 = deck.deal_card()
                            new_card_2 = deck.deal_card()
                        assert new_card_1 is not None and new_card_2 is not None
                        new_hands = hand.split(new_card_1, new_card_2)
                        counter.count(new_card_1)
                        counter.count(new_card_2)
                        bankroll -= bet
                        current_hands.remove(hand)
                        current_hands.extend(new_hands)
                        num_splits += 1
                        break
                    else:
                        finished_hands.append(hand)
                        current_hands.remove(hand)
                        break

        counter.count(dealer.cards[1])
        all_bust = all(hand.is_bust for hand in finished_hands)

        if all_bust:
            continue

        while dealer.must_hit:
            new_card = deck.deal_card()
            if new_card is None:
                reshuffle_deck(deck, counter)
                new_card = deck.deal_card()
            assert new_card is not None
            dealer.add(new_card)
            counter.count(new_card)

        for hand in finished_hands:
            assert not hand.is_bust

            if dealer.is_bust or hand.value > dealer.value:
                bankroll += 2 * bet
                if hand.is_double:
                    bankroll += 2 * bet
            elif hand.value == dealer.value:
                bankroll += bet
                if player.is_double:
                    bankroll += bet

        # print(f"Bet change per hand: {(bankroll - initial_bankroll) / (min_bet * num_hands):.5f}")
        # print(f"EV avg:              {running_ev / num_hands:.5f}")
    print(f"Played {num_hands} hands")


def get_hand_ev(counter: Counter, max_splits: int, blackjack_payout: float, surrender: int):
    final_hand_ev = 0.0
    for dealer_face in range(2, 12):
        face_prob = counter.probability(dealer_face)
        counter.count(dealer_face)
        dealer_probs = dealer_rollout(dealer_face, counter, no_blackjack=False)

        blackjack_prob = dealer_probs["blackjack"]
        for value in dealer_probs:
            dealer_probs[value] /= 1 - blackjack_prob
        del dealer_probs["blackjack"]

        hand_ev = 0.0
        prob_early_surrender = 0.0
        player_blackjack_prob = 0.0
        stand_evs, hit_evs, double_evs = get_hand_evs(dealer_probs, counter)
        for card in range(2, 12):
            card1_prob = counter.probability(card)
            counter.count(card)
            for second_card in range(card, 12):
                hand_prob = card1_prob * counter.probability(second_card)
                counter.count(second_card)
                hand = Hand([card, second_card])
                if hand.can_split:
                    split_ev = get_split_ev(
                        hand, stand_evs, hit_evs, double_evs, counter, max_splits
                    )
                else:
                    split_ev = float("-inf")
                    hand_prob *= 2  # permutation variants are twice as likely
                if hand.is_blackjack:
                    hand_ev += hand_prob * blackjack_payout
                    player_blackjack_prob = hand_ev
                    counter.uncount(second_card)
                    continue
                value = hand.value
                is_soft = hand.is_soft
                stand_ev = stand_evs[(value, is_soft)]
                hit_ev = hit_evs[(value, is_soft)]
                double_ev = double_evs[(value, is_soft)]
                ev = max(stand_ev, hit_ev, double_ev, split_ev)
                if surrender == Surrender.EARLY:
                    _ev = ev * (1 - blackjack_prob)
                    _ev -= blackjack_prob
                    if _ev < -0.5:
                        ev = -0.5
                        prob_early_surrender += hand_prob
                        counter.uncount(second_card)
                        continue
                elif surrender == Surrender.LATE:
                    ev = max(ev, -0.5)
                hand_ev += hand_prob * ev
                counter.uncount(second_card)
            counter.uncount(card)

        hand_ev *= 1 - blackjack_prob
        hand_ev += blackjack_prob * player_blackjack_prob
        hand_ev -= blackjack_prob * (1 - player_blackjack_prob)

        if surrender == Surrender.EARLY:
            hand_ev *= 1 - prob_early_surrender
            hand_ev += prob_early_surrender * -0.5

        final_hand_ev += hand_ev * face_prob
        counter.uncount(dealer_face)

    return final_hand_ev


def should_surrender(
    player: Hand,
    dealer_face: int,
    counter: Counter,
    resplit_limit: int,
    early: bool = False,
):
    if early:
        dealer_probs = dealer_rollout(dealer_face, counter, no_blackjack=False)
        blackjack_prob = dealer_probs["blackjack"]
        del dealer_probs["blackjack"]
        for value in dealer_probs:
            dealer_probs[value] /= 1 - blackjack_prob
    else:
        dealer_probs = dealer_rollout(dealer_face, counter)
        blackjack_prob = 0.0

    stand_evs, hit_evs, double_evs = get_hand_evs(dealer_probs, counter)
    stand_ev = stand_evs[(player.value, player.is_soft)]
    hit_ev = hit_evs[(player.value, player.is_soft)]
    double_ev = double_evs[(player.value, player.is_soft)]
    if player.can_split and resplit_limit > 0:
        split_ev = get_split_ev(player, stand_evs, hit_evs, double_evs, counter, resplit_limit)
    else:
        split_ev = float("-inf")
    player_ev = max(stand_ev, hit_ev, double_ev, split_ev)

    if early:
        if player.is_blackjack:
            return False
        else:
            player_ev *= 1 - blackjack_prob
            player_ev -= blackjack_prob
    return player_ev < -0.5


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
) -> dict[int | str, float]:
    dealer_probs = defaultdict(float)
    for card in range(2, 12):
        hand = Hand([dealer_face, card])
        if hand.is_blackjack:
            if not no_blackjack:
                card_prob = counter.probability(card)
                dealer_probs["blackjack"] += card_prob
            continue

        card_prob = counter.probability(card)
        probs = dealer_rollout_approximate(hand, counter)
        for value, prob in probs.items():
            dealer_probs[value] += card_prob * prob

    if not no_blackjack or dealer_face not in [10, 11]:
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
    assert hand.can_split
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
    if hand.can_double and max_ev == double_ev:
        return Move.DOUBLE
    elif hand.can_hit and max_ev == hit_ev:
        return Move.HIT
    elif hand.can_split and max_ev == split_ev:
        return Move.SPLIT
    else:
        return Move.STAND


def get_kelly_bet(hand_ev: float, bankroll: float, min_bet: int, factor: float = 1) -> int:
    p = (hand_ev + 1) / 2
    ratio = p - ((1 - p) / 1)  # ignoring blackjack payout and other things like that``
    max_bet = int(bankroll * ratio)
    bet = max_bet - max_bet % min_bet
    return max(bet, 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Configure blackjack game rules")

    config = {
        "decks": 3,
        "dealer_hits_soft_17": False,
        "double_after_split": True,
        "double_on": DoubleOn.ANY,
        "resplit_limit": 3,
        "resplit_aces": True,
        "hit_split_aces": True,
        "original_bet_only": True,
        "surrender": Surrender.EARLY,
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
