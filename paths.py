from tqdm import tqdm

from models.counter import PerfectCounter, Counter


class HandNode:
    def __init__(self, cards, value, is_soft):
        self.cards = cards
        self.children = []
        self.is_soft = is_soft
        self.value = value
        self.dealer_probs = {}
        self.stand_ev = -1.0
        self.hit_ev = -1.0
        self.double_ev = -1.0
        self.split_ev = -1.0

    def __lt__(self, other):
        return self.cards < other.cards

    def __gt__(self, other):
        return self.cards > other.cards


def get_possible_hands(dealer: bool = False) -> HandNode:
    all_nodes = {}

    def get_children(node: HandNode) -> list[HandNode]:
        children = []
        for card in range(2, 12):
            new_cards = tuple(sorted(node.cards + [card]))
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
                child = HandNode(list(new_cards), new_value, new_soft)
                all_nodes[new_cards] = child
            children.append(child)
        return children

    root_node = HandNode([], 0, False)
    current_nodes = [root_node]

    total = 0
    while current_nodes:
        next_nodes = []
        for node in current_nodes:
            stop_value = 17 if dealer else 21
            if -1 < node.value < stop_value and not node.children:
                children = get_children(node)
                node.children = children
                next_nodes.extend(node.children)
        current_nodes = next_nodes
        total += len(next_nodes)

    return root_node


def get_hand_node(root: HandNode, cards: list[int]) -> HandNode:
    current_node = root
    sorted_hand = sorted(cards)
    for card in sorted_hand:
        current_node = current_node.children[card - 2]
    return current_node


def _get_hand_values(hand_node: HandNode, counter: Counter, dealer_node: HandNode):
    current_node = hand_node
    current_position = []
    current_parent = []
    seen = set()
    down = True
    while True:
        if down:
            if current_node not in seen:
                current_node.dealer_probs = get_dealer_probs(dealer_node, counter)
                current_node.stand_ev = get_stand_ev(current_node)
                seen.add(current_node)
            if current_node.children:
                new_card = 2
                card_prob = counter.probability(new_card)
                current_position.append(0)
                if card_prob == 0:
                    down = False
                else:
                    counter.count(new_card)
                    current_parent.append(current_node)
                    current_node = current_node.children[0]
            else:
                assert current_node.value in [-1, 21]
                down = False
                current_node = current_parent.pop()
                counter.uncount(current_position[-1] + 2)

        elif current_node.children:
            if current_position[-1] < 9:
                current_position[-1] += 1
                new_card = current_position[-1] + 2
                card_prob = counter.probability(new_card)
                if card_prob != 0:
                    down = True
                    counter.count(new_card)
                    current_parent.append(current_node)
                    current_node = current_node.children[current_position[-1]]
            else:
                if current_position[-1] == 9:
                    current_node.hit_ev, current_node.double_ev = get_hit_double_ev(
                        current_node, counter
                    )
                if current_node == hand_node:
                    break
                current_node = current_parent.pop()
                current_position.pop()
                counter.uncount(current_position[-1] + 2)

    return hand_node.stand_ev, hand_node.hit_ev, hand_node.double_ev


def get_hand_values(hand_node: HandNode, counter: Counter, dealer_node: HandNode):
    seen = set()

    def calculate_node_values(node: HandNode) -> tuple[float, float]:
        if node.value != -1 and node not in seen:
            node.dealer_probs = get_dealer_probs(dealer_node, counter)
            node.stand_ev = get_stand_ev(node)
            seen.add(node)

        if not node.children:
            return node.stand_ev, node.stand_ev

        hit_sum = 0.0
        double_sum = 0.0

        for i, child in enumerate(node.children):
            card = i + 2
            prob = counter.probability(card)
            if prob > 0:
                counter.count(card)
                child_stand, child_hit = calculate_node_values(child)
                counter.uncount(card)

                hit_sum += prob * max(child_stand, child_hit)
                double_sum += prob * child_stand

        node.hit_ev = hit_sum
        node.double_ev = 2 * double_sum

        return node.stand_ev, node.hit_ev

    stand_ev, hit_ev = calculate_node_values(hand_node)
    return stand_ev, hit_ev, hand_node.double_ev


def get_stand_ev(node: HandNode) -> float:
    if node.value == -1:
        return -1
    else:
        stand_ev = 0
        for dealer_value, prob in node.dealer_probs.items():
            if node.value > dealer_value:
                stand_ev += prob
            elif node.value < dealer_value:
                stand_ev -= prob
        return stand_ev


def get_hit_double_ev(node: HandNode, counter: Counter) -> tuple[float, float]:
    hit_ev = 0
    double_ev = 0
    for idx, child in enumerate(node.children):
        card_prob = counter.probability(idx + 2)
        hit_ev += card_prob * max(child.stand_ev, child.hit_ev)
        double_ev += 2 * card_prob * child.stand_ev
    return hit_ev, double_ev


def _get_dealer_probs(hand_node: HandNode, counter: Counter) -> dict[int, float]:
    probs = {-1: 0.0, 17: 0.0, 18: 0.0, 19: 0.0, 20: 0.0, 21: 0.0}
    current_node = hand_node
    current_position = []
    current_parent = []
    current_probs = [1.0]
    down = True
    while True:
        if down:
            if current_node.children:
                current_position.append(0)
                new_card = 2
                card_prob = counter.probability(new_card)
                if card_prob == 0:
                    down = False
                else:
                    counter.count(new_card)
                    current_parent.append(current_node)
                    current_node = current_node.children[0]
                    current_probs.append(current_probs[-1] * card_prob)
            else:
                probs[current_node.value] += current_probs.pop()
                down = False
                current_node = current_parent.pop()
                counter.uncount(current_position[-1] + 2)

        elif current_node.children:
            if current_position[-1] < 9:
                current_position[-1] += 1
                new_card = current_position[-1] + 2
                card_prob = counter.probability(new_card)
                if card_prob != 0:
                    down = True
                    counter.count(new_card)
                    current_parent.append(current_node)
                    current_probs.append(current_probs[-1] * card_prob)
                    current_node = current_node.children[current_position[-1]]
            else:
                if current_node == hand_node:
                    break
                current_node = current_parent.pop()
                current_position.pop()
                counter.uncount(current_position[-1] + 2)
                current_probs.pop()

    assert abs(1 - sum(probs.values())) < 1e-6
    return probs


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


for i in tqdm(range(10)):
    root_node = get_possible_hands()

player_root = get_possible_hands(dealer=False)
dealer_root = get_possible_hands(dealer=True)

final_values = []

for i in tqdm(range(1000)):
    hand = get_hand_node(player_root, [2, 2, 2, 2, 2, 2, 11])

counter = PerfectCounter(8)

dealer_cards = [8]
for card in dealer_cards:
    counter.count(card)
player_cards = [3, 5]
for card in player_cards:
    counter.count(card)

player_node = get_hand_node(player_root, player_cards)
dealer_node = get_hand_node(dealer_root, dealer_cards)

for i in tqdm(range(100000)):
    get_dealer_probs(dealer_node, counter)

print(get_dealer_probs(dealer_node, counter))

for i in tqdm(range(100000)):
    get_hand_values(player_node, counter, dealer_node)
    counter.count(dealer_cards[0])

print(get_hand_values(player_node, counter, dealer_node))
