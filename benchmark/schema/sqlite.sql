-- Fresh controller-owned named in-memory database only. No reset/drop statements.
-- Apply these connection settings on EVERY connection, not only the schema connection.
PRAGMA foreign_keys = ON;
PRAGMA read_uncommitted = 0;
PRAGMA busy_timeout = 5000;
PRAGMA journal_mode = MEMORY;
CREATE TABLE customer (
    id INTEGER NOT NULL PRIMARY KEY CHECK (id > 0),
    email TEXT COLLATE BINARY NOT NULL UNIQUE CHECK (length(email) BETWEEN 1 AND 254 AND instr(email, char(0)) = 0 AND email NOT GLOB '*[^ -~]*' AND substr(email, -1) <> ' '),
    name TEXT COLLATE BINARY NOT NULL CHECK (length(name) BETWEEN 1 AND 128 AND instr(name, char(0)) = 0 AND name NOT GLOB '*[^ -~]*' AND substr(name, -1) <> ' '),
    balance_cents INTEGER NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 0)
) STRICT, WITHOUT ROWID;
CREATE TABLE product (
    id INTEGER NOT NULL PRIMARY KEY CHECK (id > 0),
    sku TEXT COLLATE BINARY NOT NULL UNIQUE CHECK (length(sku) BETWEEN 1 AND 64 AND instr(sku, char(0)) = 0 AND sku NOT GLOB '*[^ -~]*' AND substr(sku, -1) <> ' '),
    name TEXT COLLATE BINARY NOT NULL CHECK (length(name) BETWEEN 1 AND 128 AND instr(name, char(0)) = 0 AND name NOT GLOB '*[^ -~]*' AND substr(name, -1) <> ' '),
    price_cents INTEGER NOT NULL CHECK (price_cents >= 0)
) STRICT, WITHOUT ROWID;
CREATE TABLE orders (
    id INTEGER NOT NULL PRIMARY KEY CHECK (id > 0),
    customer_id INTEGER NOT NULL,
    status INTEGER NOT NULL CHECK (status IN (0, 1, 2, 3)),
    created_epoch_ms INTEGER NOT NULL CHECK (created_epoch_ms >= 0),
    total_cents INTEGER NOT NULL CHECK (total_cents >= 0),
    version INTEGER NOT NULL CHECK (version >= 0),
    FOREIGN KEY (customer_id) REFERENCES customer (id) ON DELETE NO ACTION
) STRICT, WITHOUT ROWID;
CREATE TABLE order_line (
    id INTEGER NOT NULL PRIMARY KEY CHECK (id > 0),
    order_id INTEGER NOT NULL,
    line_no INTEGER NOT NULL CHECK (line_no > 0),
    product_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    UNIQUE (order_id, line_no),
    FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE NO ACTION,
    FOREIGN KEY (product_id) REFERENCES product (id) ON DELETE NO ACTION
) STRICT, WITHOUT ROWID;
CREATE INDEX ix_orders_customer_created_id ON orders (customer_id, created_epoch_ms, id);
CREATE INDEX ix_orders_status_created_id ON orders (status, created_epoch_ms, id);
CREATE INDEX ix_order_line_product_id ON order_line (product_id);
