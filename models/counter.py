from abc import ABC, abstractmethod
from collections import defaultdict


class Counter(ABC):
    @abstractmethod
    def count(self, card: int) -> None:
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
        self.remaining = [0] * 12
        for i in range(2, 10):
            self.remaining[i] = self.num_decks * 4
        self.remaining[10] = self.num_decks * 16  # 10, J, Q, K
        self.remaining[11] = self.num_decks * 4  # A
        self.total_remaining = self.num_decks * 52

        if __debug__:
            self.counts = defaultdict(int)

    def count(self, card: int) -> None:
        self.remaining[card] -= 1
        self.total_remaining -= 1

        if __debug__:
            self.counts[card] += 1
            if self.remaining[card] < 0:
                raise ValueError(f"Negative remaining: {self.remaining[card]}")
            if self.total_remaining < 0:
                raise ValueError(f"Negative total remaining: {self.total_remaining}")

    def uncount(self, card: int) -> None:
        self.remaining[card] += 1
        self.total_remaining += 1

        if __debug__:
            self.counts[card] -= 1
            if self.counts[card] < 0:
                raise ValueError(f"Negative counts: {self.counts[card]}")

    def probability(self, card: int) -> float:
        return self.remaining[card] / self.total_remaining

    def reset(self):
        for i in range(2, 10):
            self.remaining[i] = self.num_decks * 4
        self.remaining[10] = self.num_decks * 16  # 10, J, Q, K
        self.remaining[11] = self.num_decks * 4  # A
        self.total_remaining = self.num_decks * 52


class HighLowCounter(Counter):
    def __init__(self, num_decks: int):
        self.num_decks = num_decks
        self.running_count = 0
        self.total_remaining = self.num_decks * 52

    def count(self, card: int) -> None:
        if card < 7:
            self.running_count += 1
        elif card > 9:
            self.running_count -= 1
        self.total_remaining -= 1

        if __debug__:
            if abs(self.running_count) / 2 > 5 * self.total_remaining / 13:
                raise ValueError(f"Running count too large: {self.running_count}")

    def uncount(self, card: int) -> None:
        if card < 7:
            self.running_count -= 1
        elif card > 9:
            self.running_count += 1
        self.total_remaining += 1

        if __debug__:
            if abs(self.running_count) / 2 > 5 * self.total_remaining / 13:
                raise ValueError(f"Running count too large: {self.running_count}")

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

    def count(self, card: int) -> None:
        self.total_remaining -= 1

    def uncount(self, card: int) -> None:
        self.total_remaining += 1

    def probability(self, card: int) -> float:
        return 1 / 13 if card != 10 else 4 / 13

    def reset(self):
        self.total_remaining = self.num_decks * 52
