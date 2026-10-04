from conftest import cart_with, error_code, place_orders


def test_no_coupon_until_milestone_then_only_one_per_milestone(shop):
    assert error_code(shop.generate_coupon) == "NO_ELIGIBLE_MILESTONE"
    place_orders(shop, 2)
    assert error_code(shop.generate_coupon) == "NO_ELIGIBLE_MILESTONE"
    place_orders(shop, 1)
    first = shop.generate_coupon()
    assert first["milestone"] == 1 and first["percent_off"] == 10 and first["status"] == "AVAILABLE"
    assert error_code(shop.generate_coupon) == "NO_ELIGIBLE_MILESTONE"     # same milestone, no repeat


def test_missed_milestones_are_generated_one_per_call_in_order(shop):
    place_orders(shop, 6)
    assert [shop.generate_coupon()["milestone"] for _ in range(2)] == [1, 2]
    assert error_code(shop.generate_coupon) == "NO_ELIGIBLE_MILESTONE"


def test_coupon_applies_discount_and_redeems_once(shop):
    place_orders(shop, 3)
    code = shop.generate_coupon()["code"]
    order, _ = shop.checkout(cart_with(shop, ("sku-tee", 1)), code.lower())   # case/space tolerant
    assert (order["subtotal_cents"], order["discount_cents"], order["total_cents"]) == (1999, 200, 1799)
    assert order["coupon_percent"] == 10
    other = cart_with(shop, ("sku-mug", 1))
    assert error_code(shop.checkout, other, code) == "COUPON_ALREADY_REDEEMED"
    assert error_code(shop.checkout, other, "BOGUS") == "COUPON_NOT_FOUND"
    assert shop.get_cart(other)["status"] == "OPEN"


def test_retry_with_different_coupon_is_a_conflict_not_a_second_order(shop):
    place_orders(shop, 3)
    code = shop.generate_coupon()["code"]
    cid = cart_with(shop, ("sku-tee", 1))
    shop.checkout(cid, code)
    assert shop.checkout(cid, code)[1] is True
    assert error_code(shop.checkout, cid, None) == "CHECKOUT_REQUEST_MISMATCH"


def test_report_reconciles_with_orders_and_coupons_and_is_read_only(shop):
    place_orders(shop, 3)
    code = shop.generate_coupon()["code"]
    shop.checkout(cart_with(shop, ("sku-tee", 2), ("sku-mug", 1)), code)
    before = shop.report()
    assert before == shop.report() == shop.report()          # repeated reads: no mutation, same answer
    orders = shop.list_orders()
    assert before["total_orders"] == len(orders) == 4
    assert before["gross_revenue_cents"] == sum(o["subtotal_cents"] for o in orders)
    assert before["total_discounts_cents"] == sum(o["discount_cents"] for o in orders)
    assert before["net_revenue_cents"] == before["gross_revenue_cents"] - before["total_discounts_cents"]
    assert before["net_revenue_cents"] == sum(o["total_cents"] for o in orders)
    sold = {p["product_id"]: p["quantity_sold"] for p in before["quantity_sold_by_product"]}
    assert sold["sku-tee"] == 2 and sold["sku-mug"] == 1 and sold["sku-notebook"] == 3
    coupons = shop.list_coupons()
    assert before["coupons"] == {"generated": 1, "available": 0, "redeemed": 1,
                                 "milestones_reached_not_yet_generated": 0}
    assert len(coupons) == 1 and coupons[0]["status"] == "REDEEMED"
