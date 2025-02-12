import math
import collections
from line_profiler import LineProfiler

from tqdm import tqdm

from models.counter import PerfectCounter, Counter


HIT_SOFT_17 = False

dealer_finals = {}


class ProbNode:
    def __init__(
        self,
        cards: tuple[int, ...],
        value: int,
    ):
        self.cards = cards
        self.value = value
        self.children: list[ProbNode] = []
        self.probs: list[float] = []


def get_prob_tree(counter: Counter) -> ProbNode:
    all_nodes = {}

    def get_child(node: ProbNode, card: int) -> ProbNode:
        new_cards = tuple(sorted(node.cards + (card,)))

        if new_cards in all_nodes:
            child = all_nodes[new_cards]
        else:
            new_value = node.value + card
            if card == 11:
                new_value -= 10
            child = ProbNode(new_cards, new_value)
            all_nodes[new_cards] = child
        return child

    def create_tree(node: ProbNode):
        nonlocal total
        if not node.children and node.value < 40:
            for card in range(2, 12):
                prob = counter.probability(card)
                child = get_child(node, card)
                node.children.append(child)
                node.probs.append(prob)
                if prob > 0:
                    counter.count(card)
                    create_tree(child)
                    counter.uncount(card)
                total += 1

    root = ProbNode((), 0)
    total = 0

    create_tree(root)

    # print(f"Number of ProbNodes : {total}")

    return root


class HandNode:
    def __init__(self, cards: tuple[int, ...], value: int, is_soft: bool):
        self.cards = cards
        self.children = []
        self.is_soft = is_soft
        self.value = value
        self.dealer_probs = {}
        self.stand_ev = -1.0
        self.hit_ev = -1.0
        self.double_ev = -2.0
        self.split_ev = -1.0

    def __lt__(self, other):
        return self.cards < other.cards

    def __gt__(self, other):
        return self.cards > other.cards


def get_player_tree() -> HandNode:
    all_nodes = {}

    def get_children(node: HandNode) -> list[HandNode]:
        children = []
        for card in range(2, 12):
            new_cards = tuple(sorted(node.cards + (card,)))
            if new_cards in all_nodes:
                child = all_nodes[new_cards]
            else:
                new_value = node.value + card
                new_soft = node.is_soft or card == 11
                if new_value > 21 and new_soft:
                    new_value -= 10
                    new_soft = False
                if new_value > 21:
                    new_value = -1
                child = HandNode(new_cards, new_value, new_soft)
                all_nodes[new_cards] = child
            children.append(child)
        return children

    root_node = HandNode((), 0, False)
    current_nodes = [root_node]

    def hit_to_bust(node):
        return node.value != -1

    while current_nodes:
        next_nodes = []
        for node in current_nodes:
            if hit_to_bust(node) and not node.children:
                children = get_children(node)
                node.children = children
                next_nodes.extend(node.children)
        current_nodes = next_nodes

    return root_node


class DealerNode:
    def __init__(self, cards: tuple[int, ...], value: int, is_soft: bool):
        self.cards = cards
        self.children = []
        self.is_soft = is_soft
        self.value = value
        self.times_reached = 0
        self.starting_cards = set()


def get_dealer_tree() -> DealerNode:
    all_nodes = {}

    root_node = DealerNode((), 0, False)

    def hit_soft_17(node):
        return -1 < node.value < 17 or node.value == 17 and node.is_soft

    def hit_under_17(node):
        return -1 < node.value < 17

    if HIT_SOFT_17:
        hit = hit_soft_17
    else:
        hit = hit_under_17

    def create_children(node: DealerNode, starting_card: int | None = None):
        for card in range(2, 12):
            if not node.cards:
                starting_card = card
            new_cards = tuple(sorted(node.cards + (card,)))
            if new_cards in all_nodes:
                child = all_nodes[new_cards]
            else:
                new_value = node.value + card
                new_soft = node.is_soft or card == 11
                if new_value > 21 and new_soft:
                    new_value -= 10
                    new_soft = False
                if new_value > 21:
                    new_value = -1
                child = DealerNode(new_cards, new_value, new_soft)
                all_nodes[new_cards] = child
            child.times_reached += 1
            child.starting_cards.add(starting_card)
            node.children.append(child)
            if not hit(child):
                if node.value != -1 and node.cards not in dealer_finals:
                    dealer_finals[child.cards] = child
            if hit(child):
                create_children(child, starting_card)

    create_children(root_node)

    return root_node


