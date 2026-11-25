-- 004: liveness of externally driven teams (agent runners, bots). Updated at most once a minute.
ALTER TABLE portfolios ADD COLUMN last_seen_at TIMESTAMPTZ;
ALTER TABLE portfolios ADD COLUMN driver_name TEXT CHECK (driver_name IS NULL OR char_length(driver_name) <= 40);
