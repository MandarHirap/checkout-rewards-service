"""All business rules live here. Every state-changing operation runs inside ONE
BEGIN IMMEDIATE transaction on its own connection, so it either happens
completely or not at all, and competing operations are serialized."""
import secrets
import uuid
from contextlib import closing
from datetime import datetime, timezone

from .config import Settings
from .db import connect, init_db, transaction
from .errors import AppError
from .money import percent_discount
from .payments import FakePaymentGateway, PaymentDeclined, PaymentGateway


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_code(code):
    if code is None:
        return None
    code = code.strip().upper()
    return code or None


class Shop:
    def __init__(self, settings: Settings, payments: PaymentGateway | None = None):
        self.settings = settings
        self.payments = payments or FakePaymentGateway()
        init_db(settings.db_path)

    def _conn(self):
        return closing(connect(self.settings.db_path))

    # ------------------------------------------------------------ products
    def list_products(self):
        with self._conn() as c, transaction(c, write=False):
            return [dict(r) for r in c.execute("SELECT * FROM products ORDER BY id")]

    def update_product(self, product_id, price_cents=None, inventory=None):
        """Admin helper: change price and/or set inventory."""
        for name, v in (("price_cents", price_cents), ("inventory", inventory)):
            if v is not None and v < 0:
                raise AppError(422, "INVALID_VALUE", f"{name} must be >= 0")
        with self._conn() as c, transaction(c):
            self._product(c, product_id)
            if price_cents is not None:
                c.execute("UPDATE products SET price_cents=? WHERE id=?", (price_cents, product_id))
            if inventory is not None:
                c.execute("UPDATE products SET inventory=? WHERE id=?", (inventory, product_id))
            return dict(self._product(c, product_id))

    @staticmethod
    def _product(c, product_id):
        row = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if row is None:
            raise AppError(404, "PRODUCT_NOT_FOUND", f"Product '{product_id}' does not exist",
                           {"product_id": product_id})
        return row

    # --------------------------------------------------------------- carts
    def create_cart(self):
        cart_id = uuid.uuid4().hex
        with self._conn() as c, transaction(c):
            c.execute("INSERT INTO carts (id, status, created_at) VALUES (?, 'OPEN', ?)",
                      (cart_id, _now()))
            return self._cart_view(c, cart_id)

    def get_cart(self, cart_id):
        with self._conn() as c, transaction(c, write=False):
            return self._cart_view(c, cart_id)

    def add_item(self, cart_id, product_id, quantity):
        self._check_quantity(quantity)
        with self._conn() as c, transaction(c):
            self._open_cart(c, cart_id)
            product = self._product(c, product_id)
            if c.execute("SELECT 1 FROM cart_items WHERE cart_id=? AND product_id=?",
                         (cart_id, product_id)).fetchone():
                raise AppError(409, "ITEM_ALREADY_IN_CART",
                               "Product is already in the cart; use PATCH to change its quantity",
                               {"product_id": product_id})
            self._check_stock(product, quantity)
            c.execute("INSERT INTO cart_items VALUES (?, ?, ?, ?)",
                      (cart_id, product_id, quantity, product["price_cents"]))
            return self._cart_view(c, cart_id)

    def set_item_quantity(self, cart_id, product_id, quantity):
        self._check_quantity(quantity)
        with self._conn() as c, transaction(c):
            self._open_cart(c, cart_id)
            self._require_item(c, cart_id, product_id)
            product = self._product(c, product_id)
            self._check_stock(product, quantity)
            c.execute("UPDATE cart_items SET quantity=? WHERE cart_id=? AND product_id=?",
                      (quantity, cart_id, product_id))
            return self._cart_view(c, cart_id)

    def remove_item(self, cart_id, product_id):
        with self._conn() as c, transaction(c):
            self._open_cart(c, cart_id)
            self._require_item(c, cart_id, product_id)
            c.execute("DELETE FROM cart_items WHERE cart_id=? AND product_id=?", (cart_id, product_id))
            return self._cart_view(c, cart_id)

    def _check_quantity(self, quantity):
        mx = self.settings.max_qty_per_item
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= mx:
            raise AppError(422, "INVALID_QUANTITY", f"quantity must be an integer between 1 and {mx}",
                           {"quantity": quantity})

    @staticmethod
    def _check_stock(product, quantity):
        # Soft check only: inventory is NOT reserved by carts. The authoritative
        # check happens atomically at checkout.
        if quantity > product["inventory"]:
            raise AppError(409, "INSUFFICIENT_INVENTORY", "Requested quantity exceeds available inventory",
                           {"product_id": product["id"], "requested": quantity,
                            "available": product["inventory"]})

    @staticmethod
    def _cart_row(c, cart_id):
        row = c.execute("SELECT * FROM carts WHERE id=?", (cart_id,)).fetchone()
        if row is None:
            raise AppError(404, "CART_NOT_FOUND", f"Cart '{cart_id}' does not exist", {"cart_id": cart_id})
        return row

    def _open_cart(self, c, cart_id):
        row = self._cart_row(c, cart_id)
        if row["status"] != "OPEN":
            raise AppError(409, "CART_NOT_OPEN", "Cart has already been checked out and can no longer change",
                           {"cart_id": cart_id, "order_id": row["order_id"]})
        return row

    @staticmethod
    def _require_item(c, cart_id, product_id):
        if not c.execute("SELECT 1 FROM cart_items WHERE cart_id=? AND product_id=?",
                         (cart_id, product_id)).fetchone():
            raise AppError(404, "ITEM_NOT_IN_CART", "Product is not in this cart",
                           {"cart_id": cart_id, "product_id": product_id})

    def _cart_view(self, c, cart_id):
        cart = self._cart_row(c, cart_id)
        cur = self.settings.currency
        if cart["status"] == "CHECKED_OUT":
            # Show what was actually bought, not today's prices.
            items = [{
                "product_id": r["product_id"], "name": r["product_name"], "quantity": r["quantity"],
                "unit_price_cents": r["unit_price_cents"], "line_total_cents": r["line_total_cents"],
            } for r in c.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY product_id",
                                 (cart["order_id"],))]
            return {"id": cart_id, "status": "CHECKED_OUT", "order_id": cart["order_id"], "items": items,
                    "subtotal_cents": sum(i["line_total_cents"] for i in items), "currency": cur,
                    "checkout_ready": False}
        rows = c.execute("""
            SELECT ci.product_id, ci.quantity, ci.unit_price_at_add_cents,
                   p.name, p.price_cents, p.inventory
            FROM cart_items ci JOIN products p ON p.id = ci.product_id
            WHERE ci.cart_id=? ORDER BY ci.rowid""", (cart_id,)).fetchall()
        items = []
        for r in rows:
            items.append({
                "product_id": r["product_id"], "name": r["name"], "quantity": r["quantity"],
                "unit_price_cents": r["price_cents"],                      # CURRENT price
                "line_total_cents": r["price_cents"] * r["quantity"],
                "unit_price_at_add_cents": r["unit_price_at_add_cents"],
                "price_changed": r["price_cents"] != r["unit_price_at_add_cents"],
                "available_inventory": r["inventory"],
                "sufficient_inventory": r["quantity"] <= r["inventory"],
            })
        return {"id": cart_id, "status": "OPEN", "order_id": None, "items": items,
                "subtotal_cents": sum(i["line_total_cents"] for i in items), "currency": cur,
                "checkout_ready": bool(items) and all(i["sufficient_inventory"] for i in items)}

    # ------------------------------------------------------------ checkout
    def checkout(self, cart_id, coupon_code=None, expected_subtotal_cents=None):
        """Returns (order, replayed). The cart id is the idempotency key."""
        code = _normalize_code(coupon_code)
        with self._conn() as c, transaction(c):
            cart = self._cart_row(c, cart_id)

            # --- retry of a completed checkout: return the original order ---
            if cart["status"] == "CHECKED_OUT":
                order = self._order_view(c, cart["order_id"])
                if order["coupon_code"] != code:
                    raise AppError(409, "CHECKOUT_REQUEST_MISMATCH",
                                   "Cart was already checked out with a different coupon",
                                   {"order_id": order["id"], "original_coupon_code": order["coupon_code"]})
                return order, True

            cart_items = c.execute("""
                SELECT ci.product_id, ci.quantity, p.name, p.price_cents, p.inventory
                FROM cart_items ci JOIN products p ON p.id = ci.product_id
                WHERE ci.cart_id=? ORDER BY ci.product_id""", (cart_id,)).fetchall()
            if not cart_items:
                raise AppError(422, "EMPTY_CART", "Cannot check out an empty cart", {"cart_id": cart_id})

            # --- validate stock against CURRENT inventory; report every shortage ---
            shortages = [{"product_id": i["product_id"], "requested": i["quantity"],
                          "available": i["inventory"]} for i in cart_items if i["quantity"] > i["inventory"]]
            if shortages:
                raise AppError(409, "INSUFFICIENT_INVENTORY",
                               "Some items are no longer available in the requested quantity",
                               {"items": shortages})

            # --- price at CURRENT prices ---
            subtotal = sum(i["price_cents"] * i["quantity"] for i in cart_items)
            if expected_subtotal_cents is not None and expected_subtotal_cents != subtotal:
                raise AppError(409, "PRICE_CHANGED", "Cart total differs from the amount the client expected",
                               {"expected_subtotal_cents": expected_subtotal_cents,
                                "current_subtotal_cents": subtotal})

            # --- coupon (validated here, consumed below inside the same transaction) ---
            coupon, discount = None, 0
            if code:
                coupon = c.execute("SELECT * FROM coupons WHERE code=?", (code,)).fetchone()
                if coupon is None:
                    raise AppError(422, "COUPON_NOT_FOUND", "Coupon code is not valid", {"coupon_code": code})
                if coupon["status"] != "AVAILABLE":
                    raise AppError(409, "COUPON_ALREADY_REDEEMED", "Coupon has already been redeemed",
                                   {"coupon_code": code})
                discount = percent_discount(subtotal, coupon["percent_off"])
            total = subtotal - discount

            # --- claim the cart (conditional update: only one checkout can win) ---
            order_id = uuid.uuid4().hex
            claimed = c.execute("UPDATE carts SET status='CHECKED_OUT', order_id=? WHERE id=? AND status='OPEN'",
                                (order_id, cart_id)).rowcount
            if claimed != 1:
                raise AppError(409, "CART_NOT_OPEN", "Cart is no longer open", {"cart_id": cart_id})

            # --- decrement inventory (conditional update; DB CHECK is the last backstop) ---
            for i in cart_items:
                done = c.execute("UPDATE products SET inventory = inventory - ? WHERE id=? AND inventory >= ?",
                                 (i["quantity"], i["product_id"], i["quantity"])).rowcount
                if done != 1:
                    raise AppError(409, "INSUFFICIENT_INVENTORY", "Inventory changed during checkout",
                                   {"items": [{"product_id": i["product_id"], "requested": i["quantity"]}]})

            # --- payment. A decline raises -> everything above rolls back. ---
            try:
                payment_ref = self.payments.charge(amount_cents=total, idempotency_key=cart_id)
            except PaymentDeclined as exc:
                raise AppError(402, "PAYMENT_DECLINED", "Payment was declined; nothing was changed",
                               {"reason": str(exc)}) from exc

            # --- write the immutable order snapshot ---
            c.execute("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (order_id, cart_id, subtotal, discount, total, code,
                       coupon["percent_off"] if coupon else None, payment_ref, _now()))
            c.executemany("INSERT INTO order_items VALUES (?, ?, ?, ?, ?, ?)",
                          [(order_id, i["product_id"], i["name"], i["price_cents"], i["quantity"],
                            i["price_cents"] * i["quantity"]) for i in cart_items])

            # --- consume the coupon last (conditional update: redeem exactly once) ---
            if coupon:
                redeemed = c.execute("""UPDATE coupons SET status='REDEEMED', redeemed_at=?, redeemed_order_id=?
                                        WHERE code=? AND status='AVAILABLE'""",
                                     (_now(), order_id, code)).rowcount
                if redeemed != 1:
                    raise AppError(409, "COUPON_ALREADY_REDEEMED", "Coupon has already been redeemed",
                                   {"coupon_code": code})
            return self._order_view(c, order_id), False

    # -------------------------------------------------------------- orders
    def get_order(self, order_id):
        with self._conn() as c, transaction(c, write=False):
            return self._order_view(c, order_id)

    def list_orders(self):
        with self._conn() as c, transaction(c, write=False):
            ids = [r["id"] for r in c.execute("SELECT id FROM orders ORDER BY rowid")]
            return [self._order_view(c, i) for i in ids]

    def _order_view(self, c, order_id):
        o = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if o is None:
            raise AppError(404, "ORDER_NOT_FOUND", f"Order '{order_id}' does not exist", {"order_id": order_id})
        items = [{"product_id": r["product_id"], "name": r["product_name"], "quantity": r["quantity"],
                  "unit_price_cents": r["unit_price_cents"], "line_total_cents": r["line_total_cents"]}
                 for r in c.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY product_id", (order_id,))]
        return {"id": o["id"], "cart_id": o["cart_id"], "status": "PLACED", "items": items,
                "subtotal_cents": o["subtotal_cents"], "discount_cents": o["discount_cents"],
                "total_cents": o["total_cents"], "coupon_code": o["coupon_code"],
                "coupon_percent": o["coupon_percent"], "currency": self.settings.currency,
                "payment_ref": o["payment_ref"], "created_at": o["created_at"]}

    # ------------------------------------------------------------- coupons
    def generate_coupon(self):
        """Create a coupon for the oldest unrewarded milestone, if one is reached.

        milestones reached = floor(placed_orders / n). Coupons are generated in
        order (1, 2, 3...), one per call. UNIQUE(milestone) is the backstop that
        makes double generation impossible even without the transaction."""
        n = self.settings.reward_every_n
        with self._conn() as c, transaction(c):
            placed = c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            reached = placed // n
            last = c.execute("SELECT COALESCE(MAX(milestone), 0) FROM coupons").fetchone()[0]
            if reached <= last:
                raise AppError(409, "NO_ELIGIBLE_MILESTONE",
                               "No unrewarded order milestone has been reached",
                               {"orders_placed": placed, "reward_every_n": n, "milestones_reached": reached,
                                "coupons_generated": last, "orders_until_next_milestone": n - placed % n})
            milestone = last + 1
            code = f"REWARD-{milestone}-{secrets.token_hex(4).upper()}"
            c.execute("INSERT INTO coupons (code, milestone, percent_off, status, created_at) "
                      "VALUES (?, ?, ?, 'AVAILABLE', ?)",
                      (code, milestone, self.settings.coupon_percent, _now()))
            return self._coupon_view(c.execute("SELECT * FROM coupons WHERE code=?", (code,)).fetchone())

    def list_coupons(self):
        with self._conn() as c, transaction(c, write=False):
            return [self._coupon_view(r) for r in c.execute("SELECT * FROM coupons ORDER BY milestone")]

    @staticmethod
    def _coupon_view(r):
        return {"code": r["code"], "milestone": r["milestone"], "percent_off": r["percent_off"],
                "status": r["status"], "created_at": r["created_at"], "redeemed_at": r["redeemed_at"],
                "redeemed_order_id": r["redeemed_order_id"]}

    # -------------------------------------------------------------- report
    def report(self):
        """Read-only, computed from orders/order_items/coupons in one snapshot."""
        n = self.settings.reward_every_n
        with self._conn() as c, transaction(c, write=False):
            o = c.execute("""SELECT COUNT(*) n, COALESCE(SUM(subtotal_cents),0) gross,
                             COALESCE(SUM(discount_cents),0) disc, COALESCE(SUM(total_cents),0) net
                             FROM orders""").fetchone()
            by_product = [dict(r) for r in c.execute("""
                SELECT p.id AS product_id, p.name,
                       COALESCE(SUM(oi.quantity), 0) AS quantity_sold,
                       COALESCE(SUM(oi.line_total_cents), 0) AS gross_cents
                FROM products p LEFT JOIN order_items oi ON oi.product_id = p.id
                GROUP BY p.id ORDER BY p.id""")]
            cp = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM coupons GROUP BY status")}
            generated = sum(cp.values())
            return {
                "total_orders": o["n"],
                "quantity_sold_by_product": by_product,
                "gross_revenue_cents": o["gross"],
                "total_discounts_cents": o["disc"],
                "net_revenue_cents": o["net"],
                "currency": self.settings.currency,
                "coupons": {"generated": generated, "available": cp.get("AVAILABLE", 0),
                            "redeemed": cp.get("REDEEMED", 0),
                            "milestones_reached_not_yet_generated": max(o["n"] // n - generated, 0)},
            }
