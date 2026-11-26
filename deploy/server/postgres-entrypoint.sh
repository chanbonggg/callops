#!/bin/sh
set -eu

install -d -m 700 -o postgres -g postgres /run/callops-db-tls
install -m 644 -o postgres -g postgres /db-tls/db.crt /run/callops-db-tls/server.crt
install -m 600 -o postgres -g postgres /db-tls/db.key /run/callops-db-tls/server.key
exec docker-entrypoint.sh postgres \
    -c ssl=on \
    -c ssl_cert_file=/run/callops-db-tls/server.crt \
    -c ssl_key_file=/run/callops-db-tls/server.key \
    -c hba_file=/etc/callops/pg_hba.conf
