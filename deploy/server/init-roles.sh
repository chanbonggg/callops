#!/bin/sh
(
    set -eu
    # Supply passwords through psql variables, outside SQL files and command arguments.
    export PGOPTIONS='-c log_statement=none -c log_min_error_statement=panic'
    psql --no-psqlrc --username postgres --dbname callops --set ON_ERROR_STOP=1 --single-transaction \
        --file /etc/callops/bootstrap.sql --file - <<'SQL'
\getenv app_password CALLOPS_DB_APP_PASSWORD
\getenv migrator_password CALLOPS_DB_MIGRATOR_PASSWORD
ALTER ROLE callops_app PASSWORD :'app_password';
ALTER ROLE callops_migrator PASSWORD :'migrator_password';
SQL
)
