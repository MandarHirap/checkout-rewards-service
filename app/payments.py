"""Payment abstraction. No real integration: the fake always approves.

The gateway is called with an idempotency key (the cart id) so that a real
provider would also de-duplicate a retried charge.
"""
from typing import Protocol


class PaymentDeclined(Exception):
    pass


class PaymentGateway(Protocol):
    def charge(self, *, amount_cents: int, idempotency_key: str) -> str:
        """Return a payment reference, or raise PaymentDeclined."""


class FakePaymentGateway:
    def charge(self, *, amount_cents: int, idempotency_key: str) -> str:
        return f"fake_pay_{idempotency_key}"
