from dataclasses import dataclass

from models.deck import Hand


class Move:
    STAND = 0
    HIT = 1
    DOUBLE = 2
    SPLIT = 3


class ExpectedValues:
    def __init__(self):
        self.evs = {}

    def get(self, hand_value: int, is_soft: bool):
        return self.evs[(hand_value, is_soft)]

    def get_ev(self, hand: Hand):
        return self.get(hand.value, hand.is_soft)

    def set(self, hand_value: int, is_soft: bool, ev: float):
        self.evs[(hand_value, is_soft)] = ev

    def add(self, hand_value: int, is_soft: bool, ev: float):
        if (hand_value, is_soft) not in self.evs:
            self.evs[(hand_value, is_soft)] = ev
        else:
            self.evs[(hand_value, is_soft)] += ev

    def subtract(self, hand_value: int, is_soft: bool, ev: float):
        if (hand_value, is_soft) not in self.evs:
            self.evs[(hand_value, is_soft)] = -ev
        else:
            self.evs[(hand_value, is_soft)] -= ev

    def contains(self, hand_value: int, is_soft: bool):
        return (hand_value, is_soft) in self.evs


class SplitEVs:
    def __init__(self):
        self.evs = {}

    def get(self, split_card: int):
        return self.evs[split_card]

    def get_ev(self, hand: Hand):
        if not hand.can_split:
            return float("-inf")
        return self.get(hand.cards[0])

    def set(self, split_card: int, ev: float):
        self.evs[split_card] = ev


class DealerProbsTable:
    def __init__(self):
        self.probs = {}

    def get(self, value: int, is_soft: bool):
        return self.probs[(value, is_soft)]

    def get_probs(self, dealer_face: int):
        is_soft = dealer_face == 11
        return self.probs[(dealer_face, is_soft)]

    def set(self, value: int, is_soft: bool, value_probs: dict[int, float]):
        self.probs[(value, is_soft)] = value_probs

    def delete(self, value: int, is_soft: bool):
        del self.probs[(value, is_soft)]

    def contains(self, value: int, is_soft: bool):
        return (value, is_soft) in self.probs


@dataclass
class HandEVs:
    stand: ExpectedValues
    hit: ExpectedValues
    double: ExpectedValues
    split: SplitEVs = SplitEVs()

    def get_max_ev(self, value: int, is_soft: bool, can_split: bool = True):
        evs = [
            self.stand.get(value, is_soft),
            self.hit.get(value, is_soft),
            self.double.get(value, is_soft),
        ]
        if can_split:
            assert value % 2 == 0
            evs.append(self.split.get(value // 2))
        return max(evs)

    def get_move_ranking(self, value: int, is_soft: bool, can_split: bool = True):
        stand_ev = self.stand.get(value, is_soft)
        hit_ev = self.hit.get(value, is_soft)
        double_ev = self.double.get(value, is_soft)
        if can_split:
            assert value % 2 == 0
            split_ev = self.split.get(value // 2)
        else:
            split_ev = float("-inf")

        evs = {
            Move.STAND: stand_ev,
            Move.HIT: hit_ev,
            Move.DOUBLE: double_ev,
            Move.SPLIT: split_ev,
        }
        return [move for move, ev in sorted(evs.items(), key=lambda x: x[1], reverse=True)]
