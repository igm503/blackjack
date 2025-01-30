from collections import defaultdict

from tqdm import tqdm

from main import dealer_rollout, get_hand_evs, get_move, Move
from deck import Deck, Hand
from counter import NoneCounter


def get_hand(first_card, deck, no_splits=False):
    deck.shuffle()
    second_card = deck.deal_card()
    if first_card == 11:
        while second_card == 10:
            second_card = deck.deal_card()  # already peaked
    elif first_card == 10:
        while second_card == 11:
            second_card = deck.deal_card()
    if no_splits:
        while second_card == first_card:
            second_card = deck.deal_card()
    hand = Hand([first_card, second_card])
    return hand


def dealer_action(hand, deck):
    while hand.must_hit:
        hit_card = deck.deal_card()
        hand.add(hit_card)


def test_dealer_rollout(verbose=False):
    num_samples = 10_000
    abs_err = 0.02

    num_decks = 8

    deck = Deck(num_decks)

    errors = defaultdict(dict)

    print(f"testing dealer rollout with hit on soft 17 set to {Hand.hit_soft_17}")

    for dealer_face in tqdm(range(2, 12)):
        sample_outcomes = defaultdict(float)

        for _ in range(num_samples):
            dealer = get_hand(dealer_face, deck)
            dealer_action(dealer, deck)
            value = dealer.value if not dealer.is_bust else 0
            sample_outcomes[value] += 1

        for value, count in sample_outcomes.items():
            sample_outcomes[value] = count / num_samples

        estimated_outcomes = dealer_rollout(dealer_face, NoneCounter(8))
        for value, prob in estimated_outcomes.items():
            error = abs(sample_outcomes[value] - prob)
            if error > abs_err:
                print(f"{dealer_face}: {sample_outcomes}")
                print(f"{dealer_face}: {estimated_outcomes}")
                raise Exception
            errors[dealer_face][value] = error

    if verbose:
        final_value_string = "  "
        for dealer_face in [0, 17, 18, 19, 20, 21]:
            final_value_string += f"|  {dealer_face} "
        print(final_value_string)

        for dealer_face in range(2, 12):
            string = f"{dealer_face}: "
            for final_value in [0, 17, 18, 19, 20, 21]:
                string += f"{errors[dealer_face].get(final_value, 0):.3f} "
            print(string)


def test_stand_evs(verbose=False):
    num_samples = 100_000
    abs_err = 0.03

    num_decks = 8

    deck = Deck(num_decks)

    errors = defaultdict(dict)

    print(f"testing stand evs with hit on soft 17 set to {Hand.hit_soft_17}")

    for dealer_face in tqdm(range(2, 12)):
        sample_outcomes = defaultdict(float)

        for _ in range(num_samples):
            dealer = get_hand(dealer_face, deck)
            dealer_action(dealer, deck)
            value = dealer.value if not dealer.is_bust else 0
            for player_value in range(4, 22):
                if player_value > value:
                    sample_outcomes[player_value] += 1
                elif player_value < value:
                    sample_outcomes[player_value] -= 1

        for value, count in sample_outcomes.items():
            sample_outcomes[value] = count / num_samples

        dealer_probs = dealer_rollout(dealer_face, NoneCounter(8))
        estimated_outcomes, _, _ = get_hand_evs(dealer_probs, NoneCounter(8))

        for (hand_value, is_soft), prob in estimated_outcomes.items():
            if is_soft:
                assert estimated_outcomes[(hand_value, False)] == prob
            error = abs(sample_outcomes[hand_value] - prob)
            if error > abs_err:
                print(f"{hand_value} {error}")
                print(f"{dealer_face}: {sample_outcomes}")
                print(f"{dealer_face}: {estimated_outcomes}")
                raise Exception
            errors[dealer_face][hand_value] = error

    if verbose:
        final_value_string = "  "
        for hand_value in range(4, 22):
            final_value_string += f"|  {hand_value} "
        print(final_value_string)

        for dealer_face in range(2, 12):
            string = f"{dealer_face}: "
            for hand_value in range(4, 22):
                string += f"{errors[dealer_face].get(hand_value, 0):.3f} "
            print(string)


