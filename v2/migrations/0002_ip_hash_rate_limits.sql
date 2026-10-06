-- FilmBam V2, 0002: daily limit per IP as well + code-attempt counter in D1.
-- ip_hash = SHA-256 of the client IP truncated to 128 bits (unsalted), so the IP is not stored in plain text.
ALTER TABLE users  ADD COLUMN ip_hash TEXT;
ALTER TABLE orders ADD COLUMN ip_hash TEXT;
CREATE INDEX IF NOT EXISTS idx_users_ip_created ON users (ip_hash, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_ip_ts     ON orders (ip_hash, ts);

-- Fixed window per key ("code:<ip_hash>"). Lives in D1, not in isolate memory: the limit of
-- 10 code attempts / 10 min per IP holds across isolates and colos (it is keyed per exact IP).
CREATE TABLE IF NOT EXISTS rate_limits (
  key   TEXT    PRIMARY KEY,
  n     INTEGER NOT NULL,
  reset INTEGER NOT NULL
);
