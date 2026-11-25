-- V1 house participant and season S1. Idempotent (safe to re-run).
BEGIN;

INSERT INTO participants (id, display_name, is_house)
VALUES ('00000000-0000-4000-8000-000000000001', 'Agora House', true)
ON CONFLICT (id) DO NOTHING;

INSERT INTO seasons (id, slug, name, start_date, end_date, initial_cash, benchmark_symbol, status)
VALUES ('00000000-0000-4000-8000-0000000000a1', 's1-2026w', 'Season 1 · 2026 冬季赛', '2026-11-23', '2027-02-26',
        1000000.00, '000300.SH', 'registration')
ON CONFLICT (id) DO NOTHING;

-- The six official teams are created by the engine on startup (arena_engine/personas.py), so names and
-- briefs live in one place.


COMMIT;
