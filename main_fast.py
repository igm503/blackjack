from collections import defaultdict
from copy import deepcopy

from models.deck import Deck, DoubleOn, Hand
from models.counter import Counter, NoneCounter, HighLowCounter, PerfectCounter
from models.ev import HandEVs, ExpectedValues, DealerProbsTable, Move
from config import GameConfig
from timer import LoopTimer


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

        current_bankroll = bankroll

        with timer.timing("dealer_probs", separate_count=True):
            dealer_prob_table = get_dealer_prob_table(counter)

        with timer.timing("hand_ev_table", separate_count=True):
            hand_ev_table = get_hand_ev_table(dealer_prob_table, counter, config)

        with timer.timing("play_ev", separate_count=True):
            play_ev = get_play_ev(hand_ev_table, counter, config)

        max_bet_multiple = get_max_bet(config.resplit_limit, config.double_after_split)
        kelly_factor = 1 / max_bet_multiple
        bet = get_kelly_bet(play_ev, current_bankroll, config.min_bet, factor=kelly_factor)

        if bet < config.min_bet:
            if config.always_play:
                bet = config.min_bet
            else:
                for _ in range(6):
                    if not deck.can_deal():
                        reshuffle_deck(deck, counter)
                    card = deck.deal_card()
                    counter.count(card)
                continue

        print(f"Hand {num_hands}, Bankroll: {bankroll}, Play EV: {play_ev}, Bet: {bet}")
        running_ev += play_ev

        num_hands += 1
        bankroll -= bet

        num_splits = 0

        dealer = deck.deal_hand()
        player = deck.deal_hand()

        dealer_face = dealer.cards[0]

        counter.count(player.cards)
        counter.count(dealer_face)

        hand_evs = hand_ev_table[dealer_face]

        if config.surrender == Surrender.EARLY:
            with timer.timing("surrender"):
                player_surrender = should_surrender(player, hand_evs, dealer_face, counter, config)

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
                player_surrender = should_surrender(player, hand_evs, dealer_face, counter, config)
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
                        move = get_move(hand, hand_evs, config.resplit_limit - num_splits)
                    if not deck.can_deal():
                        reshuffle_deck(deck, counter)
                    if move == Move.HIT:
                        new_card = deck.deal_card()
                        hand.add(new_card)
                        counter.count(new_card)
                    elif move == Move.DOUBLE:
                        new_card = deck.deal_card()
                        hand.double(new_card)
                        counter.count(new_card)
                        bankroll -= bet
                        finished_hands.append(hand)
                        current_hands.remove(hand)
                        break
                    elif move == Move.SPLIT:
                        new_card_1 = deck.deal_card()
                        new_card_2 = deck.deal_card()
                        new_hands = hand.split(new_card_1, new_card_2)
                        counter.count(new_card_1)
                        counter.count(new_card_2)
                        bankroll -= bet
                        current_hands.remove(hand)
                        current_hands.extend(new_hands)
                        num_splits += 1
                        break
                    elif move == Move.STAND:
                        finished_hands.append(hand)
                        current_hands.remove(hand)
                        break
                    else:
                        raise ValueError(f"Invalid move: {move}")

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


def get_dealer_prob_table(counter: Counter) -> DealerProbsTable:
    hard_states = [(v, False) for v in range(2, 22)]
    soft_states = [(v, True) for v in range(11, 22)]
    states = hard_states + soft_states
    probs = DealerProbsTable()

    for value, soft in states:
        if value >= 17 and value <= 21:
            probs.set(value, soft, {value: 1.0})

    if Hand.hit_soft_17:
        probs.delete(17, True)

    need_processing = True
    while need_processing:
        need_processing = False

        for value, soft in states:
            if probs.contains(value, soft) or value > 21:
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

                if new_value <= 21 and not probs.contains(new_value, new_soft):
                    next_states_ready = False
                    break

                if new_value > 21:
                    dealer_probs[0] += card_prob
                    continue

                next_probs = probs.get(new_value, new_soft)
                for val, prob in next_probs.items():
                    dealer_probs[val] += card_prob * prob

            if next_states_ready:
                probs.set(value, soft, dict(dealer_probs))
            else:
                need_processing = True

    # fix for blackjack
    probs_10 = probs.get(10, False)
    blackjack_prob = counter.probability(11)
    probs_10[21] -= blackjack_prob
    for final_value in probs_10:
        probs_10[final_value] /= 1 - blackjack_prob

    probs_11 = probs.get(11, True)
    blackjack_prob = counter.probability(10)
    probs_11[21] -= blackjack_prob
    for final_value in probs_11:
        probs_11[final_value] /= 1 - blackjack_prob

    return probs


