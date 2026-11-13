-- =============================================================================
-- JellyRent central database - 02: tables
--
-- Object-relational model of the central catalog (broker). See
-- docs/architecture.md, section "Object model", for the ER diagram.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Media hierarchy (table inheritance)
-- -----------------------------------------------------------------------------

-- Abstract root of the hierarchy. Rows live only in child tables: the
-- NO INHERIT check rejects direct inserts into media_item while children,
-- which do not inherit that constraint, accept rows. Querying media_item
-- returns the rows of every child (polymorphic read).
CREATE TABLE media_item (
    id          bigserial PRIMARY KEY,
    name        text        NOT NULL CHECK (btrim(name) <> ''),
    year        int         CHECK (year BETWEEN 1870 AND 2200),
    providers   provider_ids,
    genres      text[]      NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT media_item_is_abstract CHECK (false) NO INHERIT
);

-- A film as a work, independent of its copies. Extensible with siblings,
-- e.g. CREATE TABLE series (...) INHERITS (media_item);
CREATE TABLE title (
    rating numeric(3, 1) CHECK (rating BETWEEN 0 AND 10)
) INHERITS (media_item);

-- Caveat of PostgreSQL inheritance: PRIMARY KEY, UNIQUE and FOREIGN KEY
-- constraints (and indexes) are NOT inherited. They must be declared on each
-- child, and foreign keys must reference the child, never the parent.
ALTER TABLE title ADD PRIMARY KEY (id);

-- Matching keys (catalog sync matches IMDb -> TMDb -> name + year).
CREATE UNIQUE INDEX title_imdb_uq ON title (((providers).imdb)) WHERE (providers).imdb IS NOT NULL;
CREATE UNIQUE INDEX title_tmdb_uq ON title (((providers).tmdb)) WHERE (providers).tmdb IS NOT NULL;
CREATE INDEX title_name_year_idx ON title (lower(name), year);
CREATE INDEX title_genres_gin ON title USING gin (genres);

-- -----------------------------------------------------------------------------
-- Topology
-- -----------------------------------------------------------------------------

CREATE TABLE node (
    id                   int         PRIMARY KEY,
    name                 text        NOT NULL UNIQUE CHECK (name ~ '^[a-z][a-z0-9_]{0,30}$'),
    base_url             text        NOT NULL,  -- URL the broker uses (internal network)
    public_url           text        NOT NULL,  -- URL put into links for end users
    sqlite_path          text        NOT NULL,  -- node's jellyfin.db as seen by PostgreSQL
    api_key              text,                  -- set by the bootstrap job; NULL = not provisioned
    status               node_status NOT NULL DEFAULT 'OFFLINE',
    consecutive_failures int         NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    last_seen_at         timestamptz,
    last_sync_at         timestamptz,
    last_error           text
);

-- -----------------------------------------------------------------------------
-- Copies ("discs"): one media file on one node
-- -----------------------------------------------------------------------------

CREATE TABLE copy (
    id            bigserial   PRIMARY KEY,
    title_id      bigint      NOT NULL REFERENCES title (id),
    node_id       int         NOT NULL REFERENCES node (id),
    jf_item_id    uuid        NOT NULL,                 -- item Id in the node's Jellyfin
    path          text,
    media         media_info,
    is_present    boolean     NOT NULL DEFAULT true,    -- false once the file disappears
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT copy_node_item_uq UNIQUE (node_id, jf_item_id)
);

CREATE INDEX copy_title_present_idx ON copy (title_id) WHERE is_present;

-- -----------------------------------------------------------------------------
-- Users and their accounts on the nodes
-- -----------------------------------------------------------------------------

CREATE TABLE app_user (
    id            serial      PRIMARY KEY,
    username      text        NOT NULL UNIQUE CHECK (username ~ '^[a-z0-9][a-z0-9_.-]{2,31}$'),
    password_hash text        NOT NULL,
    is_admin      boolean     NOT NULL DEFAULT false,
    max_rentals   int         NOT NULL DEFAULT 3 CHECK (max_rentals BETWEEN 0 AND 100),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE node_account (
    user_id    int         NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    node_id    int         NOT NULL REFERENCES node (id),
    jf_user_id uuid        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, node_id),
    CONSTRAINT node_account_jf_user_uq UNIQUE (node_id, jf_user_id)
);

