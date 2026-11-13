-- =============================================================================
-- JellyRent central database - 05: topology
--
-- One CALL per Jellyfin node. base_url is reachable from the broker (compose
-- network); public_url is what end users' browsers open; the SQLite path is
-- where docker-compose.yml mounts the node's /config volume (read-only).
-- API keys are filled in later by the bootstrap job (app.bootstrap).
-- =============================================================================

CALL register_node(1, 'jf1', 'http://jellyfin-1:8096', 'http://localhost:8096', '/jf/jf1/data/jellyfin.db');
CALL register_node(2, 'jf2', 'http://jellyfin-2:8096', 'http://localhost:8097', '/jf/jf2/data/jellyfin.db');
CALL register_node(3, 'jf3', 'http://jellyfin-3:8096', 'http://localhost:8098', '/jf/jf3/data/jellyfin.db');
