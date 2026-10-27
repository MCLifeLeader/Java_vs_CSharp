-- Shared raw SQL contract; named markers are adapted without changing logical SQL.
SELECT id, email, name, balance_cents, version
FROM customer
WHERE id = :customer_id;
