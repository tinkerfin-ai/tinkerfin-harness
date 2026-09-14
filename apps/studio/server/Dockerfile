# syntax=docker/dockerfile:1.7

FROM python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d AS python-base

FROM python-base AS artifacts
COPY --from=ghcr.io/astral-sh/uv:0.9.10@sha256:29bd45092ea8902c0bbb7f0a338f0494a382b1f4b18355df5be270ade679ff1d /uv /usr/local/bin/uv
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv lock --check \
    && uv export --quiet --frozen --package tinkerfin-studio --no-dev \
        --no-emit-workspace --no-header --format requirements.txt \
        --output-file /requirements.txt \
    && UV_DEFAULT_INDEX=${PIP_INDEX_URL} python scripts/build_wheels.py \
        --out-dir /wheels --python /usr/local/bin/python

FROM python-base AS dependencies
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DEFAULT_TIMEOUT=120
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY --from=artifacts /requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --disable-pip-version-check --require-hashes \
        --prefix=/opt/python -r /tmp/requirements.txt

FROM python-base
LABEL org.opencontainers.image.title="TinkerFin Studio Server" \
      org.opencontainers.image.source="https://github.com/tinkerfin-ai/tinkerfin-harness"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN groupadd --gid 10001 tinkerfin \
    && useradd --uid 10001 --gid 10001 --create-home tinkerfin \
    && chown -R 10001:10001 /app
COPY --from=dependencies /opt/python/ /usr/local/
COPY --from=artifacts /wheels/ /tmp/wheels/
RUN python -m pip install --disable-pip-version-check --no-deps /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels
USER 10001:10001
EXPOSE 8090
CMD ["python", "-m", "tinkerfin_studio", "--host", "0.0.0.0", "--port", "8090", "--no-reload"]
