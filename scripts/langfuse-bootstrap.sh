#!/bin/sh
set -eu

echo "Waiting for Langfuse DB objects..."

DB_HOST="${LANGFUSE_PGHOST:-langfuse-postgres}"
DB_PORT="${LANGFUSE_PGPORT:-5432}"
DB_USER="${LANGFUSE_PGUSER:-langfuse}"
DB_NAME="${LANGFUSE_PGDATABASE:-langfuse}"
ORG_ID="${LANGFUSE_INIT_ORG_ID:-chronos-org}"
PROJECT_ID="${LANGFUSE_INIT_PROJECT_ID:-chronos-agent}"
USER_EMAIL="${LANGFUSE_INIT_USER_EMAIL:-admin@admin.com}"

export PGPASSWORD="${PGPASSWORD:-langfuse}"

i=0
while [ "$i" -lt 60 ]; do
  if pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
    USER_ID="$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -Atqc "SELECT id FROM users WHERE email = '$USER_EMAIL' LIMIT 1;")"
    PROJECT_EXISTS="$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -Atqc "SELECT 1 FROM projects WHERE id = '$PROJECT_ID' LIMIT 1;")"
    ORG_EXISTS="$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -Atqc "SELECT 1 FROM organizations WHERE id = '$ORG_ID' LIMIT 1;")"

    if [ -n "$USER_ID" ] && [ "$PROJECT_EXISTS" = "1" ] && [ "$ORG_EXISTS" = "1" ]; then
      ORG_MEMBERSHIP_ID="bootstrap-${ORG_ID}-${USER_ID}"

      psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" <<SQL
INSERT INTO organization_memberships (id, org_id, user_id, role)
VALUES ('$ORG_MEMBERSHIP_ID', '$ORG_ID', '$USER_ID', 'OWNER')
ON CONFLICT (org_id, user_id) DO NOTHING;

INSERT INTO project_memberships (project_id, user_id, org_membership_id, role)
SELECT '$PROJECT_ID', '$USER_ID', om.id, 'OWNER'
FROM organization_memberships om
WHERE om.org_id = '$ORG_ID' AND om.user_id = '$USER_ID'
ON CONFLICT (project_id, user_id) DO NOTHING;
SQL

      echo "Langfuse bootstrap completed for $USER_EMAIL -> $PROJECT_ID"
      exit 0
    fi
  fi

  i=$((i + 1))
  sleep 2
done

echo "Langfuse bootstrap timed out waiting for user/project/org"
exit 1
