-- Wipe Two-Key Escape gameplay so a couple can play again from scratch.
--
-- Run it with (from the repo root, backend stack up):
--   docker exec -i -e PGPASSWORD=ConfessItLocalPassword confessit_v2-postgres-1 \
--     psql -U confessit -d ConfessIt < scripts/wipe-two-key-escape.sql
--
-- What it deletes:
--   * two_key_escape_games ......... every maze period (past and current)
--   * two_key_escape_sessions ....... every couple's live game (role picks,
--     submissions, position, keys, cooldowns, results) -- removed
--     automatically through the ON DELETE CASCADE foreign key.
--
-- What it deliberately keeps:
--   * game_configs ................. your grid-size setting survives, so the
--     next period deals the same size again.
--   * couple_game_sessions ......... questionnaire pairings are untouched,
--     so the same two users can immediately re-submit roles and replay.
--   * users and everything else ..... no other game or feature is touched.
--
-- WARNING: wiping alone leaves NO active period, and reads never create
-- one -- the page will only show "no game" until a fresh period is opened.
-- Prefer the wrapper that does both steps (PowerShell, from repo root):
--   .\scripts\replay-two-key-escape.ps1
-- which wipes, restarts the backend to deal a fresh maze, and verifies it.
-- Manual alternative after running this file, pick ONE:
--   a) docker restart confessit_v2-backend-1   (boot deals a new maze), or
--   b) Admin -> Manage games -> "Run resets now".

BEGIN;

DELETE FROM two_key_escape_games;

SELECT 'two_key_escape_games' AS table_name, count(*) AS rows_left FROM two_key_escape_games
UNION ALL
SELECT 'two_key_escape_sessions', count(*) FROM two_key_escape_sessions;

COMMIT;

-- Fresh 12x12+ maze dealt on next backend boot / manual reset, at the
-- grid size still stored in game_configs.
SELECT game_id, grid_size AS kept_grid_size FROM game_configs WHERE game_id = 'two-key-escape';
