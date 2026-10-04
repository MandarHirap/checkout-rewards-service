"""Money is always an integer number of minor units (cents). No floats anywhere."""


def percent_discount(subtotal_cents: int, percent: int) -> int:
    """Discount = subtotal * percent / 100, rounded half-up to the nearest cent.

    Pure integer arithmetic, so it is deterministic. Capped at the subtotal so
    an order total can never go negative, whatever the percent is.
    """
    if subtotal_cents < 0 or not 0 <= percent <= 100:
        raise ValueError("subtotal must be >= 0 and percent within 0..100")
    discount = (subtotal_cents * percent + 50) // 100
    return min(discount, subtotal_cents)
