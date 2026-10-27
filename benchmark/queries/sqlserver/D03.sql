-- Shared raw SQL contract; named markers are adapted without changing logical SQL.
SELECT o.id AS order_id, o.customer_id, o.status, o.created_epoch_ms,
       o.total_cents, o.version AS order_version,
       c.id AS customer_record_id, c.email, c.name AS customer_name,
       c.balance_cents, c.version AS customer_version,
       l.id AS line_id, l.order_id AS line_order_id, l.line_no,
       l.product_id, l.quantity, l.unit_price_cents
FROM orders o
JOIN customer c ON c.id = o.customer_id
JOIN order_line l ON l.order_id = o.id
WHERE o.id = :order_id
ORDER BY l.line_no ASC, l.id ASC;
