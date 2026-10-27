-- Shared checkout product-price read; markers adapted only by the provider.
SELECT id, sku, name, price_cents
FROM product
WHERE id = :product_id;
