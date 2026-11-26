-- 010: every team's total equity every 5 minutes of continuous trading, plus the open and the close.
-- Feeds the leaderboard sparklines, the team page's return curve and the season report.
BEGIN;

CREATE TABLE equity_points (
  portfolio_id UUID NOT NULL REFERENCES portfolios(id),
  ts           TIMESTAMPTZ NOT NULL,
  equity       NUMERIC(20,2) NOT NULL,
  PRIMARY KEY (portfolio_id, ts)
);
CREATE INDEX equity_points_ts_idx ON equity_points (ts);

COMMIT;
