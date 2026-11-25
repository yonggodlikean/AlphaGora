-- Agora 001: core schema.
-- Conventions
--   * Money: NUMERIC(20,2) CNY. Prices: NUMERIC(12,3) (A-share tick is 0.01; 3dp leaves room for funds/ETFs at 0.001).
--   * Quantities: BIGINT shares (never "hands"); adapters normalise.
--   * Every row that is history (ledger, trades, order_events, arena_events) is append-only, enforced by trigger.
--   * `orders`, `positions` are mutable *projections* of that history and can be rebuilt from it.

BEGIN;

-- gen_random_uuid() is built in since PostgreSQL 13; no extensions required.

-- ---------------------------------------------------------------- enums
CREATE TYPE controller_type AS ENUM ('openpup_ai', 'external_bot', 'human', 'hybrid');
CREATE TYPE season_status   AS ENUM ('draft', 'registration', 'live', 'settled');
CREATE TYPE order_side      AS ENUM ('buy', 'sell');
CREATE TYPE order_type      AS ENUM ('limit', 'market');   -- market = 最优五档即时成交剩余撤销 (best-5 IOC)
CREATE TYPE order_status    AS ENUM ('new', 'partially_filled', 'filled', 'canceled', 'expired', 'rejected');
CREATE TYPE fill_liquidity  AS ENUM ('taker', 'maker', 'queue', 'board_break');
CREATE TYPE ledger_account  AS ENUM ('cash', 'position', 'fee_commission', 'fee_stamp_duty', 'fee_transfer',
                                     'realized_pnl', 'capital');
CREATE TYPE ledger_entry_type AS ENUM ('season_funding', 'buy_fill', 'sell_fill', 'fee', 'adjustment');

-- ---------------------------------------------------------------- helpers
CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only (attempted %)', TG_TABLE_NAME, TG_OP USING ERRCODE = 'integrity_constraint_violation';
END $$;

CREATE FUNCTION touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

-- ---------------------------------------------------------------- seasons
CREATE TABLE seasons (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slug             TEXT NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]{3,48}$'),
  name             TEXT NOT NULL,
  start_date       DATE NOT NULL,
  end_date         DATE NOT NULL,
  initial_cash     NUMERIC(20,2) NOT NULL DEFAULT 1000000.00 CHECK (initial_cash > 0),
  benchmark_symbol TEXT NOT NULL DEFAULT '000300.SH',
  status           season_status NOT NULL DEFAULT 'draft',
  rules            JSONB NOT NULL DEFAULT '{}'::jsonb,   -- fee overrides, symbol universe, etc.
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (end_date >= start_date)
);

-- ---------------------------------------------------------------- participants & portfolios
-- A participant is the *who* (a human, a bot operator, the house). A portfolio is one entry in one season.
CREATE TABLE participants (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  display_name TEXT NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 64),
  is_house     BOOLEAN NOT NULL DEFAULT false,      -- official teams are owned by the house participant
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE portfolios (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  season_id       UUID NOT NULL REFERENCES seasons(id),
  participant_id  UUID NOT NULL REFERENCES participants(id),
  name            TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 64),
  persona_tag     TEXT,
  controller_type controller_type NOT NULL,
  is_official     BOOLEAN NOT NULL DEFAULT false,
  is_benchmark    BOOLEAN NOT NULL DEFAULT false,   -- 沪深300 躺平基准: tracked, never trades
  brief           TEXT,
  avatar_url      TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (season_id, name)
);
CREATE INDEX portfolios_season_idx ON portfolios (season_id, controller_type);

-- Tokens live in their own table so they can be rotated / revoked / scoped without touching the portfolio.
-- token_hash = HMAC-SHA256(pepper, raw_token); the raw token is shown once at creation.
CREATE TABLE api_tokens (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  portfolio_id UUID NOT NULL REFERENCES portfolios(id),
  token_prefix TEXT NOT NULL,                       -- e.g. "agk_3f9a" for display / support
  token_hash   BYTEA NOT NULL UNIQUE,
  scopes       TEXT[] NOT NULL DEFAULT ARRAY['state:read','market:read','orders:write','debates:write'],
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at TIMESTAMPTZ,
  revoked_at   TIMESTAMPTZ
);
CREATE INDEX api_tokens_portfolio_idx ON api_tokens (portfolio_id) WHERE revoked_at IS NULL;

