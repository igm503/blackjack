from collections import defaultdict

from tqdm import tqdm

from main import dealer_rollout, get_hand_evs
from deck import Deck, Hand
from counter import NoneCounter


def get_dealer_hand(dealer_face, deck):
    deck.shuffle()
    second_card = deck.deal_card()
    if dealer_face == 11:
        while second_card == 10:
            second_card = deck.deal_card()  # dealer already peaked
    elif dealer_face == 10:
        while second_card == 11:
            second_card = deck.deal_card()
    dealer = Hand([dealer_face, second_card])
    return dealer


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
            dealer = get_dealer_hand(dealer_face, deck)
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
            dealer = get_dealer_hand(dealer_face, deck)
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


test_stand_evs(verbose=True)
test_dealer_rollout(verbose=True)
