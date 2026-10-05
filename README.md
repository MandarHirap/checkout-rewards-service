# Checkout & Rewards Service

Python 3.11+ · FastAPI · SQLite (stdlib `sqlite3`). Design rationale, invariants and trade-offs are in **[DECISIONS.md](DECISIONS.md)**.

## Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# start (creates shop.db and seeds 6 products on first start)
REWARD_EVERY_N=5 COUPON_PERCENT=10 uvicorn app.main:app_factory --factory --port 8000
# interactive docs: http://localhost:8000/docs

# tests
python -m pytest -q
```

Config (env vars): `SHOP_DB_PATH` (default `shop.db`), `REWARD_EVERY_N` = **n** (default 5), `COUPON_PERCENT` = **x** (default 10).
Delete `shop.db*` to reset. Seed products: `sku-tee`, `sku-mug`, `sku-notebook`, `sku-bottle`, `sku-headphones`, and **`sku-sneakers` (inventory 3)**.

## Conventions

- Money is an **integer number of cents** (`*_cents`) plus `currency`.
- Every error: `{"error": {"code": "<STABLE_CODE>", "message": "...", "details": {...}}}`. Clients should switch on `code`.
- **Admin endpoints** (`/admin/*`) have no auth, by assignment; they are the operations I treat as administrative.

## Endpoints

| Method & path                                                                          | Success                                                             | Notes / important errors                                                                                                                                                                                                                                                                                   |
| -------------------------------------------------------------------------------------- | ------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /products`                                                                        | 200                                                                 | list of products                                                                                                                                                                                                                                                                                           |
| `POST /carts`                                                                          | 201                                                                 | creates an empty cart                                                                                                                                                                                                                                                                                      |
| `GET /carts/{id}`                                                                      | 200                                                                 | items at **current** prices, `subtotal_cents`, per-item `price_changed`, `sufficient_inventory`, and `checkout_ready`. `404 CART_NOT_FOUND`                                                                                                                                                                |
| `POST /carts/{id}/items` `{product_id, quantity}`                                      | 201 → cart                                                          | `404 PRODUCT_NOT_FOUND`, `422 INVALID_QUANTITY` (integer 1–100), `422 VALIDATION_ERROR` (wrong types, e.g. `"2"`, `1.5`), `409 INSUFFICIENT_INVENTORY`, `409 ITEM_ALREADY_IN_CART`, `409 CART_NOT_OPEN`                                                                                                    |
| `PATCH /carts/{id}/items/{product_id}` `{quantity}`                                    | 200 → cart                                                          | sets the absolute quantity (idempotent). `404 ITEM_NOT_IN_CART`, plus the quantity/stock/open errors above                                                                                                                                                                                                 |
| `DELETE /carts/{id}/items/{product_id}`                                                | 200 → cart                                                          | `404 ITEM_NOT_IN_CART`, `409 CART_NOT_OPEN`                                                                                                                                                                                                                                                                |
| `POST /carts/{id}/checkout` `{coupon_code?, expected_subtotal_cents?}` (body optional) | **201** new order · **200** + `Idempotent-Replay: true` for a retry | `422 EMPTY_CART`, `409 INSUFFICIENT_INVENTORY` (lists every short item), `409 PRICE_CHANGED`, `422 COUPON_NOT_FOUND`, `409 COUPON_ALREADY_REDEEMED`, `409 CHECKOUT_REQUEST_MISMATCH` (retry with a different coupon), `402 PAYMENT_DECLINED`. **Any failure leaves inventory, cart and coupon untouched.** |
| `GET /orders/{id}`                                                                     | 200                                                                 | snapshot: items with name + unit price at purchase, `subtotal_cents`, `discount_cents`, `total_cents`, `coupon_code`, `coupon_percent`. `404 ORDER_NOT_FOUND`                                                                                                                                              |
| `POST /admin/coupons/generate`                                                         | 201                                                                 | creates a coupon for the oldest unrewarded milestone. `409 NO_ELIGIBLE_MILESTONE` (details show progress)                                                                                                                                                                                                  |
| `GET /admin/coupons`                                                                   | 200                                                                 | all coupons + status (`AVAILABLE`/`REDEEMED`)                                                                                                                                                                                                                                                              |
| `GET /admin/orders`                                                                    | 200                                                                 | all orders (for reconciling the report)                                                                                                                                                                                                                                                                    |
| `GET /admin/report`                                                                    | 200                                                                 | read-only: `total_orders`, `quantity_sold_by_product`, `gross_revenue_cents`, `total_discounts_cents`, `net_revenue_cents`, `coupons{generated,available,redeemed,...}`                                                                                                                                    |
| `PATCH /admin/products/{id}` `{price_cents?, inventory?}`                              | 200                                                                 | admin helper so price/stock changes can be demonstrated                                                                                                                                                                                                                                                    |

### Example session

```bash
CID=$(curl -s -X POST localhost:8000/carts | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -s -X POST localhost:8000/carts/$CID/items -H 'content-type: application/json' \
     -d '{"product_id":"sku-sneakers","quantity":1}'
curl -s -i -X POST localhost:8000/carts/$CID/checkout        # 201 Created
curl -s -i -X POST localhost:8000/carts/$CID/checkout        # 200 OK, Idempotent-Replay: true, same order
curl -s localhost:8000/admin/report
curl -s -X POST localhost:8000/admin/coupons/generate        # 409 until n orders are placed
```

## Tests

`tests/test_concurrency.py` releases threads together with a barrier and asserts: no overselling of the 3-unit product under 12 racing checkouts; 12 simultaneous retries of one checkout create exactly one order; 6 carts racing for one coupon → exactly one wins and the losers' stock/carts/coupon are untouched; concurrent coupon generation yields one coupon; mixed load keeps the report reconciled. Other files cover validation, rounding, rollback on payment failure, price/stock changes, and the report.

## Status

Approx. time spent: **~5 hours**. Everything required is implemented; deferred items are listed in DECISIONS.md.
