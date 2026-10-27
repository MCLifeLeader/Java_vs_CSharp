-- Shared raw SQL contract; named markers are adapted without changing logical SQL.
SELECT id, customer_id, status, created_epoch_ms, total_cents, version
FROM orders
WHERE status = :status
  AND (created_epoch_ms > :cursor_created_epoch_ms
       OR (created_epoch_ms = :cursor_created_epoch_ms AND id > :cursor_id))
ORDER BY created_epoch_ms ASC, id ASC
LIMIT 50;
