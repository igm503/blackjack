from main import get_move, Move
from deck import Hand
from counter import NoneCounter


counter = NoneCounter(1)

string = "     "
for dealer_face in range(2, 12):
    string += f"| {dealer_face}"

print(string)

for hand_value in range(5, 22):
    card1 = hand_value // 2
    card2 = hand_value - card1
    hand = Hand([card1, card2])
    string = ""
    for dealer_face in range(2, 12):
        move = get_move(hand, dealer_face, counter)
        if move == Move.STAND:
            string += "| S"
        elif move == Move.HIT:
            string += "| H"
        elif move == Move.DOUBLE:
            string += "| D"
    print(f"{hand_value:3.0f}: {string}")
