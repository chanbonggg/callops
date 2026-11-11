"""Explicit local commands; no implicit migrations or external connections."""

import argparse
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from callops.config import ConfigurationError, Settings
from callops.db import DatabaseError, migrate, readiness
from callops.calls.service import Service
from callops.common import Conflict


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "migrate", "ingest", "maintain", "serve"])
    parser.add_argument('--name', help='Completed file leaf name inside sync-inbox')
    parser.add_argument('--sha256', help='Checksum provided by the completed-transfer producer')
    parser.add_argument('--cert', type=Path, help='Local HTTPS certificate for the review UI')
    parser.add_argument('--key', type=Path, help='Local HTTPS private key outside this repository')
    args = parser.parse_args()
    try:
        settings = Settings.from_env(migration=args.command == "migrate")
        if args.command == "migrate":
            print(f"Applied {len(migrate(settings))} migration(s)")
        elif args.command == 'check':
            readiness(settings)
            print("Configuration and database ready")
        elif args.command == 'serve':
            if not args.cert or not args.key:
                parser.error('serve requires --cert and --key; secure session cookies require HTTPS')
            import uvicorn
            from callops.web import create_app

            uvicorn.run(create_app(settings), host='127.0.0.1', port=8443,
                        ssl_certfile=str(args.cert), ssl_keyfile=str(args.key),
                        access_log=False, proxy_headers=False)
        else:
            readiness(settings)
            service = Service(settings)
            if args.command == 'ingest':
                if not args.name or not args.sha256:
                    parser.error('ingest requires --name and producer --sha256')
                print('Accepted recording:', service.ingest(args.name, args.sha256))
            else:
                from callops.processing.queue import Queue
                service.recover()
                Queue(service).reap()
                print('Maintenance completed; offline worker copies may still require confirmation')
    except (ConfigurationError, DatabaseError, Conflict, OSError, psycopg.Error):
        print("FAIL: configuration/database unavailable; values withheld", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
