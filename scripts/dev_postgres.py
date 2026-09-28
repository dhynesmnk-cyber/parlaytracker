"""Start (or reuse) a local PostgreSQL for development and tests, without Docker.

    pip install -e ".[dev,localdb]"
    python scripts/dev_postgres.py          # prints the TEST_DATABASE_URL to export
    python scripts/dev_postgres.py --stop

Data lives in .pgdata/ (git-ignored). The server keeps running after this script exits.
"""
import sys
from pathlib import Path

import pgserver

DATA_DIR = Path(__file__).resolve().parent.parent / ".pgdata"
DB_NAME = "parlaytracker_test"


def main() -> None:
    if "--stop" in sys.argv:
        pgserver.get_server(DATA_DIR, cleanup_mode="stop").cleanup()
        print("stopped")
        return
    server = pgserver.get_server(DATA_DIR, cleanup_mode=None)
    exists = server.psql(f"SELECT 1 FROM pg_database WHERE datname = '{DB_NAME}';")
    if "(1 row)" not in exists:
        server.psql(f"CREATE DATABASE {DB_NAME};")
    print(server.get_uri(DB_NAME))


if __name__ == "__main__":
    main()