def get_node(root: HandNode | ProbNode, cards: list[int]) -> HandNode | ProbNode:
    current_node = root
    sorted_hand = sorted(cards)
    for card in sorted_hand:
        current_node = current_node.children[card - 2]
    return current_node


# def get_hand_values(hand_node: HandNode, counter: Counter, dealer_node: HandNode):
#     seen = set()
#
#     def calculate_node_values(node: HandNode) -> tuple[float, float]:
#         if node.children and node not in seen:
#             # node.dealer_probs = get_dealer_probs(dealer_node, counter)
#             node.stand_ev = get_dealer_ev(dealer_node, counter, node.value)
#             seen.add(node)
#
#         hit_sum = 0.0
#         double_sum = 0.0
#
#         for i, child in enumerate(node.children):
#             card = i + 2
#             prob = counter.probability(card)
#             if prob > 0:
#                 if child.children:
#                     counter.count(card)
#                     child_stand, child_hit = calculate_node_values(child)
#                     counter.uncount(card)
#                 else:
#                     child_stand, child_hit = -1.0, -1.0
#
#                 hit_sum += prob * max(child_stand, child_hit)
#                 double_sum += prob * child_stand
#
#         node.hit_ev = hit_sum
#         node.double_ev = 2 * double_sum
#
#         return node.stand_ev, node.hit_ev
#
#     stand_ev, hit_ev = calculate_node_values(hand_node)
#     return stand_ev, hit_ev, hand_node.double_ev
#
#
# def get_dealer_ev(dealer_node, counter, player_value):
#     assert player_value != -1
#
#     ev = 0.0
#
#     def calculate_node_values(node: HandNode, prob: float) -> None:
#         nonlocal ev
#         for i, child in enumerate(node.children):
#             card = i + 2
#             card_prob = counter.probability(card)
#             if card_prob > 0:
#                 child_prob = card_prob * prob
#                 if child.children:
#                     counter.count(card)
#                     calculate_node_values(child, child_prob)
#                     counter.uncount(card)
#                 else:
#                     if player_value > child.value:
#                         ev += child_prob
#                     elif player_value < child.value:
#                         ev -= child_prob
#
#     if not dealer_node.children:
#         if player_value > dealer_node.value:
#             return 1.0
#         elif player_value < dealer_node.value:
#             return -1.0
#         return 0.0
#
#     calculate_node_values(dealer_node, 1.0)
#     return ev


def calculate_hand_values(
    node: HandNode, prob_node: ProbNode, dealer_node: HandNode, seen: set
) -> tuple[float, float]:
    if node.children and node not in seen:
        node.stand_ev = get_dealer_ev(dealer_node, prob_node, node.value)
        seen.add(node)

    hit_sum = 0.0
    double_sum = 0.0

    for i, child in enumerate(node.children):
        child_prob_node = prob_node.children[i]
        card_prob = prob_node.probs[i]
        if card_prob > 0:
            if child.children:
                child_stand, child_hit = calculate_hand_values(
                    child, child_prob_node, dealer_node, seen
                )
            else:
                child_stand, child_hit = -1.0, -1.0

            hit_sum += card_prob * max(child_stand, child_hit)
            double_sum += card_prob * child_stand

    node.hit_ev = hit_sum
    node.double_ev = 2 * double_sum

    return node.stand_ev, node.hit_ev


def get_hand_values(hand_node: HandNode, prob_node: ProbNode, dealer_node: HandNode):
    seen = set()
    stand_ev, hit_ev = calculate_hand_values(hand_node, prob_node, dealer_node, seen)
    return stand_ev, hit_ev, hand_node.double_ev


def calculate_dealer_values(
    node: HandNode, prob_node: ProbNode, prob: float, player_value: int
) -> float:
    ev = 0.0
    for i, child in enumerate(node.children):
        print(prob_node.cards)
        print(node.cards)
        child_prob = prob * prob_node.probs[i]
        if child_prob > 0:
            if child.children:
                ev += calculate_dealer_values(
                    child, prob_node.children[i], child_prob, player_value
                )
            else:
                if player_value > child.value:
                    ev += child_prob
                elif player_value < child.value:
                    ev -= child_prob
    return ev


