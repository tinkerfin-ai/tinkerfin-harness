#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
BUILD=0
EXTERNAL=0
STARTED=0

fail() { printf '部署失败：%s\n' "$*" >&2; exit 1; }
usage() {
    printf '用法：%s [--build] [--external] [--env-file FILE]\n' "$0"
    printf '  --build       从当前仓库源码构建后端镜像\n'
    printf '  --external    只启动后端，使用配置中的外部依赖\n'
    printf '  --env-file    使用指定的环境文件及其相邻 secrets 目录\n'
}
while (($#)); do
    case "$1" in
        --build) BUILD=1; shift ;;
        --external) EXTERNAL=1; shift ;;
        --env-file) [[ $# -ge 2 ]] || fail "--env-file 需要路径"; ENV_FILE=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) fail "未知参数：$1" ;;
    esac
done
[[ $ENV_FILE = /* ]] || ENV_FILE="$PWD/$ENV_FILE"
export STUDIO_ENV_FILE="$ENV_FILE"
SECRETS_DIR="$(dirname -- "$ENV_FILE")/secrets"
export SECRETS_DIR
if ((EXTERNAL)); then
    [[ -f "$ENV_FILE" ]] || fail "请先执行 setup.sh，并配置外部依赖地址和密码"
    export COMPOSE_PROFILES=""
fi

compose() {
    docker compose --env-file "$ENV_FILE" -f "$SCRIPT_DIR/docker-compose.yaml" "$@"
}
handle_error() {
    local code=$?
    trap - ERR
    printf '\n部署失败（退出码 %s）\n' "$code" >&2
    if ((STARTED)); then
        compose ps || true
        compose logs --no-color --tail 60 server || true
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

"$SCRIPT_DIR/setup.sh" "$ENV_FILE"
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

[[ -n "$s3_storage_bucket" ]] || fail "请在 $ENV_FILE 中填写 S3_STORAGE_BUCKET，桶名没有默认值"
[[ ${#s3_storage_bucket} -ge 3 && ${#s3_storage_bucket} -le 63 && "$s3_storage_bucket" =~ ^[a-z0-9][a-z0-9.-]*[a-z0-9]$ && "$s3_storage_bucket" != *..* && "$s3_storage_bucket" != *.-* && "$s3_storage_bucket" != *-.* && ! "$s3_storage_bucket" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "S3_STORAGE_BUCKET 不是合法的 MinIO 桶名"

if ((BUILD)); then
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
else
    printf '拉取后端与依赖镜像\n'
    compose pull
fi

printf '启动服务并等待就绪\n'
STARTED=1
compose up -d --force-recreate --remove-orphans --no-build --pull never --wait --wait-timeout "$wait_timeout"
case "$bind_address" in 0.0.0.0|::) bind_address=127.0.0.1 ;; esac
[[ "$bind_address" != *:* || "$bind_address" == \[*\] ]] || bind_address="[$bind_address]"
printf '\nStudio 后端已就绪\n镜像：%s\nAPI：http://%s:%s/api\n健康检查：http://%s:%s/health/ready\n' \
    "$image" "$bind_address" "$port" "$bind_address" "$port"