def get_hand_ev_table(
    dealer_prob_table: DealerProbsTable, counter: Counter, config: GameConfig
) -> dict[int, HandEVs]:
    all_hand_evs = {}
    for dealer_face in range(2, 12):
        dealer_probs = dealer_prob_table.get_probs(dealer_face)
        hand_evs = get_hand_evs(dealer_probs, counter, config.resplit_limit)
        all_hand_evs[dealer_face] = hand_evs
    return all_hand_evs


def get_play_ev(hand_ev_table: dict[int, HandEVs], counter: Counter, config: GameConfig):
    final_hand_ev = 0.0
    for dealer_face in range(2, 12):
        if dealer_face not in [10, 11]:
            blackjack_prob = 0.0
        elif dealer_face == 11:
            blackjack_prob = counter.probability(10)
        else:
            blackjack_prob = counter.probability(11)

        hand_ev = 0.0
        prob_early_surrender = 0.0
        player_blackjack_prob = 0.0
        hand_evs = hand_ev_table[dealer_face]
        for card in range(2, 12):
            card1_prob = counter.probability(card)
            for second_card in range(card, 12):
                hand_prob = card1_prob * counter.probability(second_card)
                can_split = card == second_card
                if not can_split:
                    hand_prob *= 2  # permutation variants are twice as likely
                if card + second_card == 21:
                    hand_ev += hand_prob * config.blackjack_payout
                    player_blackjack_prob = hand_prob
                    continue
                value = card + second_card
                if value == 22:
                    value = 12
                is_soft = card == 11 or second_card == 11
                ev = hand_evs.get_max_ev(value, is_soft, can_split)
                if config.surrender == Surrender.EARLY:
                    _ev = ev * (1 - blackjack_prob)
                    _ev -= blackjack_prob
                    if _ev < -0.5:
                        prob_early_surrender += hand_prob
                        continue
                elif config.surrender == Surrender.LATE:
                    ev = max(ev, -0.5)
                hand_ev += hand_prob * ev

        hand_ev *= 1 - blackjack_prob
        hand_ev += blackjack_prob * player_blackjack_prob
        hand_ev -= blackjack_prob * (1 - player_blackjack_prob)

        if config.surrender == Surrender.EARLY:
            hand_ev *= 1 - prob_early_surrender
            hand_ev += prob_early_surrender * -0.5

        final_hand_ev += hand_ev * counter.probability(dealer_face)

    return final_hand_ev


def should_surrender(
    player: Hand,
    hand_evs: HandEVs,
    dealer_face: int,
    counter: Counter,
    config: GameConfig,
):
    blackjack_prob = 0.0
    if config.surrender == Surrender.EARLY:
        if dealer_face == 11:
            blackjack_prob = counter.probability(10)
        elif dealer_face == 10:
            blackjack_prob = counter.probability(11)

    can_split = player.can_split and config.resplit_limit > 0
    player_ev = hand_evs.get_max_ev(player.value, player.is_soft, can_split)

    if config.surrender == Surrender.EARLY:
        player_ev *= 1 - blackjack_prob
        if not player.is_blackjack:
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