def get_dealer_ev(dealer_node: HandNode, prob_node: ProbNode, player_value: int) -> float:
    assert player_value != -1
    if not dealer_node.children:
        if player_value > dealer_node.value:
            return 1.0
        elif player_value < dealer_node.value:
            return -1.0
        return 0.0

    prob_node = get_node(prob_node, dealer_node.cards)

    return calculate_dealer_values(dealer_node, prob_node, 1.0, player_value)


def _calculate_dealer_values(node: HandNode, counter, prob: float, player_value: int) -> float:
    ev = 0.0
    for i, child in enumerate(node.children):
        card = i + 2
        card_prob = counter.probability(card)
        if card_prob > 0:
            child_prob = card_prob * prob
            if child.children:
                counter.count(card)
                ev += _calculate_dealer_values(child, counter, child_prob, player_value)
                counter.uncount(card)
            else:
                if player_value > child.value:
                    ev += child_prob
                elif player_value < child.value:
                    ev -= child_prob
    return ev


def _get_dealer_ev(dealer_node: HandNode, counter, player_value: int) -> float:
    assert player_value != -1
    if not dealer_node.children:
        if player_value > dealer_node.value:
            return 1.0
        elif player_value < dealer_node.value:
            return -1.0
        return 0.0

    return _calculate_dealer_values(dealer_node, counter, 1.0, player_value)


def get_dealer_probs(hand_node: HandNode, counter: Counter):
    probs = {-1: 0.0, 17: 0.0, 18: 0.0, 19: 0.0, 20: 0.0, 21: 0.0}

    def calculate_node_values(node: HandNode, prob: float) -> dict[int, float]:
        for i, child in enumerate(node.children):
            card = i + 2
            card_prob = counter.probability(card)
            if card_prob > 0:
                if child.children:
                    counter.count(card)
                    calculate_node_values(child, card_prob * prob)
                    counter.uncount(card)
                else:
                    probs[child.value] += card_prob * prob

        return probs

    if not hand_node.children:
        probs[hand_node.value] = 1.0
        return probs

    calculate_node_values(hand_node, 1.0)
    return probs


def get_stand_ev(node: HandNode) -> float:
    stand_ev = 0
    for dealer_value, prob in node.dealer_probs.items():
        if node.value > dealer_value:
            stand_ev += prob
        elif node.value < dealer_value:
            stand_ev -= prob
    return stand_ev


def get_dealer_ev_fast(prob_node, player_value, dealer_card=None):
    ev = 0.0
    dealer_card = dealer_node.cards[0]
    total_prob = 0
    for cards, final_node in dealer_finals.items():
        if dealer_card is None or dealer_card in final_node.starting_cards:
            prob = 1.0
            times_reached = final_node.times_reached
            current_node = prob_node
            skipped = False
            # print(times_reached, cards, final_node.starting_cards, final_node.value)
            for card in cards:
                if not skipped and card == dealer_card:
                    skipped = True
                    times_reached /= len(final_node.starting_cards)
                else:
                    prob *= current_node.probs[card - 2]
                    current_node = current_node.children[card - 2]
            prob *= times_reached
            if player_value > final_node.value:
                ev += prob
            elif player_value < final_node.value:
                ev -= prob
            total_prob += prob
    print("total prob", total_prob)
    return ev + (1 - total_prob)


if __name__ == "__main__":
    # for i in tqdm(range(100)):
    #     root_node = get_possible_hands()

    counter = PerfectCounter(8)

    prob_root = get_prob_tree(counter)

    player_root = get_player_tree()
    dealer_root = get_dealer_tree()
    print("final values", len(dealer_finals))

    final_values = []

    # for i in tqdm(range(1000)):
    #     hand = get_hand_node(player_root, [2, 2, 2, 2, 2, 2, 11])

    dealer_cards = [2]
    for card in dealer_cards:
        counter.count(card)
    player_cards = [2, 9]
    for card in player_cards:
        counter.count(card)

    player_node = get_node(player_root, player_cards)
    dealer_node = get_node(dealer_root, dealer_cards)
    prob_node = get_node(prob_root, player_cards)
    profile = LineProfiler()

    # profile.add_function(get_hand_values)
    profile.add_function(get_dealer_ev)
    # profile.add_function(calculate_hand_values)
    profile.add_function(calculate_dealer_values)
    profile.add_function(get_dealer_ev_fast)

    # test_func = profile(lambda: get_hand_values(player_node, prob_node, dealer_node))
    # test_func = profile(lambda: get_dealer_ev_fast(dealer_node, prob_node, 0))
    test_func = profile(
        lambda: (
            get_dealer_ev(dealer_root, prob_node, 0),
            get_dealer_ev_fast(prob_node, 0, dealer_card=10),
        )
    )

    test_func()

    profile.print_stats()

    print(get_dealer_probs(dealer_node, counter))
    print(get_dealer_ev(dealer_node, prob_node, 0))
    print(get_dealer_ev_fast(prob_node, 0, dealer_card=dealer_node.cards[0]))
    for i in tqdm(range(100000)):
        # get_dealer_ev(dealer_node, prob_node, 0)
        # _get_dealer_ev(dealer_root, counter, 0)
        get_dealer_ev_fast(dealer_node, prob_node, 0)

    print(get_hand_values(player_node, prob_node, dealer_node))
    for i in tqdm(range(100000)):
        get_hand_values(player_node, prob_node, dealer_node)


