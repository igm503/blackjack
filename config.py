from dataclasses import dataclass


@dataclass
class GameConfig:
    min_bet: int
    num_decks: int
    dealer_hits_soft_17: bool
    double_after_split: bool
    double_on: int
    resplit_limit: int
    resplit_aces: bool
    hit_split_aces: bool
    surrender: int
    always_play: bool
    blackjack_payout: float
