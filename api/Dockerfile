# syntax=docker/dockerfile:1.7
#
# One image for the API, the watchdog worker and the bootstrap job.

ARG PYTHON_IMAGE=python:3.12.16-slim-bookworm

# --------------------------------------------------------------------------- build
FROM ${PYTHON_IMAGE} AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
# Dependencies first (cached until pyproject.toml changes), code second.
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py && pip install . && rm -rf app
COPY app ./app
RUN pip install --no-deps --force-reinstall .

# ------------------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE}

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 10001 jellyrent \
 && useradd --system --uid 10001 --gid jellyrent --no-create-home jellyrent

COPY --from=build /opt/venv /opt/venv

WORKDIR /tmp
USER jellyrent
EXPOSE 8000

CMD ["uvicorn", "--factory", "app.main:create_app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
