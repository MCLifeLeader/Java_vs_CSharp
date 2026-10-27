-- Shared raw SQL contract; named markers are adapted without changing logical SQL.
INSERT INTO orders (id, customer_id, status, created_epoch_ms, total_cents, version)
VALUES (:order_id, :customer_id, :status, :created_epoch_ms, :total_cents, :version);
