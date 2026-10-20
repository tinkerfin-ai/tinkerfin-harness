#!/bin/bash

# MySQL 官方入口仅在空数据卷执行本脚本；组件表由框架创建
set -e
export MYSQL_PWD
MYSQL_PWD=$MYSQL_ROOT_PASSWORD
sql_literal() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\'/\'\'}
    printf "'%s'" "$value"
}
for kind in BUSINESS COMPONENTS; do
    database_key="MYSQL_${kind}_DATABASE"
    user_key="MYSQL_${kind}_USER"
    password_key="MYSQL_${kind}_PASSWORD"
    database=${!database_key}
    user=${!user_key}
    password=${!password_key}
    [[ "$database" =~ ^[a-zA-Z0-9_-]+$ ]] || { echo '无效数据库名称' >&2; exit 1; }
    # 授权库名中的下划线必须按字面匹配
    grant_database=${database//_/\\_}
    mysql --protocol=socket -uroot <<SQL
CREATE DATABASE IF NOT EXISTS \`$database\` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER $(sql_literal "$user")@'%' IDENTIFIED BY $(sql_literal "$password");
GRANT ALL PRIVILEGES ON \`$grant_database\`.* TO $(sql_literal "$user")@'%';
SQL
done
mysql --protocol=socket -uroot --database="$MYSQL_BUSINESS_DATABASE" < /opt/studio/schema.sql
unset MYSQL_PWD password
