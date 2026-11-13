-- =============================================================================
-- JellyRent central database - 01: types
--
-- Object-relational building blocks: enumerations and composite types.
-- Methods on these types live in 03_functions.sql.
-- =============================================================================

-- Health of a Jellyfin node as observed by the heartbeat.
-- MAINTENANCE is set by an administrator and is never overwritten automatically.
CREATE TYPE node_status AS ENUM ('ONLINE', 'OFFLINE', 'MAINTENANCE');

-- Rental life cycle. PENDING and RETURNING are the intermediate saga states.
--
--   PENDING ──▶ ACTIVE ──▶ RETURNING ──▶ RETURNED | EXPIRED
--      └──────▶ FAILED (compensated)
CREATE TYPE rental_status AS ENUM (
    'PENDING', 'ACTIVE', 'RETURNING', 'RETURNED', 'EXPIRED', 'FAILED'
);

-- Why a rental is being closed; decides the terminal state of the return saga.
CREATE TYPE return_reason AS ENUM ('USER', 'EXPIRED', 'ADMIN');

-- What the session guard detected.
CREATE TYPE violation_reason AS ENUM ('NO_RENTAL', 'CONCURRENT_SESSION');

-- External identifiers of a work (composite type).
CREATE TYPE provider_ids AS (
    imdb text,
    tmdb text
);

-- Technical description of a physical copy (composite type).
CREATE TYPE media_info AS (
    container   text,
    video_codec text,
    width       int,
    height      int,
    runtime_sec int
);
