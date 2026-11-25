-- 009: season rollover. Final standings are kept per season; a carried-over team points back to last season's
-- portfolio (same participant, same name, same token), so its career can be shown.
BEGIN;

CREATE TABLE season_results (
  season_id     UUID NOT NULL REFERENCES seasons(id),
  portfolio_id  UUID NOT NULL REFERENCES portfolios(id),
  rank          INT,                       -- NULL for the benchmark
  cum_return    NUMERIC(12,6) NOT NULL,
  excess_return NUMERIC(12,6) NOT NULL,
  final_equity  NUMERIC(20,2) NOT NULL,
  trades        INT NOT NULL DEFAULT 0,
  PRIMARY KEY (season_id, portfolio_id)
);

ALTER TABLE portfolios ADD COLUMN carried_from UUID REFERENCES portfolios(id);
CREATE INDEX portfolios_participant_idx ON portfolios (participant_id, created_at);

COMMIT;