-- ---------------------------------------------------------------- debates
CREATE TABLE debates (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  portfolio_id         UUID NOT NULL REFERENCES portfolios(id),
  symbol               TEXT NOT NULL CHECK (symbol ~ '^[0-9]{6}\.(SH|SZ|BJ)$'),
  -- [{ "round": 1, "role": "bull", "stance": "buy", "argument": "...", "model_id": "...", "confidence": 0.7 }, ...]
  rounds               JSONB NOT NULL CHECK (jsonb_typeof(rounds) = 'array' AND jsonb_array_length(rounds) > 0),
  -- { "action": "buy", "qty": 1000, "limit_price": "10.01", "summary": "...", "dissent": [...] }
  final_decision       JSONB NOT NULL CHECK (jsonb_typeof(final_decision) = 'object'),
  execution_trace      JSONB,                          -- OpenPup graph execution trace (optional, may be large)
  execution_trace_hash TEXT CHECK (execution_trace_hash ~ '^[0-9a-f]{64}$'),  -- sha256 hex
  has_conflict         BOOLEAN GENERATED ALWAYS AS (
                         jsonb_path_exists(rounds, '$[*] ? (@.stance == "buy")')
                         AND jsonb_path_exists(rounds, '$[*] ? (@.stance == "sell")')) STORED,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX debates_portfolio_idx ON debates (portfolio_id, created_at DESC);
CREATE INDEX debates_symbol_idx    ON debates (symbol, created_at DESC);
CREATE TRIGGER debates_append_only BEFORE UPDATE OR DELETE ON debates
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- orders (projection) + order_events (history)
CREATE TABLE orders (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  portfolio_id     UUID NOT NULL REFERENCES portfolios(id),
  debate_id        UUID REFERENCES debates(id),
  idempotency_key  TEXT NOT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 128),
  request_hash     TEXT NOT NULL,                     -- sha256 of canonical payload; same key + different hash => 409
  thesis           TEXT CHECK (thesis IS NULL OR char_length(thesis) BETWEEN 4 AND 280),
  side             order_side NOT NULL,
  symbol           TEXT NOT NULL CHECK (symbol ~ '^[0-9]{6}\.(SH|SZ|BJ)$'),
  order_type       order_type NOT NULL,
  price            NUMERIC(12,3) CHECK (price IS NULL OR price > 0),
  qty              BIGINT NOT NULL CHECK (qty > 0),
  filled_qty       BIGINT NOT NULL DEFAULT 0,
  avg_fill_price   NUMERIC(12,4),
  fees_paid        NUMERIC(20,2) NOT NULL DEFAULT 0,
  queue_initial    BIGINT CHECK (queue_initial IS NULL OR queue_initial >= 0),
  queue_ahead      BIGINT CHECK (queue_ahead IS NULL OR queue_ahead >= 0),
  status           order_status NOT NULL DEFAULT 'new',
  reject_reason    TEXT,
  trade_date       DATE NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Idempotency is scoped per portfolio: two bots may both pick "order-0001".
  UNIQUE (portfolio_id, idempotency_key),
  -- "Every trade MUST carry a public thesis or multi-agent debate log."
  CHECK (thesis IS NOT NULL OR debate_id IS NOT NULL),
  CHECK ((order_type = 'limit') = (price IS NOT NULL)),
  CHECK (filled_qty BETWEEN 0 AND qty),
  CHECK (status <> 'filled' OR filled_qty = qty)
);
CREATE INDEX orders_open_idx      ON orders (symbol, created_at) WHERE status IN ('new', 'partially_filled');
CREATE INDEX orders_portfolio_idx ON orders (portfolio_id, created_at DESC);
CREATE TRIGGER orders_touch BEFORE UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE order_events (
  id         BIGSERIAL PRIMARY KEY,
  order_id   UUID NOT NULL REFERENCES orders(id),
  event_type TEXT NOT NULL,           -- accepted | partially_filled | filled | canceled | expired | queue_update
  payload    JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX order_events_order_idx ON order_events (order_id, id);
CREATE TRIGGER order_events_append_only BEFORE UPDATE OR DELETE ON order_events
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- trades (fills)
CREATE TABLE trades (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id      UUID NOT NULL REFERENCES orders(id),
  portfolio_id  UUID NOT NULL REFERENCES portfolios(id),
  debate_id     UUID REFERENCES debates(id),
  symbol        TEXT NOT NULL,
  side          order_side NOT NULL,
  price         NUMERIC(12,3) NOT NULL CHECK (price > 0),
  qty           BIGINT NOT NULL CHECK (qty > 0),
  notional      NUMERIC(20,2) NOT NULL,
  commission    NUMERIC(20,2) NOT NULL DEFAULT 0,
  stamp_duty    NUMERIC(20,2) NOT NULL DEFAULT 0,
  transfer_fee  NUMERIC(20,2) NOT NULL DEFAULT 0,
  liquidity     fill_liquidity NOT NULL,
  snapshot_seq  BIGINT NOT NULL,                      -- market snapshot that produced the fill (replayable)
  trade_date    DATE NOT NULL,
  executed_at   TIMESTAMPTZ NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX trades_portfolio_idx ON trades (portfolio_id, executed_at DESC);
CREATE INDEX trades_symbol_idx    ON trades (symbol, executed_at DESC);   -- conflict detection
CREATE TRIGGER trades_append_only BEFORE UPDATE OR DELETE ON trades
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- positions (projection)
CREATE TABLE positions (
  portfolio_id UUID NOT NULL REFERENCES portfolios(id),
  symbol       TEXT NOT NULL,
  qty          BIGINT NOT NULL DEFAULT 0 CHECK (qty >= 0),
  frozen_qty   BIGINT NOT NULL DEFAULT 0 CHECK (frozen_qty >= 0),   -- bought today, T+1 locked
  cost_basis   NUMERIC(20,2) NOT NULL DEFAULT 0,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (portfolio_id, symbol),
  CHECK (frozen_qty <= qty)
);
CREATE TRIGGER positions_touch BEFORE UPDATE ON positions FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- ---------------------------------------------------------------- double-entry ledger
-- One journal = one economic event (a fill, a funding). Its entries must sum to zero (checked at COMMIT).
-- Sign convention: debit positive. Buy 100 @ 10.00 + 5 fee:
--   position +1000, cash -1000, fee_commission +5, cash -5.
CREATE TABLE ledger_journals (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  portfolio_id UUID NOT NULL REFERENCES portfolios(id),
  entry_type   ledger_entry_type NOT NULL,
  ref_type     TEXT NOT NULL,                     -- 'trade' | 'season' | 'adjustment'
  ref_id       UUID NOT NULL,
  memo         TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (ref_type, ref_id, entry_type)           -- a fill can never be journaled twice
);

CREATE TABLE ledger_entries (
  id            BIGSERIAL PRIMARY KEY,
  journal_id    UUID NOT NULL REFERENCES ledger_journals(id),
  portfolio_id  UUID NOT NULL REFERENCES portfolios(id),
  account       ledger_account NOT NULL,
  symbol        TEXT,                              -- set for account = 'position'
  amount        NUMERIC(20,2) NOT NULL,
  qty_delta     BIGINT NOT NULL DEFAULT 0,          -- share movement for position entries
  balance_after NUMERIC(20,2) NOT NULL,             -- running balance of (portfolio, account, symbol)
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK ((account = 'position') = (symbol IS NOT NULL)),
  CHECK (account <> 'cash' OR balance_after >= 0)   -- no margin in V1
);
CREATE INDEX ledger_entries_account_idx ON ledger_entries (portfolio_id, account, symbol, id DESC);
CREATE INDEX ledger_entries_journal_idx ON ledger_entries (journal_id);
CREATE TRIGGER ledger_entries_append_only BEFORE UPDATE OR DELETE ON ledger_entries
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER ledger_journals_append_only BEFORE UPDATE OR DELETE ON ledger_journals
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE FUNCTION assert_journal_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s NUMERIC;
BEGIN
  SELECT COALESCE(SUM(amount), 0) INTO s FROM ledger_entries WHERE journal_id = NEW.journal_id;
  IF s <> 0 THEN
    RAISE EXCEPTION 'ledger journal % unbalanced by %', NEW.journal_id, s USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER ledger_entries_balanced AFTER INSERT ON ledger_entries
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION assert_journal_balanced();

-- ---------------------------------------------------------------- daily snapshots & benchmark
CREATE TABLE daily_snapshots (
  portfolio_id  UUID NOT NULL REFERENCES portfolios(id),
  trade_date    DATE NOT NULL,
  total_equity  NUMERIC(20,2) NOT NULL,
  cash          NUMERIC(20,2) NOT NULL,
  market_value  NUMERIC(20,2) NOT NULL,
  daily_return  NUMERIC(12,8) NOT NULL,
  cum_return    NUMERIC(12,8) NOT NULL,
  excess_return NUMERIC(12,8),                       -- vs season benchmark, cumulative
  max_drawdown  NUMERIC(12,8) NOT NULL CHECK (max_drawdown <= 0),
  sharpe_ratio  NUMERIC(12,6),                       -- NULL until enough observations
  win_rate      NUMERIC(6,4),
  turnover      NUMERIC(20,2) NOT NULL DEFAULT 0,
  rank          INTEGER,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (portfolio_id, trade_date)
);
CREATE INDEX daily_snapshots_date_idx ON daily_snapshots (trade_date, cum_return DESC);

CREATE TABLE benchmark_daily (
  symbol     TEXT NOT NULL,
  trade_date DATE NOT NULL,
  close      NUMERIC(12,4) NOT NULL CHECK (close > 0),
  PRIMARY KEY (symbol, trade_date)
);

-- ---------------------------------------------------------------- show layer
CREATE TABLE portfolio_badges (
  portfolio_id UUID NOT NULL REFERENCES portfolios(id),
  badge_code   TEXT NOT NULL,          -- zhaban_jiepan (炸板接盘侠) | paile_yitian (排了一天寂寞) | ditianban (地天板战神) ...
  awarded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  ref_id       UUID,                   -- trade/order that earned it
  PRIMARY KEY (portfolio_id, badge_code, awarded_at)
);

-- Durable copy of everything pushed over SSE, so clients can resume with Last-Event-ID and the
-- Conflict Feed / daily roast can be regenerated deterministically.
CREATE TABLE arena_events (
  id         BIGSERIAL PRIMARY KEY,
  season_id  UUID NOT NULL REFERENCES seasons(id),
  event_type TEXT NOT NULL,   -- market.open | market.close | order.filled | limit_up.broken | conflict.detected ...
  symbol     TEXT,
  payload    JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX arena_events_season_idx ON arena_events (season_id, id);
CREATE INDEX arena_events_type_idx   ON arena_events (season_id, event_type, id);
CREATE TRIGGER arena_events_append_only BEFORE UPDATE OR DELETE ON arena_events
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

COMMIT;
