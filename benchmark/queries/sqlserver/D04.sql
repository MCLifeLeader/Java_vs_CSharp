-- Shared raw SQL contract; named markers are adapted without changing logical SQL.
SELECT status, COUNT_BIG(id) AS order_count, SUM(total_cents) AS total_cents
FROM orders
WHERE created_epoch_ms >= :start_epoch_ms AND created_epoch_ms < :end_epoch_ms
GROUP BY status
ORDER BY status ASC;