def get_hand_evs(dealer_probs: dict[int, float], counter: Counter, resplit_limit: int) -> HandEVs:
    soft_states = [(v, True) for v in range(11, 22)]
    hard_states = [(v, False) for v in range(2, 22)]
    states = soft_states + hard_states

    stand_evs = ExpectedValues()

    for value, is_soft in states:
        for dealer_value, prob in dealer_probs.items():
            if value > dealer_value:
                stand_evs.add(value, is_soft, prob)
            elif value < dealer_value:
                stand_evs.subtract(value, is_soft, prob)

    hit_evs = ExpectedValues()
    double_evs = ExpectedValues()
    split_evs = ExpectedValues()

    needs_processing = True
    while needs_processing:
        needs_processing = False
        for value, is_soft in states:
            can_split = value < 11 or value == 11 and is_soft
            can_resplit = can_split and (Hand.resplit_aces or value != 11) and resplit_limit > 1
            can_split_hit = can_split and (Hand.hit_split_aces or value != 11)
            can_split_double = can_split and (Hand.double_after_split or value != 11)
            hit_ev = 0
            double_ev = 0
            split_ev = 0
            split_card_ev = None
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

                if not hit_evs.contains(new_value, new_soft):
                    needs_processing = True
                    next_states_ready = False
                    break

                next_hit_ev = hit_evs.get(new_value, new_soft)
                next_stand_ev = stand_evs.get(new_value, new_soft)
                hit_ev += card_prob * max(next_hit_ev, next_stand_ev)
                double_ev += 2 * card_prob * next_stand_ev
                if can_split:
                    if not can_split_hit:
                        max_ev = next_stand_ev
                    elif not can_split_double:
                        max_ev = max(next_hit_ev, next_stand_ev)
                    else:
                        max_ev = max(
                            next_hit_ev,
                            next_stand_ev,
                            double_evs.get(new_value, new_soft),
                        )
                    if card == value:
                        split_card_ev = max_ev
                    else:
                        split_ev += 2 * card_prob * max_ev

            if next_states_ready:
                if can_split:
                    assert split_card_ev is not None
                    resplit_prob = counter.probability(value)
                    terminal_split_ev = split_ev + 2 * resplit_prob * split_card_ev
                    if can_resplit and terminal_split_ev > split_card_ev:
                        num_splits = resplit_limit
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
                                    split_ev
                                    + resplit_prob * (split_values[i] + split_values[i + 1])
                                )
                            split_values = new_split_values
                        split_ev = split_values[0]
                        assert split_ev >= terminal_split_ev
                    else:
                        split_ev += 2 * counter.probability(value) * split_card_ev
                else:
                    split_ev = float("-inf")
                hit_evs.set(value, is_soft, hit_ev)
                double_evs.set(value, is_soft, double_ev)
                if can_split:
                    if value == 11:
                        pair_value = 12 
                    else:
                        pair_value = 2 * value
                    split_evs.set(pair_value, is_soft, split_ev)

    return HandEVs(stand_evs, hit_evs, double_evs, split_evs)


def get_split_ev(split_card: int, hand_evs: HandEVs, counter: Counter, num_splits: int) -> float:
    terminal_split_ev = 0.0
    split_hand = Hand([split_card])
    split_card_ev = None
    for card in range(2, 12):
        card_prob = counter.probability(card)
        split_hand.add(card)
        value = split_hand.value
        is_soft = split_hand.is_soft
        evs = [hand_evs.stand.get(value, is_soft)]
        if Hand.hit_split_aces or split_card != 11:
            evs.append(hand_evs.hit.get(value, is_soft))
            if Hand.double_after_split:
                evs.append(hand_evs.double.get(value, is_soft))
        ev = max(evs)
        if card == split_card:
            split_card_ev = ev
        terminal_split_ev += 2 * card_prob * ev
        split_hand.remove(card)

    split_ev = terminal_split_ev
    if num_splits > 1 and (Hand.resplit_aces or split_card != 11):
        num_splits -= 1
        resplit_prob = counter.probability(split_card)
        assert split_card_ev is not None
        if split_ev > split_card_ev:
            split_ev -= (2 - num_splits) * resplit_prob * split_card_ev
            split_ev += (num_splits) * resplit_prob * split_ev

    return split_ev


def get_move(hand: Hand, hand_evs: HandEVs, splits_remaining: int = 3) -> int:
    assert not hand.is_bust

    can_split = hand.can_split and splits_remaining > 0
    move_ranking = hand_evs.get_move_ranking(hand.value, hand.is_soft, can_split=can_split)
    for move in move_ranking:
        if move == Move.HIT and hand.can_hit:
            return move
        elif move == Move.DOUBLE and hand.can_double:
            return move
        elif move == Move.SPLIT and can_split:
            return move
        elif move == Move.STAND:
            return move

    raise Exception("No move found")


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
