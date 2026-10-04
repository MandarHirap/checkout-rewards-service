# DECISIONS

## 1. System invariants

| # | Invariant | Where it is enforced |
|---|---|---|
| I1 | Inventory never goes negative; units sold ≤ units available. | Checkout checks stock and decrements with `UPDATE … SET inventory = inventory - q WHERE inventory >= q` (rowcount checked), all inside one write transaction. Backstop: `CHECK (inventory >= 0)`. |
| I2 | A cart produces at most one order; a retry never creates a second one or decrements stock twice. | `carts.status` claimed with `UPDATE … WHERE status='OPEN'`; `orders.cart_id UNIQUE`; replay path returns the stored order. |
| I3 | Checkout is all-or-nothing: order, inventory decrement, cart close and coupon redemption commit together or not at all. | Single `BEGIN IMMEDIATE … COMMIT`; any exception (incl. payment decline) rolls back. |
| I4 | A coupon is redeemed at most once, and is never consumed by a failed checkout. | `UPDATE coupons SET status='REDEEMED' … WHERE status='AVAILABLE'` (rowcount checked) in the same transaction as the order; `redeemed_order_id UNIQUE`; partial unique index on `orders(coupon_code)`. |
| I5 | At most one coupon per milestone. | `coupons.milestone UNIQUE`, and generation reads order count + max milestone inside one write transaction. |
| I6 | An order is an immutable snapshot (names, unit prices, quantities, subtotal, discount, total, coupon %). | `order_items` copies name/price; `orders` stores amounts; nothing joins back to `products` to explain an order. |
| I7 | `total = subtotal − discount`, `0 ≤ discount ≤ subtotal`. | `percent_discount` caps; `CHECK` constraints on `orders`. |
| I8 | Report is derived data: it reconciles with orders/coupons and reading it never mutates. | Computed with `SELECT`s in one read snapshot; no counters are stored that could drift. |

## 2. Ambiguities and the semantics I chose

| Ambiguity | Choice |
|---|---|
| "Every nth order" – which orders count? | Every successfully placed order (including ones that used a coupon). Milestones reached = `floor(orders / n)`. |
| Coupon generated automatically or on admin request? | On admin request only (`POST /admin/coupons/generate`), as specified. One coupon per call; if several milestones were missed, calls generate them oldest-first. |
| Who can use a coupon? | Anyone holding the code (bearer coupon). It is not tied to a customer – the spec has no customer identity. |
| What does x% apply to? | The whole cart subtotal; one coupon per order; no stacking. |
| Coupon expiry? | None. Deferred. |
| Rounding of the discount | Half-up to the cent, integer arithmetic, capped at subtotal. |
| Price changes between add-to-cart and checkout | Cart view always shows **current** price and flags `price_changed` (with the price at add time). Checkout charges the **current** price. Optional `expected_subtotal_cents` lets a client assert what the customer saw → `409 PRICE_CHANGED` instead of a surprise. |
| Stock changes between add-to-cart and checkout | Carts do **not** reserve stock. Add/update does a soft check; checkout does the authoritative atomic check and fails the whole checkout with `409 INSUFFICIENT_INVENTORY` listing every short item. The cart stays open so the client can adjust. |
| Retry with a different body | Retry with the same coupon (or none) → original order, `200`. Retry with a different coupon → `409 CHECKOUT_REQUEST_MISMATCH` (it's not the same request, and we won't silently change what was charged). |
| Empty cart | `422 EMPTY_CART`. |
| Valid quantity | Integer 1..100. Strict typing: `"2"`, `1.5`, `true` are rejected. |
| Which operations are admin? | Everything under `/admin/*`: coupon generate/list, order list, report, product patch. No auth, by assignment. |

## 3. Material design decisions

### Decision: SQLite + one write transaction per operation (not in-memory locks)
**Context:** Need overlapping requests to preserve invariants, and ideally the same reasoning should carry to Postgres.
**Options:** (a) in-memory dicts with a global `threading.Lock`; (b) in-memory with per-product locks; (c) optimistic concurrency with version columns and retry; (d) SQLite/SQL with `BEGIN IMMEDIATE`.
**Choice:** (d). Each service call opens its own connection and runs inside one transaction.
**Why:** (a)/(b) work only in one process, lose data on restart, and lock-ordering for multi-product carts is easy to get wrong. (c) is the right shape for Postgres but needs retry plumbing. (d) gives atomicity + rollback for free (needed for I3/I4) and SQL constraints as a second line of defence.
**Consequences:** All writers are serialized (single-writer throughput ceiling, fine here, not at scale). The conditional `UPDATE … WHERE` + rowcount checks are technically redundant under `BEGIN IMMEDIATE`, but I kept them on purpose: they are what keeps the code correct under Postgres `READ COMMITTED`, where the write lock is not taken up front.

