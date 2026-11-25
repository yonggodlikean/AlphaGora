-- 007: the last snapshot of every symbol at the close. An engine restarted after hours reads it back, so prices,
-- the symbol list and position values stay at the close instead of going blank (or back to cost) until the
-- next open. One row per symbol, overwritten every close.
BEGIN;

CREATE TABLE market_marks (
  symbol     TEXT PRIMARY KEY,
  trade_date DATE NOT NULL,
  data       JSONB NOT NULL,          -- canonical snapshot JSON (market.feed.snapshot_from_dict)
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
