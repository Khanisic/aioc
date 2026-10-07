"""Apply the schema to a database: ``python -m aioc.db <command>`` (Day 24).

    uv run python -m aioc.db status                  # what a migrate would do; changes nothing
    uv run python -m aioc.db migrate                 # apply every file the ledger lacks
    uv run python -m aioc.db baseline                # a database init/ built: record, run nothing
    uv run python -m aioc.db migrate --database-url postgresql://...

The database is `DATABASE_URL` (or the `POSTGRES_*` variables), the one reader the tool
servers use, unless ``--database-url`` names another. In the image this is the deploy
step that runs before the service starts. Exit 0 when the database matches the files,
1 when a file was refused, 2 when the database could not be reached.
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from .schema import SchemaError, baseline, migrate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m aioc.db", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("status", "migrate", "baseline"))
    parser.add_argument("--database-url", default=None, help="default: DATABASE_URL")
    args = parser.parse_args(argv)
    try:
        if args.command == "baseline":
            baseline(args.database_url)
        else:
            steps = migrate(args.database_url, dry_run=args.command == "status")
            todo = sum(1 for step in steps if step.action == "apply")
            if args.command == "status":
                print(f"{todo} file(s) to apply" if todo else "up to date")
            else:
                print(f"applied {todo} file(s)" if todo else "up to date; nothing applied")
    except SchemaError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except psycopg.OperationalError as exc:
        print(f"cannot reach the database: {' '.join(str(exc).split())}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
