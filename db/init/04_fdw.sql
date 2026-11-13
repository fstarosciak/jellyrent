-- =============================================================================
-- JellyRent central database - 04: distributed read layer (sqlite_fdw)
--
-- Every Jellyfin node keeps its library in its own SQLite file (jellyfin.db).
-- PostgreSQL attaches each file as a foreign server and exposes:
--
--   node_<name>.base_items, .base_item_providers, .media_stream_infos
--       foreign tables over the Jellyfin tables (explicit column contract)
--   node_<name>.movies
--       the node's horizontal fragment of the library, normalised
--   global_library
--       UNION ALL of all fragments + node_id = global schema with
--       location transparency
--
-- The Jellyfin schema is version-specific (pinned image, see
-- docs/jellyfin_schema.md). It is referenced ONLY in attach_node_fragment().
--
-- Foreign tables are declared explicitly instead of IMPORT FOREIGN SCHEMA:
-- declaring them does not open the SQLite file, so the schema can be created
-- before the Jellyfin nodes have ever started, and the columns we depend on
-- are written down as a contract.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS sqlite_fdw;

-- Attaches one node's SQLite database as a fragment.
CREATE PROCEDURE attach_node_fragment(p_node_name text, p_sqlite_path text)
LANGUAGE plpgsql AS $proc$
DECLARE
    v_server text := 'jf_' || p_node_name;
    v_schema text := 'node_' || p_node_name;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_foreign_server WHERE srvname = v_server) THEN
        EXECUTE format('ALTER SERVER %I OPTIONS (SET database %L)', v_server, p_sqlite_path);
    ELSE
        -- force_readonly: SQLite opens the file read-only, so the broker can
        -- never write to a node's database (requirement NF3).
        -- keep_connections off: do not hold the file open between queries,
        -- so that Jellyfin can checkpoint its WAL freely.
        EXECUTE format(
            'CREATE SERVER %I FOREIGN DATA WRAPPER sqlite_fdw '
            'OPTIONS (database %L, force_readonly %L, keep_connections %L)',
            v_server, p_sqlite_path, 'true', 'false');
    END IF;

    EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', v_schema);

    EXECUTE format($sql$
        CREATE FOREIGN TABLE IF NOT EXISTS %1$I.base_items (
            item_id          text             OPTIONS (column_name 'Id'),
            type             text             OPTIONS (column_name 'Type'),
            name             text             OPTIONS (column_name 'Name'),
            production_year  integer          OPTIONS (column_name 'ProductionYear'),
            path             text             OPTIONS (column_name 'Path'),
            run_time_ticks   bigint           OPTIONS (column_name 'RunTimeTicks'),
            genres           text             OPTIONS (column_name 'Genres'),
            tags             text             OPTIONS (column_name 'Tags'),
            community_rating double precision OPTIONS (column_name 'CommunityRating'),
            width            integer          OPTIONS (column_name 'Width'),
            height           integer          OPTIONS (column_name 'Height'),
            is_virtual_item  integer          OPTIONS (column_name 'IsVirtualItem')
        ) SERVER %2$I OPTIONS (table 'BaseItems', updatable 'false')
    $sql$, v_schema, v_server);

    EXECUTE format($sql$
        CREATE FOREIGN TABLE IF NOT EXISTS %1$I.base_item_providers (
            item_id        text OPTIONS (column_name 'ItemId'),
            provider_id    text OPTIONS (column_name 'ProviderId'),
            provider_value text OPTIONS (column_name 'ProviderValue')
        ) SERVER %2$I OPTIONS (table 'BaseItemProviders', updatable 'false')
    $sql$, v_schema, v_server);

    EXECUTE format($sql$
        CREATE FOREIGN TABLE IF NOT EXISTS %1$I.media_stream_infos (
            item_id      text    OPTIONS (column_name 'ItemId'),
            stream_index integer OPTIONS (column_name 'StreamIndex'),
            stream_type  integer OPTIONS (column_name 'StreamType'),
            codec        text    OPTIONS (column_name 'Codec')
        ) SERVER %2$I OPTIONS (table 'MediaStreamInfos', updatable 'false')
    $sql$, v_schema, v_server);

    -- The node's fragment of the library in the broker's vocabulary.
    -- Jellyfin stores GUIDs as text, multi-valued fields pipe-separated,
    -- runtime in 100 ns ticks and stream types as an enum (1 = Video).
    EXECUTE format($sql$
        CREATE OR REPLACE VIEW %1$I.movies AS
        SELECT bi.item_id::uuid                                        AS jf_item_id,
               bi.name                                                 AS name,
               bi.production_year                                      AS year,
               bi.path                                                 AS path,
               imdb.provider_value                                     AS imdb,
               tmdb.provider_value                                     AS tmdb,
               coalesce(string_to_array(nullif(bi.genres, ''), '|'), '{}') AS genres,
               coalesce(string_to_array(nullif(bi.tags, ''), '|'), '{}')   AS tags,
               round(bi.community_rating::numeric, 1)                  AS rating,
               lower(substring(bi.path FROM '\.([A-Za-z0-9]+)$'))      AS container,
               vs.codec                                                AS video_codec,
               bi.width                                                AS width,
               bi.height                                               AS height,
               (bi.run_time_ticks / 10000000)::int                     AS runtime_sec
          FROM %1$I.base_items bi
          LEFT JOIN %1$I.base_item_providers imdb
                 ON imdb.item_id = bi.item_id AND lower(imdb.provider_id) = 'imdb'
          LEFT JOIN %1$I.base_item_providers tmdb
                 ON tmdb.item_id = bi.item_id AND lower(tmdb.provider_id) = 'tmdb'
          LEFT JOIN LATERAL (
                SELECT ms.codec
                  FROM %1$I.media_stream_infos ms
                 WHERE ms.item_id = bi.item_id AND ms.stream_type = 1
                 ORDER BY ms.stream_index
                 LIMIT 1) vs ON true
         WHERE bi.type = 'MediaBrowser.Controller.Entities.Movies.Movie'
           AND coalesce(bi.is_virtual_item, 0) = 0
    $sql$, v_schema);

    -- Schemas created after 06_grants.sql ran need their own grants.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jellyrent_app') THEN
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO jellyrent_app', v_schema);
        EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO jellyrent_app', v_schema);
    END IF;
