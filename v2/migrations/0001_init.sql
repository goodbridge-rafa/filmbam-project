-- FilmBam V2: initial schema. D1 = SQLite.
CREATE TABLE IF NOT EXISTS users (
  id         TEXT PRIMARY KEY,
  role       TEXT    NOT NULL DEFAULT 'user',
  created_at INTEGER NOT NULL,
  last_seen  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
  id        TEXT PRIMARY KEY,
  user_id   TEXT    NOT NULL,
  ts        INTEGER NOT NULL,
  mode      TEXT    NOT NULL,
  len       INTEGER NOT NULL,
  fmt       TEXT    NOT NULL,
  q         TEXT    NOT NULL,
  price     REAL    NOT NULL,
  cost      REAL    NOT NULL,
  brief     TEXT    NOT NULL,
  status    TEXT    NOT NULL DEFAULT 'pending',
  note      TEXT,
  link      TEXT,
  file      TEXT,
  ts_prod   INTEGER,
  ts_done   INTEGER,
  cost_real REAL,
  runner    TEXT
);

CREATE TABLE IF NOT EXISTS ledger (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id TEXT,
  ts       INTEGER NOT NULL,
  usd      REAL    NOT NULL,
  status   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_user_ts   ON orders (user_id, ts);
CREATE INDEX IF NOT EXISTS idx_orders_status_ts ON orders (status, ts);
