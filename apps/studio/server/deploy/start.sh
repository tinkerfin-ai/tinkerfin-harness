#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../../.env"
LOCAL=0
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yaml"
CONTAINER=0
STARTED=0

fail() { printf '部署失败：%s\n' "$*" >&2; exit 1; }
usage() {
    printf '用法：%s [--local | --container] [--env-file FILE]\n' "$0"
    printf '  --local       启动所选依赖，后端由用户在本机运行\n'
    printf '  --container   从当前源码构建并启动后端容器和所选依赖\n'
    printf '  --env-file    使用指定的统一配置文件\n'
    printf '不带模式参数时使用现成的后端镜像；依赖由 COMPOSE_PROFILES 选择\n'
}
while (($#)); do
    case "$1" in
        --local) LOCAL=1; shift ;;
        --container) CONTAINER=1; shift ;;
        --env-file) [[ $# -ge 2 ]] || fail "--env-file 需要路径"; ENV_FILE=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) fail "未知参数：$1" ;;
    esac
done
(( !LOCAL || !CONTAINER )) || fail "--local 不能与 --container 同用"
if ((LOCAL)); then
    COMPOSE_FILE="$SCRIPT_DIR/docker-compose-base.yaml"
    export OPEN_SANDBOX_PUBLIC_HOST=127.0.0.1
fi
[[ $ENV_FILE = /* ]] || ENV_FILE="$PWD/$ENV_FILE"
export STUDIO_ENV_FILE="$ENV_FILE"

compose() {
    docker compose --project-name tinkerfin --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}
handle_error() {
    local code=$?
    trap - ERR
    printf '\n部署失败（退出码 %s）\n' "$code" >&2
    if ((STARTED)); then
        compose ps || true
        compose logs --no-color --tail 60 || true
    fi
    exit "$code"
}
trap handle_error ERR
trap 'exit 130' INT
trap 'exit 143' TERM

command -v docker >/dev/null 2>&1 || fail "请先安装 Docker 和 Docker Compose"
docker info >/dev/null
version=$(docker compose version --short)
awk -v version="$version" 'BEGIN {
    sub(/^v/, "", version); split(version, n, ".");
    exit !((n[1]+0)>2 || ((n[1]+0)==2 && (n[2]+0)>=24))
}' || fail "Docker Compose 至少需要 2.24，当前为 $version"

"$SCRIPT_DIR/init-env.sh" "$ENV_FILE"
compose config --quiet
configuration=$(compose config --environment)
image=ghcr.io/tinkerfin-ai/studio-server:0.1.0
bind_address=127.0.0.1
port=8090
wait_timeout=600
s3_storage_bucket=
while IFS='=' read -r key value; do
    case "$key" in
        STUDIO_IMAGE) image=$value ;;
        STUDIO_BIND_ADDRESS) bind_address=$value ;;
        STUDIO_PORT) port=$value ;;
        DEPLOY_WAIT_TIMEOUT) wait_timeout=$value ;;
        S3_STORAGE_BUCKET) s3_storage_bucket=$value ;;
    esac
done <<< "$configuration"
[[ "$wait_timeout" =~ ^[1-9][0-9]*$ ]] || fail "DEPLOY_WAIT_TIMEOUT 必须是正整数秒数"

[[ -n "$s3_storage_bucket" ]] || fail "请在 $ENV_FILE 中填写 S3_STORAGE_BUCKET"
[[ ${#s3_storage_bucket} -ge 3 && ${#s3_storage_bucket} -le 63 && "$s3_storage_bucket" =~ ^[a-z0-9][a-z0-9.-]*[a-z0-9]$ && "$s3_storage_bucket" != *..* && "$s3_storage_bucket" != *.-* && "$s3_storage_bucket" != *-.* && ! "$s3_storage_bucket" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "S3_STORAGE_BUCKET 不是合法的 MinIO 桶名"

# 固定名称不接管其他项目或人工创建的容器
services=$(compose config --services)
while IFS= read -r service; do
    case "$service" in
        mysql) container=mysql8 ;;
        redis-runtime|minio|opensandbox) container=$service ;;
        *) continue ;;
    esac
    if owner=$(docker container inspect --format '{{index .Config.Labels "com.docker.compose.project"}}/{{index .Config.Labels "com.docker.compose.service"}}' "$container" 2>/dev/null); then
        [[ "$owner" == "tinkerfin/$service" ]] || fail "容器名 $container 已被其他资源使用，请先处理名称冲突"
    fi
done <<< "$services"

if ((CONTAINER)); then
    if [[ "$image" == ghcr.io/tinkerfin-ai/studio-server:0.1.0 ]]; then
        export STUDIO_IMAGE=tinkerfin-studio-server:local
        image=$STUDIO_IMAGE
    fi
    export STUDIO_IMAGE="$image"
    printf '构建后端镜像：%s\n' "$image"
    compose build --pull server
    services=$(compose config --services)
    dependencies=()
    while IFS= read -r service; do
        [[ "$service" == server || -z "$service" ]] || dependencies+=("$service")
    done <<< "$services"
    if ((${#dependencies[@]})); then compose pull "${dependencies[@]}"; fi
elif [[ -n "$services" ]]; then
    printf '拉取部署所需镜像\n'
    compose pull
fi

if [[ -n "$services" ]]; then
    printf '启动服务并等待就绪\n'
    STARTED=1
    compose up -d --no-build --pull never --wait --wait-timeout "$wait_timeout"
fi
if ((LOCAL)); then
    printf '\n配置已就绪。启动本机后端：\n  uv run --package tinkerfin-studio python -m tinkerfin_studio\n前端：\n  cd apps/studio/web && pnpm install --frozen-lockfile && pnpm dev\n'
    exit 0
fi
case "$bind_address" in 0.0.0.0|::) bind_address=127.0.0.1 ;; esac
[[ "$bind_address" != *:* || "$bind_address" == \[*\] ]] || bind_address="[$bind_address]"
printf '\nStudio 后端已就绪\n镜像：%s\nAPI：http://%s:%s/api\n健康检查：http://%s:%s/health/ready\n' \
    "$image" "$bind_address" "$port" "$bind_address" "$port"