-- -----------------------------------------------------------------------------
-- Rentals
-- -----------------------------------------------------------------------------

CREATE TABLE rental (
    id            bigserial     PRIMARY KEY,
    copy_id       bigint        NOT NULL REFERENCES copy (id),
    user_id       int           NOT NULL REFERENCES app_user (id),
    status        rental_status NOT NULL DEFAULT 'PENDING',
    return_reason return_reason,
    rented_at     timestamptz   NOT NULL DEFAULT now(),
    due_at        timestamptz   NOT NULL,
    activated_at  timestamptz,
    returned_at   timestamptz,
    updated_at    timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT rental_due_after_start
        CHECK (due_at > rented_at),
    CONSTRAINT rental_reason_iff_returning
        CHECK ((status IN ('RETURNING', 'RETURNED', 'EXPIRED')) = (return_reason IS NOT NULL)),
    CONSTRAINT rental_expired_has_expired_reason
        CHECK (status <> 'EXPIRED' OR return_reason = 'EXPIRED'),
    CONSTRAINT rental_returned_has_user_or_admin_reason
        CHECK (status <> 'RETURNED' OR return_reason IN ('USER', 'ADMIN')),
    CONSTRAINT rental_returned_at_iff_closed
        CHECK ((status IN ('RETURNED', 'EXPIRED')) = (returned_at IS NOT NULL))
);

-- KEY INVARIANT: one copy = at most one "live" rental. Enforced by the
-- database, so no application bug or race can double-rent a disc.
CREATE UNIQUE INDEX one_active_rental_per_copy
    ON rental (copy_id)
    WHERE status IN ('PENDING', 'ACTIVE', 'RETURNING');

CREATE INDEX rental_user_live_idx ON rental (user_id) WHERE status IN ('PENDING', 'ACTIVE', 'RETURNING');
CREATE INDEX rental_due_idx ON rental (due_at) WHERE status = 'ACTIVE';
CREATE INDEX rental_pending_idx ON rental (rented_at) WHERE status = 'PENDING';
CREATE INDEX rental_returning_idx ON rental (updated_at) WHERE status = 'RETURNING';

-- Saga log: every state change of a rental, written by a trigger.
-- Append-only from the application's point of view (see 06_grants.sql).
CREATE TABLE rental_event (
    id          bigserial     PRIMARY KEY,
    rental_id   bigint        NOT NULL REFERENCES rental (id),
    from_status rental_status,
    to_status   rental_status NOT NULL,
    occurred_at timestamptz   NOT NULL DEFAULT now()
);

CREATE INDEX rental_event_rental_idx ON rental_event (rental_id, occurred_at);

-- -----------------------------------------------------------------------------
-- Session guard
-- -----------------------------------------------------------------------------

-- Playback sessions observed on the nodes. Lets the guard tell which session
-- of a copy came first, even across worker restarts.
CREATE TABLE playback_session (
    node_id       int         NOT NULL REFERENCES node (id),
    jf_session_id text        NOT NULL,
    copy_id       bigint      NOT NULL REFERENCES copy (id),
    jf_user_id    uuid,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (node_id, jf_session_id, copy_id)
);

-- Log of "one disc" violations.
CREATE TABLE violation (
    id            bigserial        PRIMARY KEY,
    node_id       int              NOT NULL REFERENCES node (id),
    copy_id       bigint           NOT NULL REFERENCES copy (id),
    user_id       int              REFERENCES app_user (id),  -- NULL: Jellyfin user not managed by JellyRent
    jf_user_id    uuid,
    jf_session_id text             NOT NULL,
    reason        violation_reason NOT NULL,
    detail        text,
    detected_at   timestamptz      NOT NULL DEFAULT now(),
    CONSTRAINT violation_once_per_session UNIQUE (node_id, jf_session_id, copy_id, reason)
);

CREATE INDEX violation_detected_idx ON violation (detected_at DESC);
