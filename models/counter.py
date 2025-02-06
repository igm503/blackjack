from abc import ABC, abstractmethod


class Counter(ABC):
    @abstractmethod
    def count(self, card: int | list[int]) -> None:
        self.total_remaining: int

    @abstractmethod
    def uncount(self, card: int) -> None:
        pass

    @abstractmethod
    def probability(self, card: int) -> float:
        pass

    @abstractmethod
    def reset(self):
        pass


class PerfectCounter(Counter):
    def __init__(self, num_decks: int):
        self.num_decks = num_decks
        self.remaining = {i: self.num_decks * 4 for i in range(2, 10)}
        self.remaining[10] = self.num_decks * 16  # 10, J, Q, K
        self.remaining[11] = self.num_decks * 4  # A
        self.total_remaining = self.num_decks * 52

    def count(self, card: int | list[int]) -> None:
        if isinstance(card, list):
            for c in card:
                self.count(c)
        else:
            self.remaining[card] -= 1
            self.total_remaining -= 1

    def uncount(self, card: int) -> None:
        self.remaining[card] += 1
        self.total_remaining += 1

    def probability(self, card: int) -> float:
        return self.remaining[card] / self.total_remaining

    def reset(self):
        self.remaining = {i: self.num_decks * 4 for i in range(2, 10)}
        self.remaining[10] = self.num_decks * 16  # 10, J, Q, K
        self.remaining[11] = self.num_decks * 4  # A
        self.total_remaining = self.num_decks * 52


class HighLowCounter(Counter):
    def __init__(self, num_decks: int):
        self.num_decks = num_decks
        self.running_count = 0
        self.total_remaining = self.num_decks * 52

    def count(self, card: int | list[int]) -> None:
        if isinstance(card, list):
            for c in card:
                self.count(c)
        else:
            if card < 7:
                self.running_count += 1
            elif card > 9:
                self.running_count -= 1
            self.total_remaining -= 1

    def uncount(self, card: int) -> None:
        if card < 7:
            self.running_count -= 1
        elif card > 9:
            self.running_count += 1
        self.total_remaining += 1

    def probability(self, card: int) -> float:
        naive_num_remaining = self.total_remaining / 13
        if card < 7:
            prob = (5 * naive_num_remaining - self.running_count / 2) / (5 * self.total_remaining)
        elif card > 9:
            prob = (5 * naive_num_remaining + self.running_count / 2) / (5 * self.total_remaining)
            if card == 10:
                prob *= 4
        else:
            prob = naive_num_remaining / self.total_remaining

        return prob

    def reset(self):
        self.running_count = 0
        self.total_remaining = self.num_decks * 52


class NoneCounter(Counter):
    def __init__(self, num_decks: int):
        self.num_decks = num_decks
        self.total_remaining = num_decks * 52

    def count(self, card: int | list[int]) -> None:
        self.total_remaining -= 1

    def uncount(self, card: int) -> None:
        self.total_remaining += 1

    def probability(self, card: int) -> float:
        return 1 / 13 if card != 10 else 4 / 13

    def reset(self):
        self.total_remaining = self.num_decks * 52
