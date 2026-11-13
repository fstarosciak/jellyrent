-- =============================================================================
-- JellyRent central database - 06: privileges of the application role
--
-- Least privilege: the API and the worker can read everything they need and
-- change only what their use cases require. Notably they cannot
--   * insert or delete rentals other than through the rules (no DELETE),
--   * write the saga log (rental_event is written by a SECURITY DEFINER trigger),
--   * change node topology (column-level UPDATE on node).
-- =============================================================================

GRANT USAGE ON SCHEMA public TO jellyrent_app;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO jellyrent_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO jellyrent_app;

GRANT INSERT, UPDATE ON title, copy, app_user, node_account, rental, violation TO jellyrent_app;
GRANT INSERT, UPDATE, DELETE ON playback_session TO jellyrent_app;

-- media_item is the abstract parent: inserts go to the child tables only.
REVOKE INSERT, UPDATE, DELETE ON media_item FROM jellyrent_app;

-- Column-level privilege: the broker maintains runtime state and the
-- bootstrap job stores API keys, but topology stays owner-managed.
GRANT UPDATE (api_key, status, consecutive_failures, last_seen_at, last_sync_at, last_error)
    ON node TO jellyrent_app;
