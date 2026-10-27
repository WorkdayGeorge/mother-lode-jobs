"""Command line entry points.

    python -m app.cli migrate            # create/upgrade database tables
    python -m app.cli seed-sources       # load app/seed/sources.yaml (adds new keys only)
    python -m app.cli run                # one full worker pass (fetch, match, alert)
    python -m app.cli run --source ccwd  # fetch just one source
    python -m app.cli make-admin you@example.com
    python -m app.cli demo-data          # fake jobs for local design work
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import yaml
from sqlalchemy import select

from .db import session_scope

SEED_FILE = Path(__file__).parent / "seed" / "sources.yaml"


def migrate() -> None:
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(cfg, "head")


def seed_sources(path: Path = SEED_FILE) -> int:
    from .models import Source

    entries = yaml.safe_load(path.read_text()) or []
    added = 0
    with session_scope() as session:
        existing = set(session.scalars(select(Source.key)))
        for entry in entries:
            if entry["key"] in existing:
                continue
            session.add(
                Source(
                    key=entry["key"],
                    name=entry["name"],
                    kind=entry.get("kind", "employer"),
                    adapter=entry["adapter"],
                    config=entry.get("config") or {},
                    careers_url=entry.get("careers_url"),
                    county=entry.get("county"),
                    default_category=entry.get("default_category"),
                    active=entry.get("active", True),
                    notes=entry.get("notes"),
                )
            )
            added += 1
    return added


def make_admin(email: str) -> bool:
    from .models import User

    with session_scope() as session:
        user = session.scalar(select(User).where(User.email == email.lower()))
        if user is None:
            return False
        user.is_admin = True
        user.status = "active"
        return True


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    sub.add_parser("seed-sources")
    run = sub.add_parser("run")
    run.add_argument("--source", action="append", help="only this source key (repeatable)")
    run.add_argument("--no-alerts", action="store_true")
    admin = sub.add_parser("make-admin")
    admin.add_argument("email")
    sub.add_parser("demo-data")
    args = parser.parse_args(argv)

    if args.command == "migrate":
        migrate()
        print("Database is up to date.")
    elif args.command == "seed-sources":
        print(f"Added {seed_sources()} sources.")
    elif args.command == "run":
        from .pipeline import run_once

        with session_scope() as session:
            summary = run_once(session, keys=args.source, alerts=not args.no_alerts)
        print(json.dumps(summary, indent=2, default=str))
    elif args.command == "make-admin":
        if not make_admin(args.email):
            print("No user with that email. Sign up on the site first.", file=sys.stderr)
            return 1
        print(f"{args.email} is now an admin.")
    elif args.command == "demo-data":
        from .demo import load_demo

        print(f"Added {load_demo()} demo jobs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
