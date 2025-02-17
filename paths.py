from collections import defaultdict
from line_profiler import LineProfiler

from tqdm import tqdm

from models.counter import PerfectCounter, Counter


HIT_SOFT_17 = False


class ProbNode:
    def __init__(
        self,
        cards: tuple[int, ...],
        value: int,
    ):
        self.cards = cards
        self.value = value
        self.children: tuple[ProbNode, ...] = ()
        self.probs: tuple[float, ...] = ()


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
        if not node.children and node.value < 43:
            probs = []
            children = []
            for card in range(2, 12):
                prob = counter.probability(card)
                child = get_child(node, card)
                children.append(child)
                probs.append(prob)
                if prob > 0:
                    counter.count(card)
                    create_tree(child)
                    counter.uncount(card)
            node.children = tuple(children)
            node.probs = tuple(probs)

    root = ProbNode((), 0)

    create_tree(root)

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
        return node.value != -1 and (node.value < 21 or node.is_soft)

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
        self.times_reached = defaultdict(int)


def get_dealer_finals() -> dict[int, list[tuple[int, int, tuple[int, ...], int]]]:
    final_nodes = {}
    all_nodes: dict[tuple[int, ...], DealerNode] = {}

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
        children = []
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
            child.times_reached[starting_card] += 1
            children.append(child)
            if hit(child):
                create_children(child, starting_card)
            elif child.value != -1 and node.cards not in final_nodes:
                final_nodes[child.cards] = child
        if not node.children:
            node.children = children

    create_children(root_node)

    start_sorted = defaultdict(list)

    for node in final_nodes.values():
        start_sorted[None].append(node)
        for card in node.times_reached:
            start_sorted[card].append(node)

    dealer_finals = defaultdict(list)
    for starting_card in start_sorted:
        nodes = sorted(start_sorted[starting_card], key=lambda x: x.cards)
        prev_cards = ()
        for node in nodes:
            cards = node.cards
            if starting_card is not None:
                cards = list(cards)
                cards.remove(starting_card)
                cards = tuple(sorted(cards))
            return_cards, new_cards = find_path_difference(prev_cards, node.cards)
            stack_index = len(prev_cards) - len(return_cards)
            if starting_card is not None:
                times_reached = node.times_reached[starting_card]
            else:
                times_reached = sum(node.times_reached.values())
            dealer_finals[starting_card].append((node.value, stack_index, new_cards, times_reached))
            prev_cards = cards

    return dealer_finals


def get_node(root: HandNode | ProbNode, cards: list[int]) -> HandNode | ProbNode:
    current_node = root
    sorted_hand = sorted(cards)
    for card in sorted_hand:
        current_node = current_node.children[card - 2]
    return current_node


def calculate_hand_values(
    node: HandNode,
    prob_node: ProbNode,
    dealer_card: int | None,
    dealer_finals: dict,
    seen: set,
) -> tuple[float, float]:
    if node.value != -1 and node not in seen and len(node.cards) > 1:
        node.stand_ev = get_stand_ev(dealer_card, prob_node, node.value, dealer_finals)
        seen.add(node)

    hit_sum = 0.0
    double_sum = 0.0

    for i, child in enumerate(node.children):
        child_prob_node = prob_node.children[i]
        card_prob = prob_node.probs[i]
        if card_prob > 0:
            if child.value != -1:
                child_stand, child_hit = calculate_hand_values(
                    child, child_prob_node, dealer_card, dealer_finals, seen
                )
            else:
                child_stand, child_hit = -1.0, -1.0

            hit_sum += card_prob * max(child_stand, child_hit)
            double_sum += card_prob * child_stand

    node.hit_ev = hit_sum
    node.double_ev = 2 * double_sum

    return node.stand_ev, node.hit_ev


def get_hand_values(
    hand_node: HandNode,
    prob_node: ProbNode,
    dealer_finals,
    dealer_card: int | None = None,
):
    seen = set()
    stand_ev, hit_ev = calculate_hand_values(hand_node, prob_node, dealer_card, dealer_finals, seen)
    return stand_ev, hit_ev, hand_node.double_ev


def get_stand_ev(dealer_card: int | None, prob_node, player_value, dealer_finals):
    ev = 0.0
    bust_prob = 1.0
    prob_stack = [1.0] * (22)
    prob_node_stack = [prob_node] * (22)
    for dealer_value, stack_idx, new_cards, times_reached in dealer_finals[dealer_card]:
        for card in new_cards:
            prob_stack[stack_idx + 1] = (
                prob_stack[stack_idx] * prob_node_stack[stack_idx].probs[card - 2]
            )
            prob_node_stack[stack_idx + 1] = prob_node_stack[stack_idx].children[card - 2]
            stack_idx += 1
        prob = prob_stack[stack_idx]
        prob *= times_reached
        if player_value > dealer_value:
            ev += prob
        elif player_value < dealer_value:
            ev -= prob
        bust_prob -= prob
    return ev + bust_prob


def find_path_difference(
    a: tuple[int, ...], b: tuple[int, ...]
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    if not a:
        return (), b
    if not b:
        return a, ()

    min_len = min(len(a), len(b))
    diff_point = 0

    while diff_point < min_len and a[diff_point] == b[diff_point]:
        diff_point += 1

    if diff_point == min_len:
        return a[diff_point:], b[diff_point:]

    return a[diff_point:], b[diff_point:]


if __name__ == "__main__":
    counter = PerfectCounter(8)

    prob_root = get_prob_tree(counter)
    player_root = get_player_tree()
    dealer_finals = get_dealer_finals()

    dealer_card = 5
    counter.count(dealer_card)
    player_cards = [2, 9]
    for card in player_cards:
        counter.count(card)

    player_node = get_node(player_root, player_cards)
    prob_node = get_node(prob_root, player_cards)

    profile = LineProfiler()
    profile.add_function(get_hand_values)
    profile.add_function(calculate_hand_values)
    profile.add_function(get_stand_ev)

    test_func = profile(
        lambda: (get_hand_values(player_node, prob_node, dealer_finals, dealer_card),)
    )
    # test_func = profile(lambda: (get_stand_ev(None, prob_root, 10, dealer_finals),))

    test_func()
    profile.print_stats()

    for i in tqdm(range(100000)):
        get_stand_ev(dealer_card, prob_node, 10, dealer_finals)

    print(get_hand_values(player_root, prob_root, dealer_finals))
    print(get_hand_values(player_node, prob_node, dealer_finals))

    for i in tqdm(range(100000)):
        get_hand_values(player_node, prob_node, dealer_finals, dealer_card)
        # get_hand_values(player_node, prob_node, dealer_finals, dealer_card)
