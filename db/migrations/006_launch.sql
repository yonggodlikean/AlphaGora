-- 006: launch features: invite-only sign-up with seat caps, account status (anti-cheat),
-- moderation, spectator cheers and visitor counts.
BEGIN;

CREATE TABLE invites (
  code       TEXT PRIMARY KEY CHECK (code ~ '^[A-Z0-9-]{6,32}$'),
  season_id  UUID NOT NULL REFERENCES seasons(id),
  kind       TEXT NOT NULL CHECK (kind IN ('human', 'ai')),
  max_uses   INT  NOT NULL DEFAULT 1 CHECK (max_uses > 0),
  used       INT  NOT NULL DEFAULT 0,
  note       TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at TIMESTAMPTZ,
  CHECK (used <= max_uses)
);

-- active: normal · suspended: cannot trade, off the leaderboard, reversible · banned: token dead, off the board
ALTER TABLE portfolios ADD COLUMN status TEXT NOT NULL DEFAULT 'active'
  CHECK (status IN ('active', 'suspended', 'banned'));
ALTER TABLE portfolios ADD COLUMN status_reason TEXT;
ALTER TABLE portfolios ADD COLUMN invite_code TEXT REFERENCES invites(code);
-- sha256(pepper | client IP) at sign-up: lets an admin see several accounts from one source, never the IP itself
ALTER TABLE portfolios ADD COLUMN join_fingerprint TEXT;
CREATE INDEX portfolios_fingerprint_idx ON portfolios (join_fingerprint) WHERE join_fingerprint IS NOT NULL;

-- Hidden user content (debates are append-only, so moderation lives beside them)
CREATE TABLE moderation (
  ref_type  TEXT NOT NULL CHECK (ref_type IN ('order', 'debate')),
  ref_id    UUID NOT NULL,
  reason    TEXT,
  hidden_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (ref_type, ref_id)
);

-- One cheer per visitor per team per day. visitor = sha256(pepper | browser id)
CREATE TABLE cheers (
  portfolio_id UUID NOT NULL REFERENCES portfolios(id),
  day          DATE NOT NULL,
  visitor      TEXT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (portfolio_id, day, visitor)
);

-- Daily unique visitors (counts only)
CREATE TABLE visits (
  day      DATE PRIMARY KEY,
  visitors INT NOT NULL DEFAULT 0
);

COMMIT;
