import time

from main import should_hit, dealer_rollout, dealer_rollout_approximate
from deck import Hand
from counter import NoneCounter


counter = NoneCounter(1)

slow_time = 0
iter_time = 0
for dealer_face in range(2, 12):
    hand = Hand([dealer_face])
    print(hand)
    t0 = time.time()
    for i in range(1):
        dealer_rollout(hand, counter)
    print(dealer_rollout(hand, counter))
    t1 = time.time()
    t2 = time.time()
    for i in range(1):
        dealer_rollout_approximate(hand, counter)
    print(dealer_rollout_approximate(hand, counter))
    t3 = time.time()

    slow_time += t1 - t0
    iter_time += t3 - t2

print(f"Slow time: {slow_time}")
print(f"Iter time: {iter_time}")

string = ""
for dealer_face in range(2, 12):
    string += f"| {dealer_face}"

for hand_value in range(5, 22):
    card1 = hand_value // 2
    card2 = hand_value - card1
    hand = Hand([card1, card2])
    string = ""
    for dealer_face in range(2, 12):
        if should_hit(hand, dealer_face, counter):
            string += "| H"
        else:
            string += "| S"
    print(f"{hand_value}: {string}")
