import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = "shop.db"
    reward_every_n: int = 5      # n: a coupon becomes available after every nth placed order
    coupon_percent: int = 10     # x: percent off granted by each coupon
    max_qty_per_item: int = 100  # sanity cap on a single line's quantity
    currency: str = "USD"

    def __post_init__(self):
        if self.reward_every_n < 1:
            raise ValueError("reward_every_n must be >= 1")
        if not 1 <= self.coupon_percent <= 100:
            raise ValueError("coupon_percent must be between 1 and 100")


def load_settings() -> Settings:
    return Settings(
        db_path=os.getenv("SHOP_DB_PATH", "shop.db"),
        reward_every_n=int(os.getenv("REWARD_EVERY_N", "5")),
        coupon_percent=int(os.getenv("COUPON_PERCENT", "10")),
    )
