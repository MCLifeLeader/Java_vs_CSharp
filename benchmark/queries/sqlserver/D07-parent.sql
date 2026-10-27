-- Shared raw SQL contract; named markers are adapted without changing logical SQL.
DELETE FROM orders
WHERE id = :order_id AND version = :old_version;
