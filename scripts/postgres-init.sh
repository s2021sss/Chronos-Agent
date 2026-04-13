#!/bin/bash
# Создаёт дополнительные базы данных и пользователей для LiteLLM и Langfuse
# в рамках единого PostgreSQL-контейнера.
# Скрипт запускается автоматически при первом старте контейнера
# (только если каталог данных пустой).
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE USER litellm WITH PASSWORD 'litellm';
    CREATE DATABASE litellm OWNER litellm;
    GRANT ALL PRIVILEGES ON DATABASE litellm TO litellm;

    CREATE USER langfuse WITH PASSWORD 'langfuse';
    CREATE DATABASE langfuse OWNER langfuse;
    GRANT ALL PRIVILEGES ON DATABASE langfuse TO langfuse;
EOSQL