### Decision: The cart id is the idempotency key (no `Idempotency-Key` header)
**Context:** Clients retry checkout after timeouts.
**Options:** (a) require an `Idempotency-Key` header and store key→response; (b) use cart state as the key; (c) return `409` on the second attempt.
**Choice:** (b): once a cart is `CHECKED_OUT` it points to its order; a repeated `POST /carts/{id}/checkout` returns that order with `200` + `Idempotent-Replay: true` (first call is `201`).
**Why:** The domain already has a natural key – "a cart is checked out once" is itself a requirement – so a separate key store adds a table and TTL logic without adding safety. (c) is wrong for the timeout case: the client can't tell whether *its* attempt succeeded.
**Consequences:** Simple and race-proof (the cart claim is a conditional update). Weaker than a header if one day a cart could be re-opened or checked out for several payments; then I'd add explicit keys. A retry that changes the coupon is rejected rather than ignored.

### Decision: Price at checkout time, with an optional client guard; no stock reservation
**Context:** Spec requires an explicit policy for price/availability drift.
**Options:** lock price at add time; reject checkout on any price change; charge current price; reserve inventory when added to cart.
**Choice:** Charge current price; surface drift in the cart view; optional `expected_subtotal_cents` for clients that want a hard guarantee. No reservations.
**Why:** Locking prices lets stale carts buy at old prices forever; always-reject makes every price edit break every open cart. Reservations need expiry/cleanup jobs and let abandoned carts block real buyers, which is a bigger risk than the occasional stock failure at checkout.
**Consequences:** A customer can hit a checkout-time stock failure. Mitigated by `checkout_ready`/`sufficient_inventory` in the cart view and a precise error.

### Decision: Coupon is consumed in the order transaction (no RESERVED state)
**Context:** A coupon must not be lost by a failed checkout nor used twice.
**Options:** (a) mark `RESERVED` when checkout starts, finalize/release after; (b) validate first, redeem at the end of the same transaction.
**Choice:** (b). Two states only: `AVAILABLE → REDEEMED`.
**Why:** With atomic checkout, "failed checkout loses the coupon" cannot happen – rollback restores it. A reservation state introduces the new failure mode of a crashed request leaving a coupon stuck, requiring a sweeper.
**Consequences:** If payment becomes a slow external call, holding the coupon (and write lock) for its duration is costly – see the payment decision and scale-out section.

### Decision: Coupon generation is lazy, sequential and DB-unique per milestone
**Options:** (a) auto-create the coupon inside the checkout transaction at every nth order; (b) admin endpoint computing eligibility from counts; (c) a counter row incremented by checkout.
**Choice:** (b), with `UNIQUE(milestone)`; milestone to create = `max(existing)+1`, allowed only if `floor(orders/n)` ≥ it.
**Why:** The spec makes generation an admin action. Deriving eligibility from `COUNT(orders)` instead of a counter removes a piece of state that could drift from reality (I8). Sequential milestones make "catch-up" behaviour deterministic.
**Consequences:** Coupons are not issued until an admin asks. `COUNT(*)` is O(n) – fine here; at scale use an indexed counter updated in the order transaction.

### Decision: Payment is a small interface called inside the transaction
**Choice:** `PaymentGateway.charge(amount, idempotency_key)`; fake always approves; tests inject a declining fake. A decline raises → full rollback → `402`.
**Why:** It lets me *prove* the rollback property (inventory, cart and coupon untouched after a decline) with a test.
**Consequences:** Calling a network service while holding a write lock is not acceptable in production. Production design: reserve in the DB → authorize payment (idempotency key = cart id) outside the lock → finalize in a second short transaction, with a recovery job for crashes in between. Deferred.

### Decision: Integer cents and half-up rounding
See §5.

## 4. Transaction, concurrency and idempotency strategy

Checkout, in order, inside one `BEGIN IMMEDIATE`:
1. Load cart. If already `CHECKED_OUT` → return stored order (replay) or `409` on coupon mismatch.
2. Reject empty cart; validate stock against current inventory (report all shortages); compute subtotal at current prices; optional `expected_subtotal_cents` check.
3. Validate coupon exists and is `AVAILABLE`; compute discount.
4. Claim cart (`WHERE status='OPEN'`), decrement inventory (`WHERE inventory >= q`), charge payment, insert order + items, redeem coupon (`WHERE status='AVAILABLE'`). Any failed rowcount or exception → rollback of everything.

