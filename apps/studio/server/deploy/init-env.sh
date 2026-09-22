#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
(($# <= 1)) || { printf '用法：%s [ENV_FILE]\n' "$0" >&2; exit 1; }
ENV_FILE="${1:-$SCRIPT_DIR/../../.env}"
[[ $ENV_FILE = /* ]] || ENV_FILE="$PWD/$ENV_FILE"
umask 077
fail() { printf '初始化失败：%s\n' "$*" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || fail "请先安装 Docker 和 Docker Compose"

# 唯一模板只在首次创建时填入随机密码；不执行 dotenv 中的任何内容
if [[ ! -e "$ENV_FILE" ]]; then
    mkdir -p "$(dirname -- "$ENV_FILE")"
    temporary=$(mktemp "${ENV_FILE}.XXXXXX")
    trap 'rm -f -- "$temporary"' EXIT
    while IFS= read -r line || [[ -n "$line" ]]; do
        case "$line" in
            MYSQL_ROOT_PASSWORD=|MYSQL_BUSINESS_PASSWORD=|MYSQL_COMPONENTS_PASSWORD=|REDIS_RUNTIME_PASSWORD=|OPEN_SANDBOX_API_KEY=|S3_STORAGE_ACCESS_KEY=|S3_STORAGE_SECRET_KEY=)
                password=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
                line="$line$password" ;;
        esac
        printf '%s\n' "$line" >> "$temporary"
    done < "$SCRIPT_DIR/../../.env.example"
    # 同时启动的初始化进程不能覆盖先完成的配置
    ln "$temporary" "$ENV_FILE" || fail "配置已存在，请重新执行"
    rm -f -- "$temporary"
fi
[[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail "配置必须是普通文件"
chmod 600 "$ENV_FILE"
compose() { STUDIO_ENV_FILE="$ENV_FILE" docker compose --env-file "$ENV_FILE" -f "$SCRIPT_DIR/docker-compose.yaml" "$@"; }
configuration=$(compose config --environment)
value() { printf '%s\n' "$configuration" | awk -v key="$1" 'index($0,key "=")==1 {print substr($0,length(key)+2); exit}'; }
# 只校验本次启动的内置服务，外部服务的连接由后端配置校验
services=$(compose config --services)
while IFS= read -r service; do
    case "$service" in
        mysql)
            keys=(MYSQL_ROOT_PASSWORD MYSQL_BUSINESS_PASSWORD MYSQL_COMPONENTS_PASSWORD)
            port_key=MYSQL_PUBLISHED_PORT; default_port=13306
            for key in MYSQL_BUSINESS_DATABASE MYSQL_COMPONENTS_DATABASE; do
                database=$(value "$key")
                [[ -z "$database" || "$database" =~ ^[a-zA-Z0-9_-]+$ ]] || fail "$key 只允许字母、数字、下划线和短横线"
            done
            business_database=$(value MYSQL_BUSINESS_DATABASE); components_database=$(value MYSQL_COMPONENTS_DATABASE)
            business_user=$(value MYSQL_BUSINESS_USER); components_user=$(value MYSQL_COMPONENTS_USER)
            [[ "${business_database:-tinkerfin}" != "${components_database:-tinkerfin_components}" && "${business_user:-tinkerfin}" != "${components_user:-tinkerfin_components}" ]] || fail "业务库与组件库必须使用不同库名和账号"
            ;;
        redis-runtime) keys=(REDIS_RUNTIME_PASSWORD); port_key=REDIS_RUNTIME_PUBLISHED_PORT; default_port=6379 ;;
        minio) keys=(S3_STORAGE_ACCESS_KEY S3_STORAGE_SECRET_KEY); port_key=S3_STORAGE_PUBLISHED_PORT; default_port=9000 ;;
        opensandbox) keys=(OPEN_SANDBOX_API_KEY); port_key=OPEN_SANDBOX_PUBLISHED_PORT; default_port=8091 ;;
        *) continue ;;
    esac
    for key in "${keys[@]}"; do
        [[ -n "$(value "$key")" ]] || fail "$key 未配置；已有凭据不会自动重置"
    done
    port=$(value "$port_key"); port=${port:-$default_port}
    [[ "$port" =~ ^[0-9]{1,5}$ ]] && ((10#$port > 0 && 10#$port <= 65535)) || fail "$port_key 必须在 1 至 65535 之间"
done <<< "$services"
printf '配置已准备：%s\n' "$ENV_FILE"
