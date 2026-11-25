-- 003: runtime support for v1 (symbol names for the stock picker, fast lookups used by the API).
BEGIN;

CREATE TABLE symbols (
  symbol     TEXT PRIMARY KEY CHECK (symbol ~ '^[0-9]{6}\.(SH|SZ|BJ)$'),
  name       TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX orders_debate_idx ON orders (debate_id) WHERE debate_id IS NOT NULL;
CREATE INDEX trades_order_idx ON trades (order_id);
CREATE INDEX portfolio_badges_idx ON portfolio_badges (portfolio_id, badge_code);

COMMIT;
