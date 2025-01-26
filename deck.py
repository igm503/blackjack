from __future__ import annotations
import random


class Deck:
    def __init__(self, num_decks, penetration=0.5):
        self.num_cards = num_decks * 52
        self.penetration = penetration
        suit = [2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 10, 10, 11]
        self.cards = []
        for _ in range(num_decks * 4):
            self.cards.extend([i for i in suit])

        self.discard = []

    def deal_card(self):
        card = self.cards.pop()
        self.discard.append(card)
        return card

    def deal_hand(self):
        return Hand([self.deal_card() for _ in range(2)])

    def shuffle(self):
        self.cards.extend(self.discard)
        self.discard = []
        random.shuffle(self.cards)

    @property
    def must_shuffle(self):
        return len(self.discard) > self.num_cards * self.penetration


class DoubleOn:
    ANY = 0
    NINE_TO_ELEVEN = 1
    TEN_TO_ELEVEN = 2


class Hand:
    double_after_split: bool = False
    double_on: int = DoubleOn.ANY
    hit_split_aces: bool = False
    resplit_aces: bool = False
    hit_soft_17: bool = False

    @classmethod
    def set_rules(cls, hit_soft_17, double_after_split, double_on, hit_split_aces, resplit_aces):
        cls.hit_soft_17 = hit_soft_17
        cls.double_after_split = double_after_split
        cls.double_on = double_on
        cls.hit_split_aces = hit_split_aces
        cls.resplit_aces = resplit_aces

    def __init__(self, cards: list[int], is_split=False):
        self.cards = cards
        self.is_split = is_split
        self.is_double = False

    def add(self, card: int) -> None:
        self.cards.append(card)

    def split(self, deck: Deck) -> tuple[Hand, Hand]:
        assert self.can_split
        return (
            Hand([self.cards[0], deck.deal_card()], is_split=True),
            Hand([self.cards[1], deck.deal_card()], is_split=True),
        )

    def double(self, card):
        assert self.can_double
        self.cards.append(card)
        self.is_double = True

    @property
    def value(self):
        num_aces = self.cards.count(11)
        value = sum([card if card != 11 else 1 for card in self.cards])
        if num_aces > 0 and value + 10 <= 21:
            value += 10
        return value

    @property
    def is_soft(self):
        num_aces = self.cards.count(11)
        value = sum([card if card != 11 else 1 for card in self.cards])
        if num_aces > 0 and value + 10 <= 21:
            return True
        return False

    @property
    def can_hit(self):
        return not self.is_double and not (self.is_split and not self.hit_split_aces)

    @property
    def must_hit(self):
        value = self.value
        return value < 17 or (value == 17 and self.is_soft and self.hit_soft_17)

    @property
    def can_split(self) -> bool:
        return len(self.cards) == 2 and self.cards[0] == self.cards[1]

    @property
    def can_double(self) -> bool:
        return len(self.cards) == 2

    @property
    def is_bust(self):
        return self.value > 21

    @property
    def is_blackjack(self):
        return len(self.cards) == 2 and sum(self.cards) == 21

    def __str__(self):
        return str(self.cards)