Defence in depth: (1) the write lock serializes competing operations; (2) every claim is a conditional update whose rowcount is checked; (3) DB `UNIQUE`/`CHECK` constraints make the invariants true even if the code is wrong. Reads (`GET`, report) use a deferred read transaction, i.e. a consistent snapshot, and take no write lock.

**Evidence the tests have teeth:** I mutation-tested by removing the up-front write lock, the conditional guards and the inventory `CHECK` together; `test_concurrent_checkouts_never_oversell_limited_stock` failed. (I removed them all at once, so I have *not* shown which single layer each test depends on. That is a gap.)

## 5. Money and rounding

* All amounts are integer cents in the DB, in code and in JSON (`*_cents`). No floats.
* Line total = unit price × quantity. Subtotal = sum of line totals.
* Discount = `(subtotal × percent + 50) // 100` (round half up), capped at subtotal. Total = subtotal − discount ≥ 0.
* Rounding happens once, on the order-level discount, not per line, so the result is independent of line order.
* The order stores subtotal, discount, total, coupon code and percent, so the total can be re-derived from the order alone.

## 6. Error model

Uniform envelope `{error:{code,message,details}}`; `code` is the stable contract. Status classes: `404` unknown resource, `422` request invalid regardless of system state (bad type/quantity, empty cart, unknown coupon code), `409` valid request that conflicts with current state (stock, redeemed coupon, cart not open, price changed, no milestone), `402` payment declined. Validation errors from the framework are re-wrapped into the same envelope. Insufficient-inventory errors list every offending item with requested/available quantities.

## 7. Implemented vs deferred

**Implemented:** all required endpoints; seeded products incl. a 3-unit item; atomic idempotent checkout; coupon generation/redemption rules; read-only reconciling report; payment fake; tests including overlapping and repeated operations.

**Deferred on purpose:** authn/authz; coupon expiry and per-customer binding; stock reservation and cart expiry; pagination on list endpoints; a stored counter for order count; structured logging/metrics; request-level idempotency keys; OpenAPI examples (FastAPI's generated `/docs` is available); multiple currencies, tax, shipping.

**Known weaknesses:** write throughput is capped by SQLite's single writer; the payment call sits inside the lock; coupon codes are bearer tokens; `GET /admin/orders` is unpaginated; the concurrency tests only cover a 12-thread burst in one process, not multiple processes (SQLite file locking would cover them, but I haven't tested that).

## 8. Evolution to multiple instances / production

* Move to Postgres. `BEGIN IMMEDIATE` is replaced by `READ COMMITTED` + the same conditional updates (already written that way), or `SELECT … FOR UPDATE` on products in a fixed order (by id) to avoid deadlocks; retry on serialization failure.
* Coupon redeem and cart claim stay as conditional updates; uniqueness constraints are unchanged.
* Order count for milestones: a counter row updated in the order transaction (or `COUNT` on an index), with generation guarded by `UNIQUE(milestone)` – an advisory lock is optional.
* Payment: authorize outside the DB transaction with the cart id as provider idempotency key; persist a `PAYMENT_PENDING` state and a reconciliation job for crashes.
* Add real idempotency keys with stored responses if checkout ever stops being 1 cart = 1 order; add cart expiry, optional soft reservations with TTL, a read replica or materialized view for reporting, auth on `/admin`, outbox events for downstream consumers.

## 9. How I used AI tools

I used Claude to draft the service skeleton, the test suite and these documents, then reviewed and ran everything. Concrete cases where I changed or rejected its output *(edit this section so it reflects what you personally did – the evaluators will ask about it)*:

* **Rejected a hacky schema initializer.** The first draft split the SQL script on `";\n\n"` and ran statements inside a transaction with a conditional-expression trick. I replaced it with `executescript` for the schema and a transaction only for seeding.
* **Tightened weak tests.** Generated assertions like `assert A or B` (price snapshot and sold-quantity checks) could pass for the wrong reason; I replaced them with exact lookups.
* **Redirected the price-guard API.** I first considered `expected_total_cents` for the optimistic price check; I changed it to `expected_subtotal_cents` because a client cannot know the post-coupon total without re-implementing the rounding rule.
* **Validated by breaking it.** I ran a mutation experiment (removing the locking/guards) to confirm the concurrency test fails on a wrong implementation, rather than trusting that it passes on the right one.

## 10. What I would examine first with two more hours

1. Mutation-test each protection layer *individually* to see which tests depend on which, and add the missing ones.
2. A multi-process test (several uvicorn workers hitting one DB file) instead of threads in one process.
3. Port the repository layer to Postgres and run the same concurrency tests with `READ COMMITTED`.
4. Split payment out of the transaction (reserve → authorize → finalize) with a crash-recovery test.
5. Coupon expiry, and pagination for list endpoints.
