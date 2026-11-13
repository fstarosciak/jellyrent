-- =============================================================================
-- JellyRent central database - 03: functions, triggers, views
--
-- Error contract (SQLSTATE -> meaning), mirrored in api/app/errors.py:
--   JR001 RENTAL_LIMIT        user reached max_rentals
--   JR002 NO_COPY_AVAILABLE   no free copy of the title on an ONLINE node
--   JR003 ALREADY_RENTED      user already holds a live rental of this title
--   JR005 INVALID_TRANSITION  illegal rental state change
--   JR404 NOT_FOUND           referenced user / rental does not exist
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Generic helpers
-- -----------------------------------------------------------------------------

CREATE FUNCTION touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;

-- Triggers are not inherited either: attach to the concrete table.
CREATE TRIGGER title_touch_updated_at
    BEFORE UPDATE ON title
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- -----------------------------------------------------------------------------
-- Methods on composite types
-- (callable with functional or attribute notation: imdb_url(t.providers))
-- -----------------------------------------------------------------------------

CREATE FUNCTION imdb_url(p provider_ids) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN (p).imdb IS NULL THEN NULL
                ELSE 'https://www.imdb.com/title/' || (p).imdb || '/' END
$$;

CREATE FUNCTION resolution(m media_info) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN (m).height IS NULL THEN NULL
                WHEN (m).height >= 2160 THEN '4K'
                WHEN (m).height >= 1080 THEN '1080p'
                WHEN (m).height >= 720  THEN '720p'
                ELSE 'SD' END
$$;

-- -----------------------------------------------------------------------------
-- Methods on row types (copy / title)
-- Attribute notation works too: SELECT c.is_available, c.play_url FROM copy c;
-- -----------------------------------------------------------------------------

-- Jellyfin tag that grants access to this copy (AllowedTags on the node).
CREATE FUNCTION access_tag(c copy) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT 'rent-' || (c).id
$$;

-- Deep link to the copy in the node's Jellyfin web client.
CREATE FUNCTION play_url(c copy) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT rtrim(n.public_url, '/') || '/web/#/details?id=' || replace((c).jf_item_id::text, '-', '')
      FROM node n
     WHERE n.id = (c).node_id
$$;

-- A copy is available when the file is present, its node is ONLINE and it
-- has no live rental.
CREATE FUNCTION is_available(c copy) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT (c).is_present
       AND EXISTS (SELECT 1 FROM node n
                    WHERE n.id = (c).node_id AND n.status = 'ONLINE')
       AND NOT EXISTS (SELECT 1 FROM rental r
                        WHERE r.copy_id = (c).id
                          AND r.status IN ('PENDING', 'ACTIVE', 'RETURNING'))
$$;

CREATE FUNCTION available_copies(t title) RETURNS SETOF copy
LANGUAGE sql STABLE AS $$
    SELECT c.*
      FROM copy c
     WHERE c.title_id = (t).id
       AND is_available(c)
     ORDER BY c.node_id, c.id
$$;

-- -----------------------------------------------------------------------------
-- Rental state machine (enforced in the database)
-- -----------------------------------------------------------------------------

CREATE FUNCTION rental_transition_allowed(p_from rental_status, p_to rental_status)
RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE p_from
               WHEN 'PENDING'   THEN p_to IN ('ACTIVE', 'FAILED')
               WHEN 'ACTIVE'    THEN p_to = 'RETURNING'
               WHEN 'RETURNING' THEN p_to IN ('RETURNED', 'EXPIRED')
               ELSE false            -- RETURNED, EXPIRED, FAILED are terminal
           END
$$;

CREATE FUNCTION rental_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'PENDING' THEN
            RAISE EXCEPTION 'INVALID_TRANSITION'
                USING ERRCODE = 'JR005', DETAIL = 'a rental must start in PENDING';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.copy_id <> OLD.copy_id OR NEW.user_id <> OLD.user_id THEN
        RAISE EXCEPTION 'INVALID_TRANSITION'
            USING ERRCODE = 'JR005', DETAIL = 'copy_id and user_id are immutable';
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT rental_transition_allowed(OLD.status, NEW.status) THEN
            RAISE EXCEPTION 'INVALID_TRANSITION'
                USING ERRCODE = 'JR005', DETAIL = format('%s -> %s', OLD.status, NEW.status);
        END IF;
        IF NEW.status = 'ACTIVE' THEN
            NEW.activated_at := now();
        ELSIF NEW.status IN ('RETURNED', 'EXPIRED') THEN
            NEW.returned_at := now();
        END IF;
    END IF;

    NEW.updated_at := now();
    RETURN NEW;
END $$;