# def _get_hand_values(hand_node: HandNode, counter: Counter, dealer_node: HandNode):
#     current_node = hand_node
#     current_position = []
#     current_parent = []
#     seen = set()
#     down = True
#     while True:
#         if down:
#             if current_node not in seen:
#                 current_node.dealer_probs = get_dealer_probs(dealer_node, counter)
#                 current_node.stand_ev = get_stand_ev(current_node)
#                 seen.add(current_node)
#             if current_node.children:
#                 new_card = 2
#                 card_prob = counter.probability(new_card)
#                 current_position.append(0)
#                 if card_prob == 0:
#                     down = False
#                 else:
#                     counter.count(new_card)
#                     current_parent.append(current_node)
#                     current_node = current_node.children[0]
#             else:
#                 assert current_node.value in [-1, 21]
#                 down = False
#                 current_node = current_parent.pop()
#                 counter.uncount(current_position[-1] + 2)
#
#         elif current_node.children:
#             if current_position[-1] < 9:
#                 current_position[-1] += 1
#                 new_card = current_position[-1] + 2
#                 card_prob = counter.probability(new_card)
#                 if card_prob != 0:
#                     down = True
#                     counter.count(new_card)
#                     current_parent.append(current_node)
#                     current_node = current_node.children[current_position[-1]]
#             else:
#                 if current_position[-1] == 9:
#                     current_node.hit_ev, current_node.double_ev = get_hit_double_ev(
#                         current_node, counter
#                     )
#                 if current_node == hand_node:
#                     break
#                 current_node = current_parent.pop()
#                 current_position.pop()
#                 counter.uncount(current_position[-1] + 2)
#
#     return hand_node.stand_ev, hand_node.hit_ev, hand_node.double_ev
#
#
# def _get_dealer_probs(hand_node: HandNode, counter: Counter) -> dict[int, float]:
#     probs = {-1: 0.0, 17: 0.0, 18: 0.0, 19: 0.0, 20: 0.0, 21: 0.0}
#     current_node = hand_node
#     current_position = []
#     current_parent = []
#     current_probs = [1.0]
#     down = True
#     while True:
#         if down:
#             if current_node.children:
#                 current_position.append(0)
#                 new_card = 2
#                 card_prob = counter.probability(new_card)
#                 if card_prob == 0:
#                     down = False
#                 else:
#                     counter.count(new_card)
#                     current_parent.append(current_node)
#                     current_node = current_node.children[0]
#                     current_probs.append(current_probs[-1] * card_prob)
#             else:
#                 probs[current_node.value] += current_probs.pop()
#                 down = False
#                 current_node = current_parent.pop()
#                 counter.uncount(current_position[-1] + 2)
#
#         elif current_node.children:
#             if current_position[-1] < 9:
#                 current_position[-1] += 1
#                 new_card = current_position[-1] + 2
#                 card_prob = counter.probability(new_card)
#                 if card_prob != 0:
#                     down = True
#                     counter.count(new_card)
#                     current_parent.append(current_node)
#                     current_probs.append(current_probs[-1] * card_prob)
#                     current_node = current_node.children[current_position[-1]]
#             else:
#                 if current_node == hand_node:
#                     break
#                 current_node = current_parent.pop()
#                 current_position.pop()
#                 counter.uncount(current_position[-1] + 2)
#                 current_probs.pop()
#
#     assert abs(1 - sum(probs.values())) < 1e-6
#     return probs
