# syntax=docker/dockerfile:1.7

FROM python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d AS python-base

FROM python-base AS sources
COPY --from=ghcr.io/astral-sh/uv:0.9.10@sha256:29bd45092ea8902c0bbb7f0a338f0494a382b1f4b18355df5be270ade679ff1d /uv /usr/local/bin/uv
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv lock --check \
    && uv export --quiet --frozen --package tinkerfin-studio --no-dev \
        --no-emit-workspace --no-header --format requirements.txt \
        --output-file /requirements.txt
# 框架版本来自当前工作区元数据，生产构建不能回退到本地源码
RUN python - <<'PYTHON'
import tomllib
from pathlib import Path

from scripts.build_wheels import project_paths_for

requirements = []
for selected in project_paths_for("tinkerfin-studio"):
    member = Path(selected)
    if member.parts[0] != "packages":
        continue
    project = tomllib.loads((member / "pyproject.toml").read_text())["project"]
    requirements.append(f'{project["name"]}=={project["version"]}\n')
if not requirements:
    raise SystemExit("No framework projects found in the workspace")
Path("/framework-requirements.txt").write_text("".join(sorted(requirements)))

locked = tomllib.loads(Path("uv.lock").read_text())
backend = next(package for package in locked["package"] if package["name"] == "setuptools")
hashes = sorted({artifact["hash"] for artifact in [backend["sdist"], *backend["wheels"]]})
Path("/build-constraints.txt").write_text(
    f'{backend["name"]}=={backend["version"]}'
    + "".join(f" --hash={digest}" for digest in hashes)
    + "\n"
)
PYTHON

FROM sources AS studio-artifacts
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_BUILD_CONSTRAINT=/build-constraints.txt UV_REQUIRE_HASHES=true \
    UV_DEFAULT_INDEX=${PIP_INDEX_URL} python scripts/build_wheels.py apps/studio/server \
        --out-dir /wheels --python /usr/local/bin/python

FROM sources AS candidate-artifacts
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_BUILD_CONSTRAINT=/build-constraints.txt UV_REQUIRE_HASHES=true \
    UV_DEFAULT_INDEX=${PIP_INDEX_URL} python scripts/build_wheels.py \
        --package tinkerfin-studio --out-dir /wheels --python /usr/local/bin/python

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
COPY --from=sources /requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --disable-pip-version-check --require-hashes \
        --prefix=/opt/python -r /tmp/requirements.txt

FROM python-base AS runtime
LABEL org.opencontainers.image.title="TinkerFin Studio Server" \
      org.opencontainers.image.source="https://github.com/tinkerfin-ai/tinkerfin-harness"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 STUDIO_ENV_FILE=/app/.env
WORKDIR /app
RUN groupadd --gid 10001 tinkerfin \
    && useradd --uid 10001 --gid 10001 --create-home tinkerfin \
    && chown -R 10001:10001 /app
COPY --from=dependencies /opt/python/ /usr/local/
EXPOSE 8090
CMD ["python", "-m", "tinkerfin_studio", "--host", "0.0.0.0", "--port", "8090", "--no-reload"]

# 候选目标只用于验证尚未发布的框架源码，必须显式选择
FROM runtime AS candidate
COPY --from=candidate-artifacts /wheels/ /tmp/wheels/
RUN python -m pip install --disable-pip-version-check --no-deps /tmp/wheels/*.whl \
    && python -m pip check \
    && rm -rf /tmp/wheels
USER 10001:10001

FROM runtime AS production
COPY --from=studio-artifacts /wheels/ /tmp/wheels/
COPY --from=sources /framework-requirements.txt /tmp/framework-requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip --isolated install --disable-pip-version-check \
        --index-url https://pypi.org/simple --only-binary=:all: --no-deps \
        -r /tmp/framework-requirements.txt \
    && python -m pip install --disable-pip-version-check --no-deps /tmp/wheels/*.whl \
    && python -m pip check \
    && rm -rf /tmp/wheels /tmp/framework-requirements.txt
USER 10001:10001
