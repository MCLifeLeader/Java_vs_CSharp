-- Fresh controller-owned database only; no CREATE DATABASE, DROP or reset operation.
-- Write transactions must explicitly select READ COMMITTED.
CREATE TABLE customer (
    id BIGINT NOT NULL PRIMARY KEY CHECK (id > 0),
    email VARCHAR(254) COLLATE "C" NOT NULL UNIQUE CHECK (length(email) BETWEEN 1 AND 254 AND email ~ '^[ -~]+$' AND right(email, 1) <> ' '),
    name VARCHAR(128) COLLATE "C" NOT NULL CHECK (length(name) BETWEEN 1 AND 128 AND name ~ '^[ -~]+$' AND right(name, 1) <> ' '),
    balance_cents BIGINT NOT NULL,
    version BIGINT NOT NULL CHECK (version >= 0)
);
CREATE TABLE product (
    id BIGINT NOT NULL PRIMARY KEY CHECK (id > 0),
    sku VARCHAR(64) COLLATE "C" NOT NULL UNIQUE CHECK (length(sku) BETWEEN 1 AND 64 AND sku ~ '^[ -~]+$' AND right(sku, 1) <> ' '),
    name VARCHAR(128) COLLATE "C" NOT NULL CHECK (length(name) BETWEEN 1 AND 128 AND name ~ '^[ -~]+$' AND right(name, 1) <> ' '),
    price_cents BIGINT NOT NULL CHECK (price_cents >= 0)
);
CREATE TABLE orders (
    id BIGINT NOT NULL PRIMARY KEY CHECK (id > 0),
    customer_id BIGINT NOT NULL,
    status BIGINT NOT NULL CHECK (status IN (0, 1, 2, 3)),
    created_epoch_ms BIGINT NOT NULL CHECK (created_epoch_ms >= 0),
    total_cents BIGINT NOT NULL CHECK (total_cents >= 0),
    version BIGINT NOT NULL CHECK (version >= 0),
    FOREIGN KEY (customer_id) REFERENCES customer (id) ON DELETE NO ACTION
);
CREATE TABLE order_line (
    id BIGINT NOT NULL PRIMARY KEY CHECK (id > 0),
    order_id BIGINT NOT NULL,
    line_no BIGINT NOT NULL CHECK (line_no > 0),
    product_id BIGINT NOT NULL,
    quantity BIGINT NOT NULL CHECK (quantity > 0),
    unit_price_cents BIGINT NOT NULL CHECK (unit_price_cents >= 0),
    UNIQUE (order_id, line_no),
    FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE NO ACTION,
    FOREIGN KEY (product_id) REFERENCES product (id) ON DELETE NO ACTION
);
CREATE INDEX ix_orders_customer_created_id ON orders (customer_id, created_epoch_ms, id);
CREATE INDEX ix_orders_status_created_id ON orders (status, created_epoch_ms, id);
CREATE INDEX ix_order_line_product_id ON order_line (product_id);