END $proc$;

-- (Re)creates global_library as UNION ALL over the fragments of all nodes.
-- A distributed UNION ALL is only as available as its least available
-- fragment: if one SQLite file cannot be opened, the whole query fails. That
-- is why the application reads fragments one by one and materialises them in
-- the central catalog (see app/catalog_sync.py).
CREATE PROCEDURE rebuild_global_library()
LANGUAGE plpgsql AS $proc$
DECLARE
    v_sql text;
BEGIN
    SELECT string_agg(
               format('SELECT %s AS node_id, m.* FROM %I.movies m', n.id, 'node_' || n.name),
               E'\nUNION ALL\n' ORDER BY n.id)
      INTO v_sql
      FROM node n;

    IF v_sql IS NULL THEN
        RAISE NOTICE 'no nodes registered, global_library not created';
        RETURN;
    END IF;

    EXECUTE 'CREATE OR REPLACE VIEW global_library AS ' || v_sql;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jellyrent_app') THEN
        GRANT SELECT ON global_library TO jellyrent_app;
    END IF;
END $proc$;

-- Registers (or updates) a node: topology row + FDW fragment + global view.
-- Adding a fourth node is a single CALL.
CREATE PROCEDURE register_node(
    p_id          int,
    p_name        text,
    p_base_url    text,
    p_public_url  text,
    p_sqlite_path text
)
LANGUAGE plpgsql AS $proc$
BEGIN
    INSERT INTO node (id, name, base_url, public_url, sqlite_path)
    VALUES (p_id, p_name, p_base_url, p_public_url, p_sqlite_path)
    ON CONFLICT (id) DO UPDATE
        SET name        = EXCLUDED.name,
            base_url    = EXCLUDED.base_url,
            public_url  = EXCLUDED.public_url,
            sqlite_path = EXCLUDED.sqlite_path;

    CALL attach_node_fragment(p_name, p_sqlite_path);
    CALL rebuild_global_library();
END $proc$;
