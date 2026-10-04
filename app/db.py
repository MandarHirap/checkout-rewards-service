import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    price_cents  INTEGER NOT NULL CHECK (price_cents >= 0),
    inventory    INTEGER NOT NULL CHECK (inventory >= 0)      -- DB-level "never oversell" backstop
);

CREATE TABLE IF NOT EXISTS carts (
    id          TEXT PRIMARY KEY,
    status      TEXT NOT NULL CHECK (status IN ('OPEN', 'CHECKED_OUT')),
    order_id    TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cart_items (
    cart_id                  TEXT NOT NULL REFERENCES carts(id),
    product_id               TEXT NOT NULL REFERENCES products(id),
    quantity                 INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_at_add_cents  INTEGER NOT NULL,
    PRIMARY KEY (cart_id, product_id)
);

CREATE TABLE IF NOT EXISTS orders (
    id               TEXT PRIMARY KEY,
    cart_id          TEXT NOT NULL UNIQUE REFERENCES carts(id),   -- one order per cart
    subtotal_cents   INTEGER NOT NULL,
    discount_cents   INTEGER NOT NULL,
    total_cents      INTEGER NOT NULL,
    coupon_code      TEXT,
    coupon_percent   INTEGER,
    payment_ref      TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    CHECK (discount_cents >= 0 AND discount_cents <= subtotal_cents),
    CHECK (total_cents = subtotal_cents - discount_cents)
);
-- a coupon code can appear on at most one order
CREATE UNIQUE INDEX IF NOT EXISTS orders_coupon_once ON orders(coupon_code) WHERE coupon_code IS NOT NULL;

CREATE TABLE IF NOT EXISTS order_items (
    order_id          TEXT NOT NULL REFERENCES orders(id),
    product_id        TEXT NOT NULL,
    product_name      TEXT NOT NULL,       -- snapshot
    unit_price_cents  INTEGER NOT NULL,    -- snapshot
    quantity          INTEGER NOT NULL CHECK (quantity > 0),
    line_total_cents  INTEGER NOT NULL,
    PRIMARY KEY (order_id, product_id)
);

CREATE TABLE IF NOT EXISTS coupons (
    code               TEXT PRIMARY KEY,
    milestone          INTEGER NOT NULL UNIQUE,   -- at most one coupon per milestone
    percent_off        INTEGER NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('AVAILABLE', 'REDEEMED')),
    created_at         TEXT NOT NULL,
    redeemed_at        TEXT,
    redeemed_order_id  TEXT UNIQUE REFERENCES orders(id)
);
"""

SEED_PRODUCTS = [
    ("sku-tee",       "Classic T-Shirt",          1999, 100),
    ("sku-mug",       "Ceramic Mug",              1250, 50),
    ("sku-notebook",  "Dot-Grid Notebook",         899, 200),
    ("sku-bottle",    "Insulated Water Bottle",   2499, 30),
    ("sku-headphones", "Wireless Headphones",    8999, 15),
    ("sku-sneakers",  "Limited Edition Sneakers", 12999, 3),   # deliberately scarce
]


def connect(path: str) -> sqlite3.Connection:
    # isolation_level=None -> we issue BEGIN/COMMIT ourselves, nothing implicit.
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, write: bool = True):
    """BEGIN IMMEDIATE takes SQLite's write lock up front, so check-then-act
    sequences inside the block cannot interleave with another writer.
    write=False gives a deferred read transaction = a consistent snapshot."""
    conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db(path: str) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)  # idempotent (IF NOT EXISTS)
        with transaction(conn):
            if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
                conn.executemany("INSERT INTO products VALUES (?, ?, ?, ?)", SEED_PRODUCTS)
    finally:
        conn.close()
