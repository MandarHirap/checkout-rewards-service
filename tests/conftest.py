import pytest

from app.config import Settings
from app.errors import AppError
from app.payments import PaymentDeclined
from app.services import Shop


class DecliningGateway:
    """Fake gateway that can be told to decline."""
    def __init__(self):
        self.decline = False

    def charge(self, *, amount_cents, idempotency_key):
        if self.decline:
            raise PaymentDeclined("card_declined")
        return f"pay_{idempotency_key}"


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=str(tmp_path / "test.db"), reward_every_n=3, coupon_percent=10)


@pytest.fixture
def gateway():
    return DecliningGateway()


@pytest.fixture
def shop(settings, gateway):
    return Shop(settings, gateway)


def cart_with(shop, *items):
    """items: (product_id, quantity) pairs. Returns cart id."""
    cid = shop.create_cart()["id"]
    for pid, qty in items:
        shop.add_item(cid, pid, qty)
    return cid


def place_orders(shop, count, product="sku-notebook"):
    return [shop.checkout(cart_with(shop, (product, 1)))[0] for _ in range(count)]


def inventory(shop, product_id):
    return next(p["inventory"] for p in shop.list_products() if p["id"] == product_id)


def error_code(callable_, *a, **kw):
    with pytest.raises(AppError) as e:
        callable_(*a, **kw)
    return e.value.code
