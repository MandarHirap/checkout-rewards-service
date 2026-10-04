"""Competing / repeated operations. Threads are released together via a barrier
so requests genuinely overlap; each service call uses its own DB connection."""
import threading
from concurrent.futures import ThreadPoolExecutor

from app.errors import AppError
from conftest import cart_with, inventory, place_orders

THREADS = 12


def race(fns):
    barrier = threading.Barrier(len(fns))

    def run(fn):
        barrier.wait()
        try:
            return ("ok", fn())
        except AppError as e:
            return ("err", e.code)

    with ThreadPoolExecutor(len(fns)) as ex:
        return list(ex.map(run, fns))


def test_concurrent_checkouts_never_oversell_limited_stock(shop):
    carts = [cart_with(shop, ("sku-sneakers", 1)) for _ in range(THREADS)]   # stock is 3
    results = race([lambda c=c: shop.checkout(c) for c in carts])
    wins = [r for r in results if r[0] == "ok"]
    losses = [r for r in results if r[0] == "err"]
    assert len(wins) == 3
    assert {code for _, code in losses} == {"INSUFFICIENT_INVENTORY"} and len(losses) == THREADS - 3
    assert inventory(shop, "sku-sneakers") == 0
    sold = {p["product_id"]: p["quantity_sold"] for p in shop.report()["quantity_sold_by_product"]}
    assert sold["sku-sneakers"] == 3


def test_concurrent_retries_of_one_checkout_create_exactly_one_order(shop):
    cid = cart_with(shop, ("sku-tee", 2))
    results = race([lambda: shop.checkout(cid) for _ in range(THREADS)])
    assert all(kind == "ok" for kind, _ in results)
    assert len({order["id"] for _, (order, _) in results}) == 1
    assert sum(1 for _, (_, replayed) in results if not replayed) == 1      # exactly one real checkout
    assert inventory(shop, "sku-tee") == 98
    assert shop.report()["total_orders"] == 1


def test_two_checkouts_racing_for_one_coupon_exactly_one_wins_and_loser_is_untouched(shop):
    place_orders(shop, 3)
    code = shop.generate_coupon()["code"]
    carts = [cart_with(shop, ("sku-tee", 1)) for _ in range(6)]
    before = inventory(shop, "sku-tee")
    results = race([lambda c=c: shop.checkout(c, code) for c in carts])
    wins = [r for r in results if r[0] == "ok"]
    assert len(wins) == 1
    assert [r[1] for r in results if r[0] == "err"] == ["COUPON_ALREADY_REDEEMED"] * 5
    assert inventory(shop, "sku-tee") == before - 1          # losers did not consume stock
    losers_open = [shop.get_cart(c)["status"] for c in carts].count("OPEN")
    assert losers_open == 5
    assert [c["status"] for c in shop.list_coupons()] == ["REDEEMED"]
    assert sum(1 for o in shop.list_orders() if o["coupon_code"] == code) == 1


def test_concurrent_coupon_generation_creates_one_coupon_per_milestone(shop):
    place_orders(shop, 3)                                    # exactly one milestone reached
    results = race([shop.generate_coupon for _ in range(THREADS)])
    assert sum(1 for kind, _ in results if kind == "ok") == 1
    assert {code for kind, code in results if kind == "err"} == {"NO_ELIGIBLE_MILESTONE"}
    assert len(shop.list_coupons()) == 1


def test_mixed_load_keeps_report_consistent_with_orders(shop):
    shop.update_product("sku-mug", inventory=5)
    carts = [cart_with(shop, ("sku-mug", 1)) for _ in range(10)]
    race([lambda c=c: shop.checkout(c) for c in carts] + [shop.report for _ in range(4)]
         + [shop.generate_coupon for _ in range(2)])
    rep, orders = shop.report(), shop.list_orders()
    assert rep["total_orders"] == len(orders) == 5
    assert rep["gross_revenue_cents"] == sum(o["subtotal_cents"] for o in orders) == 5 * 1250
    assert inventory(shop, "sku-mug") == 0