CREATE TRIGGER rental_guard
    BEFORE INSERT OR UPDATE ON rental
    FOR EACH ROW EXECUTE FUNCTION rental_guard();

-- Saga log. SECURITY DEFINER: the application role cannot write rental_event
-- directly, only through this trigger, which keeps the log append-only.
CREATE FUNCTION rental_log() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO rental_event (rental_id, from_status, to_status)
        VALUES (NEW.id, NULL, NEW.status);
    ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
        INSERT INTO rental_event (rental_id, from_status, to_status)
        VALUES (NEW.id, OLD.status, NEW.status);
    END IF;
    RETURN NULL;
END $$;

CREATE TRIGGER rental_log
    AFTER INSERT OR UPDATE OF status ON rental
    FOR EACH ROW EXECUTE FUNCTION rental_log();

-- -----------------------------------------------------------------------------
-- Rental operations
-- -----------------------------------------------------------------------------

-- Atomically picks a free copy of a title and reserves it (status PENDING).
--
-- Concurrency (READ COMMITTED):
--  * the app_user row lock serialises rentals of ONE user, so the limit check
--    cannot be raced;
--  * FOR UPDATE ... SKIP LOCKED makes parallel transactions for the same title
--    pick different copies instead of queueing on the same row;
--  * the partial unique index one_active_rental_per_copy is the last line of
--    defence. Its violation means a competitor committed after our snapshot
--    was taken; we retry, and the next statement sees a fresh snapshot.
--
-- Copy choice: only nodes where the user has an account, least loaded node
-- first (fewest live rentals), then lowest node id. p_node pins a node.
CREATE FUNCTION rent_title(
    p_user  int,
    p_title bigint,
    p_node  int DEFAULT NULL,
    p_hours int DEFAULT 48
) RETURNS rental
LANGUAGE plpgsql AS $$
DECLARE
    v_limit   int;
    v_live    int;
    v_copy_id bigint;
    v_rental  rental;
BEGIN
    IF p_hours IS NULL OR p_hours NOT BETWEEN 1 AND 720 THEN
        RAISE EXCEPTION 'INVALID_DURATION'
            USING ERRCODE = '22023', DETAIL = format('hours=%s, allowed 1..720', p_hours);
    END IF;

    SELECT max_rentals INTO v_limit FROM app_user WHERE id = p_user FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'NOT_FOUND' USING ERRCODE = 'JR404', DETAIL = format('user %s', p_user);
    END IF;

    SELECT count(*) INTO v_live
      FROM rental
     WHERE user_id = p_user AND status IN ('PENDING', 'ACTIVE');
    IF v_live >= v_limit THEN
        RAISE EXCEPTION 'RENTAL_LIMIT'
            USING ERRCODE = 'JR001', DETAIL = format('limit=%s', v_limit);
    END IF;

    IF EXISTS (SELECT 1
                 FROM rental r JOIN copy c ON c.id = r.copy_id
                WHERE r.user_id = p_user
                  AND c.title_id = p_title
                  AND r.status IN ('PENDING', 'ACTIVE')) THEN
        RAISE EXCEPTION 'ALREADY_RENTED' USING ERRCODE = 'JR003';
    END IF;

    FOR v_attempt IN 1..3 LOOP
        SELECT c.id INTO v_copy_id
          FROM copy c
          JOIN node n          ON n.id = c.node_id
          JOIN node_account na ON na.node_id = c.node_id AND na.user_id = p_user
         WHERE c.title_id = p_title
           AND c.is_present
           AND n.status = 'ONLINE'
           AND (p_node IS NULL OR c.node_id = p_node)
           AND NOT EXISTS (SELECT 1 FROM rental r
                            WHERE r.copy_id = c.id
                              AND r.status IN ('PENDING', 'ACTIVE', 'RETURNING'))
         ORDER BY (SELECT count(*)
                     FROM rental r JOIN copy c2 ON c2.id = r.copy_id
                    WHERE c2.node_id = c.node_id
                      AND r.status IN ('PENDING', 'ACTIVE', 'RETURNING')),
                  c.node_id,
                  c.id
         LIMIT 1
           FOR UPDATE OF c SKIP LOCKED;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'NO_COPY_AVAILABLE' USING ERRCODE = 'JR002';
        END IF;

        BEGIN
            INSERT INTO rental (copy_id, user_id, status, due_at)
            VALUES (v_copy_id, p_user, 'PENDING', now() + make_interval(hours => p_hours))
            RETURNING * INTO v_rental;
            RETURN v_rental;
        EXCEPTION WHEN unique_violation THEN
            NULL;  -- lost the race for this copy; retry with a fresh snapshot
        END;
    END LOOP;

    RAISE EXCEPTION 'NO_COPY_AVAILABLE' USING ERRCODE = 'JR002';
