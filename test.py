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


def test_hit_evs(verbose=False):
    num_samples = 100_000
    abs_err = 5.93

    num_decks = 8

    deck = Deck(num_decks)
    counter = NoneCounter(8)

    errors = defaultdict(dict)

    print(f"testing hit evs with hit on soft 17 set to {Hand.hit_soft_17}")

    for dealer_face in tqdm(range(2, 12)):
        sample_outcomes = defaultdict(float)
        num_outcomes = defaultdict(int)

        for _ in range(num_samples):
            dealer = get_hand(dealer_face, deck)
            dealer_action(dealer, deck)
            value = dealer.value if not dealer.is_bust else 0
            value_move_combinations = []
            player_card = deck.deal_card()
            player = get_hand(player_card, deck, no_splits=True)
            assert not player.can_split
            while not player.is_bust:
                move = get_move(player, dealer_face, counter, num_splits=0)
                value_move_combinations.append((player.value, player.is_soft, move))
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

            if player.value > value and not player.is_bust:
                outcome = 1
            elif player.value < value:
                outcome = -1
            else:
                outcome = 0

            for combo in value_move_combinations:
                sample_outcomes[combo] += outcome
                num_outcomes[combo] += 1

        for combo, count in sample_outcomes.items():
            sample_outcomes[combo] = count / num_outcomes[combo]

        dealer_probs = dealer_rollout(dealer_face, NoneCounter(8))
        stand_evs, hit_evs, double_evs = get_hand_evs(dealer_probs, NoneCounter(8))

        _sample_outcomes = {combo: ev for combo, ev in sample_outcomes.items()}
        sample_outcomes = _sample_outcomes

        for (hand_value, is_soft), prob in stand_evs.items():
            if (hand_value, is_soft, Move.STAND) in sample_outcomes:
                error = abs(sample_outcomes[(hand_value, is_soft, Move.STAND)] - prob)
                if error > abs_err:
                    print(f"{hand_value} {is_soft} {Move.STAND} {error}")
                    print(f"{dealer_face}: {sample_outcomes}")
                    print(f"{dealer_face}: {stand_evs}")
                    raise Exception
            else:
                error = float("-inf")
            errors[dealer_face][(hand_value, is_soft, Move.STAND)] = error

        for (hand_value, is_soft), prob in hit_evs.items():
            if (hand_value, is_soft, Move.HIT) in sample_outcomes:
                error = abs(sample_outcomes[(hand_value, is_soft, Move.HIT)] - prob)
                if error > abs_err:
                    print(f"{hand_value} {is_soft} {Move.HIT} {error}")
                    print(f"{dealer_face}: {sample_outcomes}")
                    print(f"{dealer_face}: {hit_evs}")
                    raise Exception
            else:
                error = float("-inf")
            errors[dealer_face][(hand_value, is_soft, Move.HIT)] = error

        for (hand_value, is_soft), prob in double_evs.items():
            if (hand_value, is_soft, Move.DOUBLE) in sample_outcomes:
                error = abs(sample_outcomes[(hand_value, is_soft, Move.DOUBLE)] - prob)
                if error > abs_err:
                    print(f"{hand_value} {is_soft} {Move.DOUBLE} {error}")
                    print(f"{dealer_face}: {sample_outcomes}")
                    print(f"{dealer_face}: {double_evs}")
                    raise Exception
            else:
                error = float("-inf")
            errors[dealer_face][(hand_value, is_soft, Move.DOUBLE)] = error

    if verbose:
        for move in [Move.STAND, Move.HIT, Move.DOUBLE]:
            print(f"{move}:")
            final_value_string = "  "
            for hand_value in range(4, 22):
                final_value_string += f"|  {hand_value} "
            print(final_value_string)

            for is_soft in [True, False]:
                print(f"is_soft: {is_soft}")
                for dealer_face in range(2, 12):
                    string = f"{dealer_face}: "
                    for hand_value in range(4, 22):
                        ev = errors[dealer_face].get((hand_value, is_soft, move), 0)
                        string += f"{ev:.3f} "
                    print(string)


test_hit_evs(verbose=True)
test_stand_evs(verbose=True)
test_dealer_rollout(verbose=True)
