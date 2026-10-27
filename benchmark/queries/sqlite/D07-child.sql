-- Shared raw SQL contract; named markers are adapted without changing logical SQL.
DELETE FROM order_line
WHERE id = :line_id AND order_id = :order_id;
