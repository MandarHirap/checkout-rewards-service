from conftest import cart_with, error_code, inventory


def test_invalid_items_never_enter_the_cart(shop):
    cid = shop.create_cart()["id"]
    assert error_code(shop.add_item, cid, "nope", 1) == "PRODUCT_NOT_FOUND"
    for bad in (0, -1, 101, 1.5, True, "2", None):
        assert error_code(shop.add_item, cid, "sku-tee", bad) == "INVALID_QUANTITY"
    assert error_code(shop.add_item, cid, "sku-sneakers", 4) == "INSUFFICIENT_INVENTORY"  # only 3 exist
    assert shop.get_cart(cid)["items"] == []


def test_cart_totals_and_item_lifecycle(shop):
    cid = cart_with(shop, ("sku-tee", 2), ("sku-mug", 1))
    assert shop.get_cart(cid)["subtotal_cents"] == 2 * 1999 + 1250
    assert shop.set_item_quantity(cid, "sku-tee", 1)["subtotal_cents"] == 1999 + 1250
    assert shop.remove_item(cid, "sku-mug")["subtotal_cents"] == 1999
    assert error_code(shop.remove_item, cid, "sku-mug") == "ITEM_NOT_IN_CART"
    assert error_code(shop.add_item, cid, "sku-tee", 1) == "ITEM_ALREADY_IN_CART"


def test_checkout_snapshots_order_and_decrements_inventory(shop):
    cid = cart_with(shop, ("sku-tee", 2), ("sku-mug", 1))
    order, replayed = shop.checkout(cid)
    assert not replayed
    assert order["subtotal_cents"] == order["total_cents"] == 5248 and order["discount_cents"] == 0
    assert inventory(shop, "sku-tee") == 98
    # later product changes must not rewrite history
    shop.update_product("sku-tee", price_cents=1)
    again = shop.get_order(order["id"])
    assert {i["product_id"]: i["unit_price_cents"] for i in again["items"]}["sku-tee"] == 1999
    assert again["total_cents"] == 5248


def test_retry_returns_same_order_and_charges_inventory_once(shop):
    cid = cart_with(shop, ("sku-tee", 2))
    first, _ = shop.checkout(cid)
    second, replayed = shop.checkout(cid)
    assert replayed and second["id"] == first["id"]
    assert inventory(shop, "sku-tee") == 98
    assert shop.report()["total_orders"] == 1
    assert error_code(shop.add_item, cid, "sku-mug", 1) == "CART_NOT_OPEN"


def test_empty_cart_cannot_check_out(shop):
    assert error_code(shop.checkout, shop.create_cart()["id"]) == "EMPTY_CART"


def test_inventory_shortage_at_checkout_changes_nothing(shop):
    cid = cart_with(shop, ("sku-tee", 1), ("sku-sneakers", 3))
    shop.update_product("sku-sneakers", inventory=2)     # stock drops after add-to-cart
    assert error_code(shop.checkout, cid) == "INSUFFICIENT_INVENTORY"
    assert inventory(shop, "sku-tee") == 100             # no partial decrement
    cart = shop.get_cart(cid)
    assert cart["status"] == "OPEN" and cart["checkout_ready"] is False


def test_price_change_policy_charge_current_price_but_let_client_guard(shop):
    cid = cart_with(shop, ("sku-mug", 2))
    shop.update_product("sku-mug", price_cents=1500)
    item = shop.get_cart(cid)["items"][0]
    assert item["price_changed"] and item["unit_price_at_add_cents"] == 1250 and item["unit_price_cents"] == 1500
    stale = shop.get_cart(cid)["subtotal_cents"] - 600
    assert error_code(shop.checkout, cid, None, stale) == "PRICE_CHANGED"
    order, _ = shop.checkout(cid, None, 3000)
    assert order["total_cents"] == 3000


def test_failed_payment_rolls_back_inventory_cart_and_coupon(shop, gateway):
    from conftest import place_orders
    place_orders(shop, 3)
    code = shop.generate_coupon()["code"]
    cid = cart_with(shop, ("sku-tee", 1))
    gateway.decline = True
    assert error_code(shop.checkout, cid, code) == "PAYMENT_DECLINED"
    assert inventory(shop, "sku-tee") == 100
    assert shop.get_cart(cid)["status"] == "OPEN"
    assert shop.list_coupons()[0]["status"] == "AVAILABLE"     # coupon not lost
    gateway.decline = False
    order, _ = shop.checkout(cid, code)                         # same cart + coupon now works
    assert order["discount_cents"] == 200
