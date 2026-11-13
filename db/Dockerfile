# syntax=docker/dockerfile:1.7
#
# Central database image: PostgreSQL 16 + sqlite_fdw (pgspider), built from source.
# Multi-stage build keeps compilers and headers out of the runtime image.

ARG PG_IMAGE=postgres:16.15-bookworm

# --------------------------------------------------------------------------- build
FROM ${PG_IMAGE} AS build

ARG SQLITE_FDW_VERSION=2.5.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl libsqlite3-dev "postgresql-server-dev-${PG_MAJOR}" \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN curl -fsSL "https://github.com/pgspider/sqlite_fdw/archive/refs/tags/v${SQLITE_FDW_VERSION}.tar.gz" \
      | tar xz --strip-components=1 \
 && make USE_PGXS=1 \
 && make USE_PGXS=1 install DESTDIR=/out

# --------------------------------------------------------------------------- runtime
FROM ${PG_IMAGE}

# libsqlite3 is the runtime dependency of the FDW; the sqlite3 CLI is handy for
# inspecting the Jellyfin databases (see docs/jellyfin_schema.md).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libsqlite3-0 sqlite3 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /out/ /
COPY --chmod=0755 init/ /docker-entrypoint-initdb.d/