def test_evs(verbose=False):
    move_cache = {}

    num_samples = 1000
    abs_err = 0.3

    num_decks = 8

    deck = Deck(num_decks)
    counter = NoneCounter(8)

    errors = defaultdict(dict)

    states = [(value, False) for value in range(4, 22)] + [(value, True) for value in range(12, 22)]

    print(f"testing stand, hit, and double evs with hit on soft 17 set to {Hand.hit_soft_17}")

    for dealer_face in tqdm(range(2, 12)):
        sample_outcomes = defaultdict(float)
        num_outcomes = defaultdict(int)

        for hand_value, is_soft in states:
            for _ in range(num_samples):
                dealer = get_hand(dealer_face, deck)
                dealer_action(dealer, deck)
                dealer_value = dealer.value if not dealer.is_bust else 0
                combos = []

                if is_soft:
                    player = Hand([11, hand_value - 11])
                else:
                    card1 = min(10, hand_value - 2)
                    card2 = hand_value - card1
                    player = Hand([card1, card2])
                while not player.is_bust:
                    move_combo = (
                        player.value,
                        dealer_face,
                        player.is_soft,
                        player.can_double,
                    )
                    if move_combo in move_cache:
                        move = move_cache[move_combo]
                    else:
                        move = get_move(player, dealer_face, counter, num_splits=0)
                        move_cache[move_combo] = move
                    combos.append((player.value, player.is_soft, move))
                    if move == Move.HIT:
                        new_card = deck.deal_card()
                        player.add(new_card)
                    elif move == Move.DOUBLE:
                        new_card = deck.deal_card()
                        player.double(new_card)
                        break
                    elif move == Move.STAND:
                        break
                    else:
                        raise Exception

                if player.value > dealer_value and not player.is_bust:
                    outcome = 1
                elif player.value < dealer_value or player.is_bust:
                    outcome = -1
                else:
                    outcome = 0

                for combo in combos:
                    if combo[2] == Move.DOUBLE:
                        sample_outcomes[combo] += 2 * outcome
                    else:
                        sample_outcomes[combo] += outcome
                    num_outcomes[combo] += 1

        for combo, count in sample_outcomes.items():
            sample_outcomes[combo] = count / num_outcomes[combo]

        dealer_probs = dealer_rollout(dealer_face, NoneCounter(8))
        stand_evs, hit_evs, double_evs = get_hand_evs(dealer_probs, NoneCounter(8))

        _sample_outcomes = {combo: ev for combo, ev in sample_outcomes.items()}
        sample_outcomes = _sample_outcomes

        for move, estimated_evs in zip(
            [Move.STAND, Move.HIT, Move.DOUBLE], [stand_evs, hit_evs, double_evs]
        ):
            for (hand_value, is_soft), ev in estimated_evs.items():
                combo = (hand_value, is_soft, move)
                if combo in sample_outcomes:
                    sample_ev = sample_outcomes[combo]
                    error = abs(sample_ev - ev)
                    if error > abs_err:
                        print(f"{hand_value} {is_soft} {move} {error}")
                        print(f"{dealer_face}: {sample_outcomes}")
                        print(f"{dealer_face}: {estimated_evs}")
                        raise Exception
                else:
                    error = None
                errors[dealer_face][combo] = error

    if verbose:
        for move in [Move.STAND, Move.HIT, Move.DOUBLE]:
            if move == Move.STAND:
                print("\n\nSTAND\n\n")
            elif move == Move.HIT:
                print("\n\nHIT\n\n")
            elif move == Move.DOUBLE:
                print("\n\nDOUBLE\n\n")

            for is_soft in [True, False]:
                print(f"is_soft: {is_soft}")

                dealer_string = "         "
                for dealer_face in range(2, 12):
                    dealer_string += f"| {dealer_face}  "
                print(dealer_string)

                start = 12 if is_soft else 4
                for hand_value in range(start, 22):
                    string_error = f"error  {hand_value}: "
                    for dealer_face in range(2, 12):
                        combo = (hand_value, is_soft, move)
                        error = errors[dealer_face].get(combo, None)
                        string_error += f"{error:1.2f} " if error is not None else "---- "
                    print(string_error)


test_evs(verbose=True)
test_stand_evs(verbose=True)
test_dealer_rollout(verbose=True)
