-- Shared raw SQL contract; named markers are adapted without changing logical SQL.
UPDATE customer
SET balance_cents = :new_balance_cents, version = :new_version
WHERE id = :customer_id AND version = :old_version;
