from collections import defaultdict
from copy import deepcopy

from models.deck import Deck, DoubleOn, Hand
from models.counter import Counter, NoneCounter, HighLowCounter, PerfectCounter
from timer import LoopTimer
from config import GameConfig


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
    assert counter.total_remaining == len(deck.cards)
    deck.shuffle()
    counter.reset()


def main(bankroll: float, config: GameConfig):
    timer = LoopTimer(1)

    Hand.set_rules(config)
    deck = Deck(config.num_decks)
    deck.shuffle()

    counter = PerfectCounter(config.num_decks)

    num_hands = 0
    running_ev = 0.0

    timer.start()

    while bankroll > 0:
        if deck.must_shuffle:
            reshuffle_deck(deck, counter)
        counter.check()

        current_bankroll = bankroll

        with timer.timing("play_ev"):
            play_ev = get_play_ev(counter, config)

        max_bet_multiple = get_max_bet(config.resplit_limit, config.double_after_split)
        kelly_factor = 1 / max_bet_multiple
        bet = get_kelly_bet(play_ev, current_bankroll, config.min_bet, factor=kelly_factor)

        if bet < config.min_bet:
            if config.always_play:
                bet = config.min_bet
            else:
                for _ in range(6):
                    card = deck.deal_card()
                    counter.count(card)
                continue

        # print(f"Hand {num_hands}, Bankroll: {bankroll}, Play EV: {play_ev}, Bet: {bet}")
        running_ev += play_ev

        num_hands += 1
        num_splits = 0

        bankroll -= bet

        dealer = deck.deal_hand()
        player = deck.deal_hand()

        counter.count(player.cards)
        counter.count(dealer.cards[0])

        dealer_face = dealer.cards[0]

        if config.surrender == Surrender.EARLY:
            with timer.timing("surrender"):
                player_surrender = should_surrender(
                    player, dealer_face, counter, config.resplit_limit, early=False
                )

            if player_surrender:
                bankroll += bet / 2
                counter.count(dealer.cards[1])
                timer.loop()
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
                timer.loop()
                continue

        if config.surrender == Surrender.LATE:
            with timer.timing("surrender"):
                player_surrender = should_surrender(
                    player, dealer_face, counter, config.resplit_limit
                )
            if player_surrender:
                bankroll += bet / 2
                counter.count(dealer.cards[1])
                timer.loop()
                continue

        if player.is_blackjack:
            bankroll += (1 + config.blackjack_payout) * bet
            counter.count(dealer.cards[1])
            timer.loop()
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
                    with timer.timing("get_move", separate_count=True):
                        move = get_move(
                            hand,
                            dealer_face,
                            counter,
                            num_splits=config.resplit_limit - num_splits,
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

        timer.loop()

        # print(f"Bet change per hand: {(bankroll - initial_bankroll) / (min_bet * num_hands):.5f}")
        # print(f"EV avg:              {running_ev / num_hands:.5f}")
    print(f"Played {num_hands} hands")


def get_play_ev(counter: Counter, config: GameConfig):
    final_hand_ev = 0.0
    for dealer_face in range(2, 12):
        face_prob = counter.probability(dealer_face)
        if face_prob == 0:
            continue
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
            if card1_prob == 0:
                continue
            counter.count(card)
            for second_card in range(card, 12):
                hand_prob = card1_prob * counter.probability(second_card)
                if hand_prob == 0:
                    continue
                counter.count(second_card)
                hand = Hand([card, second_card])
                if hand.can_split:
                    split_ev = get_split_ev(
                        hand,
                        stand_evs,
                        hit_evs,
                        double_evs,
                        counter,
                        config.resplit_limit,
                    )
                else:
                    split_ev = float("-inf")
                    hand_prob *= 2  # permutation variants are twice as likely
                if hand.is_blackjack:
                    hand_ev += hand_prob * config.blackjack_payout
                    player_blackjack_prob = hand_prob
                    counter.uncount(second_card)
                    continue
                value = hand.value
                is_soft = hand.is_soft
                stand_ev = stand_evs[(value, is_soft)]
                hit_ev = hit_evs[(value, is_soft)]
                double_ev = double_evs[(value, is_soft)]
                ev = max(stand_ev, hit_ev, double_ev, split_ev)
                if config.surrender == Surrender.EARLY:
                    _ev = ev * (1 - blackjack_prob)
                    _ev -= blackjack_prob
                    if _ev < -0.5:
                        ev = -0.5
                        prob_early_surrender += hand_prob
                        counter.uncount(second_card)
                        continue
                elif config.surrender == Surrender.LATE:
                    ev = max(ev, -0.5)
                hand_ev += hand_prob * ev
                counter.uncount(second_card)
            counter.uncount(card)

        hand_ev *= 1 - blackjack_prob
        hand_ev += blackjack_prob * player_blackjack_prob
        hand_ev -= blackjack_prob * (1 - player_blackjack_prob)

        if config.surrender == Surrender.EARLY:
            hand_ev *= 1 - prob_early_surrender
            hand_ev += prob_early_surrender * -0.5

        final_hand_ev += hand_ev * face_prob
        counter.uncount(dealer_face)

    return final_hand_ev


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
        assert sum(dealer_probs.values()) - 1 < 1e-6, f"{sum(dealer_probs.values())}"
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


def dealer_rollout_exact(dealer_hand: Hand, counter: Counter) -> dict[int, float]:
    if dealer_hand.must_hit:
        dealer_probs = defaultdict(float)
        for card in range(2, 12):
            card_prob = counter.probability(card)
            if card_prob == 0:
                continue
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
    split_limit: int,
) -> float:
    assert hand.can_split
    split_card = hand.cards[0]

    split_ev = 0.0
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
        else:
            split_ev += 2 * card_prob * ev
        split_hand.remove(card)

    assert split_card_ev is not None
    resplit_prob = counter.probability(split_card)
    terminal_split_ev = split_ev + 2 * resplit_prob * split_card_ev

    if (
        split_limit > 1
        and (Hand.resplit_aces or split_card != 11)
        and terminal_split_ev > split_card_ev
    ):
        # if multiple splits are allowed and splitting is desirable,
        # the first split's EV should be higher than the second split's EV
        # this won't affect later splitting decisions since it will only
        # affect the play EV since we only increase the split ev if it
        # is higher than the non-split EV anyway
        num_splits = split_limit
        split_level = 1
        while num_splits > split_level:
            num_splits -= split_level
            split_level *= 2
        top_level_remaining = split_level - num_splits
        split_values = [terminal_split_ev] * num_splits
        split_values += [split_card_ev] * top_level_remaining
        while len(split_values) > 1:
            new_split_values = []
            for i in range(0, len(split_values), 2):
                new_split_values.append(
                    split_ev + resplit_prob * (split_values[i] + split_values[i + 1])
                )
            split_values = new_split_values
        split_ev = split_values[0]
        assert split_ev >= terminal_split_ev, f"{split_ev: .4f}, {terminal_split_ev: .4f}"
    else:
        split_ev = terminal_split_ev

    return split_ev


def get_kelly_bet(hand_ev: float, bankroll: float, min_bet: int, factor: float = 1) -> int:
    p = (hand_ev + 1) / 2
    ratio = p - ((1 - p) / 1)  # ignoring blackjack payout and other things like that``
    max_bet = bankroll * ratio
    max_bet = int(min(max_bet, bankroll * factor))
    bet = max_bet - max_bet % min_bet
    return max(bet, 0)


def get_max_bet(resplit_limit: int, double_after_split: bool) -> int:
    max_bet_multiple = 1 + resplit_limit
    max_bet_multiple *= 2 if double_after_split else 1
    max_bet_multiple = max(max_bet_multiple, 2)
    return max_bet_multiple


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


if __name__ == "__main__":
    config = GameConfig(
        min_bet=2,
        num_decks=3,
        dealer_hits_soft_17=False,
        double_after_split=True,
        double_on=DoubleOn.ANY,
        resplit_limit=3,
        resplit_aces=True,
        hit_split_aces=True,
        surrender=Surrender.EARLY,
        blackjack_payout=BlackJackPayout.THREE_TWO,
        always_play=True,
    )

    main(1000, config)
