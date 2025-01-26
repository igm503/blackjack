from main import get_move, Move
from deck import Hand
from counter import NoneCounter

GREEN = "\033[92m"
RED = "\033[95m"
BLUE = "\033[94m"
YELLOW = "\033[93m"
ENDC = "\033[0m"


def get_dealer_face_string():
    string = "     "
    for dealer_face in range(2, 12):
        string += f"| {dealer_face} "
    return string + "\n"


def get_move_string(hand, dealer_face, counter):
    move = get_move(hand, dealer_face, counter)
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


def basic_strategy_hard():
    counter = NoneCounter(1)

    print(get_dealer_face_string())

    for hand_value in range(5, 22):
        card1 = hand_value // 2
        card2 = hand_value - card1
        if hand_value == 21:
            hand = Hand([card1, card2, 0])
        else:
            hand = Hand([card1, card2])
        string = ""
        for dealer_face in range(2, 12):
            string += get_move_string(hand, dealer_face, counter)
        string += "\n" + "-" * 45
        print(f"{hand_value:3.0f}: {string}")


def basic_strategy_soft():
    counter = NoneCounter(1)

    print(get_dealer_face_string())

    for hand_value in range(13, 22):
        card1 = 11
        card2 = hand_value - 11
        if hand_value == 21:
            hand = Hand([card1, card2, 0])
        else:
            hand = Hand([card1, card2])
        string = ""
        for dealer_face in range(2, 12):
            string += get_move_string(hand, dealer_face, counter)
        string += "\n" + "-" * 45
        print(f"{hand_value:3.0f}: {string}")


basic_strategy_hard()
print("\n\n")
basic_strategy_soft()
