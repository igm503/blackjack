from main import get_move, Move, should_surrender
from models.deck import Hand
from models.counter import PerfectCounter

GREEN = "\033[91m"
RED = "\033[95m"
BLUE = "\033[94m"
YELLOW = "\033[93m"
ENDC = "\033[0m"

NUM_DECKS = 8


def get_dealer_face_string():
    string = "     "
    for dealer_face in range(2, 12):
        string += f"| {dealer_face} "
    return string + "\n"


def get_move_string(hand, dealer_face, counter, num_splits=0):
    move = get_move(hand, dealer_face, counter, num_splits)
    if move == Move.STAND:
        return f"| {YELLOW}S{ENDC} "
    elif move == Move.HIT:
        return f"| {RED}H{ENDC} "
    elif move == Move.DOUBLE:
        return f"| {BLUE}D{ENDC} "
    elif move == Move.SPLIT:
        return f"| {GREEN}P{ENDC} "
    else:
        return "| ?"


def get_surrender_string(hand, dealer_face, counter, num_splits=3):
    surrender = should_surrender(hand, dealer_face, counter, num_splits)
    if surrender:
        return f"| {BLUE}S{ENDC} "
    else:
        return f"| {RED}N{ENDC} "


def basic_strategy_hard():
    counter = PerfectCounter(NUM_DECKS)

    print(get_dealer_face_string())

    for hand_value in range(5, 22):
        card1 = min(hand_value - 2, 10)
        card2 = hand_value - card1
        counter.count(card1)
        counter.count(card2)
        hand = Hand([card1, card2])
        string = ""
        for dealer_face in range(2, 12):
            counter.count(dealer_face)
            string += get_move_string(hand, dealer_face, counter)
            counter.uncount(dealer_face)
        counter.uncount(card1)
        counter.uncount(card2)
        string += "\n" + "-" * 45
        print(f"{hand_value:3.0f}: {string}")


def basic_strategy_soft():
    counter = PerfectCounter(NUM_DECKS)

    print(get_dealer_face_string())

    for hand_value in range(13, 22):
        card1 = 11
        card2 = hand_value - 11
        counter.count(card1)
        counter.count(card2)
        hand = Hand([card1, card2])
        string = ""
        for dealer_face in range(2, 12):
            counter.count(dealer_face)
            string += get_move_string(hand, dealer_face, counter)
            counter.uncount(dealer_face)
        counter.uncount(card1)
        counter.uncount(card2)
        string += "\n" + "-" * 45
        print(f"{hand_value:3.0f}: {string}")


def basic_strategy_pair():
    counter = PerfectCounter(NUM_DECKS)

    print(get_dealer_face_string())

    for card in range(2, 12):
        counter.count(card)
        counter.count(card)
        hand = Hand([card, card])
        string = ""
        for dealer_face in range(2, 12):
            counter.count(dealer_face)
            string += get_move_string(hand, dealer_face, counter, num_splits=3)
            counter.uncount(dealer_face)
        string += "\n" + "-" * 45
        print(f"{card}: {string}")
        counter.uncount(card)
        counter.uncount(card)


def surrender_strategy():
    counter = PerfectCounter(NUM_DECKS)
    print(get_dealer_face_string())
    for hand_value in range(5, 22):
        card1 = min(hand_value - 2, 10)
        card2 = hand_value - card1
        counter.count(card1)
        counter.count(card2)
        hand = Hand([card1, card2])
        string = ""
        for dealer_face in range(2, 12):
            counter.count(dealer_face)
            string += get_surrender_string(hand, dealer_face, counter, num_splits=3)
            counter.uncount(dealer_face)
        counter.uncount(card1)
        counter.uncount(card2)
        string += "\n" + "-" * 45
        print(f"{hand_value:3.0f}: {string}")


basic_strategy_hard()
print("\n\n")
basic_strategy_soft()
print("\n\n")
basic_strategy_pair()
print("\n\n")
surrender_strategy()
