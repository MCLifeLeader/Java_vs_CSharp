-- Shared raw SQL contract; named markers are adapted without changing logical SQL.
INSERT INTO order_line (id, order_id, line_no, product_id, quantity, unit_price_cents)
VALUES (:line_id, :order_id, :line_no, :product_id, :quantity, :unit_price_cents);
