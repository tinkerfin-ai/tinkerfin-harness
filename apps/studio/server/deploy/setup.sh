#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${1:-$SCRIPT_DIR/.env}"
[[ $ENV_FILE = /* ]] || ENV_FILE="$PWD/$ENV_FILE"
SECRETS_DIR="$(dirname -- "$ENV_FILE")/secrets"
umask 077

fail() { printf '初始化失败：%s\n' "$*" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || fail "请先安装 Docker 和 Docker Compose"

if [[ ! -e "$ENV_FILE" ]]; then
    [[ ! -e "$SECRETS_DIR" ]] || fail "已有 Secrets，请恢复对应的 .env"
    mkdir -p "$(dirname -- "$ENV_FILE")"
    (set -o noclobber; cat "$SCRIPT_DIR/.env.example" > "$ENV_FILE")
    # 首次配置校验失败时允许直接重试，不改动已有配置或凭据
    trap '[[ -d "$SECRETS_DIR" ]] || rm -f -- "$ENV_FILE"' EXIT
elif [[ ! -d "$SECRETS_DIR" ]]; then
    fail "已有 .env 但 Secrets 缺失，请恢复凭据；首次安装请直接执行 setup.sh"
fi
[[ -f "$ENV_FILE" ]] || fail ".env 不是普通文件"

# 让 Compose 解析引号、插值和环境覆盖，不执行 .env 中的内容
configuration=$(STUDIO_ENV_FILE="$ENV_FILE" SECRETS_DIR="$SECRETS_DIR" \
    docker compose --env-file "$ENV_FILE" -f "$SCRIPT_DIR/docker-compose.yaml" config --environment)
mysql_host=mysql
mysql_port=3306
mysql_database=tinkerfin
mysql_user=studio
while IFS='=' read -r key value; do
    case "$key" in
        MYSQL_HOST) mysql_host=$value ;;
        MYSQL_PORT) mysql_port=$value ;;
        MYSQL_DATABASE) mysql_database=$value ;;
        MYSQL_USER) mysql_user=$value ;;
    esac
done <<< "$configuration"
[[ -n "$mysql_host" && -n "$mysql_database" && -n "$mysql_user" ]] || fail "MySQL 地址、库名和用户名不能为空"
if [[ ! "$mysql_port" =~ ^[0-9]+$ ]] || ! ((10#$mysql_port > 0 && 10#$mysql_port <= 65535)); then
    fail "MYSQL_PORT 必须在 1 至 65535 之间"
fi

password_files=(mysql_root_password mysql_password redis_runtime_password opensandbox_api_key s3_storage_access_key s3_storage_secret_key)
if [[ ! -e "$SECRETS_DIR" ]]; then
    mkdir "$SECRETS_DIR"
    for name in "${password_files[@]}"; do
        od -An -N32 -tx1 /dev/urandom | tr -d ' \n' > "$SECRETS_DIR/$name"
        printf '\n' >> "$SECRETS_DIR/$name"
    done
fi
for name in "${password_files[@]}"; do
    [[ -f "$SECRETS_DIR/$name" && -s "$SECRETS_DIR/$name" && ! -L "$SECRETS_DIR/$name" ]] \
        || fail "Secrets 不完整，请恢复 ${SECRETS_DIR}/${name}；已有密码不会自动重置"
done

urlencode() {
    local input=$1 index character code
    local LC_ALL=C
    for ((index=0; index<${#input}; index++)); do
        character=${input:index:1}
        case "$character" in
            [a-zA-Z0-9.~_-]) printf '%s' "$character" ;;
            *) printf -v code '%d' "'$character"; printf '%%%02X' "$((code & 255))" ;;
        esac
    done
}

# 地址随 .env 更新，账号密码保持已有 Secret；已有数据库账号需由管理员同步修改
if [[ "$mysql_host" == *:* && "$mysql_host" != \[*\] ]]; then
    mysql_host="[$mysql_host]"
fi
temporary=$(mktemp "$SECRETS_DIR/.database_url.XXXXXX")
trap 'rm -f -- "$temporary"' EXIT
printf 'mysql+asyncmy://%s:%s@%s:%s/%s?charset=utf8mb4\n' \
    "$(urlencode "$mysql_user")" "$(urlencode "$(cat "$SECRETS_DIR/mysql_password")")" \
    "$mysql_host" "$mysql_port" "$(urlencode "$mysql_database")" > "$temporary"
mv -f -- "$temporary" "$SECRETS_DIR/database_url"
# 宿主目录仅当前用户可进入，挂载的单个文件允许非 root 容器读取
chmod 700 "$SECRETS_DIR"
chmod 600 "$ENV_FILE"
chmod 644 "${password_files[@]/#/$SECRETS_DIR/}" "$SECRETS_DIR/database_url"
printf '配置已准备：%s\n' "$ENV_FILE"
printf '请在配置中填写 S3_STORAGE_BUCKET，再运行 deploy.sh\n'