END $$;

-- Saga step 3 of renting: PENDING -> ACTIVE.
-- Compare-and-set: returns no row when the rental is no longer PENDING (e.g.
-- the repair job already compensated it), so the caller can react.
CREATE FUNCTION activate_rental(p_rental bigint) RETURNS SETOF rental
LANGUAGE sql AS $$
    UPDATE rental SET status = 'ACTIVE'
     WHERE id = p_rental AND status = 'PENDING'
    RETURNING *
$$;

-- Compensation of a failed rent: PENDING -> FAILED (compare-and-set).
CREATE FUNCTION fail_rental(p_rental bigint) RETURNS SETOF rental
LANGUAGE sql AS $$
    UPDATE rental SET status = 'FAILED'
     WHERE id = p_rental AND status = 'PENDING'
    RETURNING *
$$;

-- Return saga step 1: ACTIVE -> RETURNING. Idempotent for RETURNING.
CREATE FUNCTION begin_return(p_rental bigint, p_reason return_reason DEFAULT 'USER')
RETURNS rental
LANGUAGE plpgsql AS $$
DECLARE
    v_rental rental;
BEGIN
    UPDATE rental SET status = 'RETURNING', return_reason = p_reason
     WHERE id = p_rental AND status = 'ACTIVE'
    RETURNING * INTO v_rental;
    IF FOUND THEN
        RETURN v_rental;
    END IF;

    SELECT * INTO v_rental FROM rental WHERE id = p_rental;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'NOT_FOUND' USING ERRCODE = 'JR404', DETAIL = format('rental %s', p_rental);
    END IF;
    IF v_rental.status = 'RETURNING' THEN
        RETURN v_rental;
    END IF;
    RAISE EXCEPTION 'INVALID_TRANSITION'
        USING ERRCODE = 'JR005', DETAIL = format('cannot return a %s rental', v_rental.status);
END $$;

-- Return saga step 3: RETURNING -> RETURNED | EXPIRED (decided by the reason).
-- Idempotent for rentals that are already closed.
CREATE FUNCTION finish_return(p_rental bigint) RETURNS rental
LANGUAGE plpgsql AS $$
DECLARE
    v_rental rental;
BEGIN
    UPDATE rental
       SET status = CASE WHEN return_reason = 'EXPIRED' THEN 'EXPIRED'::rental_status
                         ELSE 'RETURNED'::rental_status END
     WHERE id = p_rental AND status = 'RETURNING'
    RETURNING * INTO v_rental;
    IF FOUND THEN
        RETURN v_rental;
    END IF;

    SELECT * INTO v_rental FROM rental WHERE id = p_rental;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'NOT_FOUND' USING ERRCODE = 'JR404', DETAIL = format('rental %s', p_rental);
    END IF;
    IF v_rental.status IN ('RETURNED', 'EXPIRED') THEN
        RETURN v_rental;
    END IF;
    RAISE EXCEPTION 'INVALID_TRANSITION'
        USING ERRCODE = 'JR005', DETAIL = format('cannot finish returning a %s rental', v_rental.status);
END $$;

-- -----------------------------------------------------------------------------
-- Read models
-- -----------------------------------------------------------------------------

-- Catalog with availability: "2/3 available - jf1, jf3".
CREATE VIEW title_availability AS
SELECT t.id                                                     AS title_id,
       t.name,
       t.year,
       t.genres,
       t.rating,
       t.providers,
       count(*)::int                                            AS total,
       (count(*) FILTER (WHERE is_available(c)))::int           AS available,
       array_agg(DISTINCT n.name ORDER BY n.name)               AS nodes,
       coalesce(array_agg(DISTINCT n.name ORDER BY n.name)
                    FILTER (WHERE is_available(c)), '{}')       AS available_nodes
  FROM title t
  JOIN copy c ON c.title_id = t.id AND c.is_present
  JOIN node n ON n.id = c.node_id
 GROUP BY t.id;

-- Node dashboard. Each copy has at most one live rental (unique index), so the
-- rental join cannot duplicate copies.
CREATE VIEW node_overview AS
SELECT n.id,
       n.name,
       n.status,
       n.public_url,
       n.last_seen_at,
       n.last_sync_at,
       n.last_error,
       (n.api_key IS NOT NULL)                         AS provisioned,
       (count(c.id) FILTER (WHERE c.is_present))::int  AS copies,
       count(r.id)::int                                AS live_rentals
  FROM node n
  LEFT JOIN copy c   ON c.node_id = n.id
  LEFT JOIN rental r ON r.copy_id = c.id AND r.status IN ('PENDING', 'ACTIVE', 'RETURNING')
 GROUP BY n.id;
