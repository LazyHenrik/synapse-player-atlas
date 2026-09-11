import argparse
import contextlib
import json
import logging
import os
import signal
import sqlite3
import sys
from pathlib import Path

from monosuite_cli import Config

from .api import Source, make_client
from .collector import Collector, now_ms
from .storage import connect, initialize


@contextlib.contextmanager
def collector_lock(path):
    lock = open(str(path) + ".lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            if not lock.read(1):
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise RuntimeError("Another collector holds this database lock")
    try:
        yield
    finally:
        lock.close()


def main():
    parser = argparse.ArgumentParser(description="Synapse read-only player atlas")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("collect", "init", "serve", "demo", "backup"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--db", default=os.environ.get("SYNAPSE_DB", "synapse.sqlite"))
        if name in ("collect", "init"):
            cmd.add_argument(
                "--server",
                default=os.environ.get("MONOSUITE_SERVER_ID")
                or Config().load().get("server_id"),
            )
            cmd.add_argument("--interval", type=int, default=60)
        if name == "collect":
            cmd.add_argument(
                "--once",
                action="store_true",
                help="Validate schema, poll once, and perform one history step",
            )
            cmd.add_argument("--history-interval", type=int, default=21600)
            cmd.add_argument(
                "--log-lookback-days",
                type=int,
                default=7,
                help="Initial log backfill; 1–90 days, constrained by upstream retention",
            )
        if name == "serve":
            cmd.add_argument("--host", default="127.0.0.1")
            cmd.add_argument("--port", type=int, default=8787)
        if name == "backup":
            cmd.add_argument("destination")
    check = sub.add_parser("check-schema")
    check.add_argument(
        "--output", help="Save the public introspection result to this file"
    )
    sub.add_parser(
        "servers", help="List accessible server IDs without changing configuration"
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if args.command == "servers":
        source = Source(make_client())
        source.check_schema()
        print(json.dumps(source.query("servers"), indent=2))
    elif args.command == "check-schema":
        schema = Source(make_client()).check_schema()
        if args.output:
            Path(args.output).write_text(json.dumps(schema, indent=2), encoding="utf-8")
        print("Collector queries match the current MonoSuite schema.")
    elif args.command in ("collect", "init"):
        if not args.server:
            parser.error("--server or MONOSUITE_SERVER_ID is required")
        if args.interval < 10:
            parser.error("--interval must be at least 10 seconds")
        if args.command == "init":
            initialize(args.db, args.server, args.interval).close()
            return
        with collector_lock(args.db):
            collector = Collector(
                args.db,
                args.server,
                args.interval,
                args.history_interval,
                log_lookback_days=args.log_lookback_days,
            )
            if args.once:
                source = Source(make_client())
                source.check_schema()
                with contextlib.closing(connect(args.db)) as db:
                    if not collector.poll(db, source):
                        raise RuntimeError("Poll failed; see collector log")
                    collector.history_step(db, source)
                    from .log_collector import collect_log_step

                    collect_log_step(
                        db, source, args.server, now_ms(), args.log_lookback_days
                    )
            else:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    signal.signal(sig, lambda *_: collector.stop.set())
                collector.run()
    elif args.command == "serve":
        from waitress import serve
        from .web import create_app

        if os.environ.get(
            "SYNAPSE_REQUIRE_AUTH", "false"
        ).lower() == "true" and not os.environ.get("SYNAPSE_OAUTH_CONFIG"):
            raise ValueError(
                "OAuth is required; start with the OAuth Compose override and credential mount"
            )
        with contextlib.closing(connect(args.db, readonly=True)) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] != 2:
                raise ValueError("Initialize a compatible database before serving")
        print(f"Synapse viewer: http://{args.host}:{args.port}", flush=True)
        app = create_app(args.db)
        if os.environ.get("SYNAPSE_OAUTH_CONFIG"):
            from .auth import Auth

            app = Auth(
                app,
                os.environ["SYNAPSE_OAUTH_CONFIG"],
                os.environ.get("SYNAPSE_AUTH_DB", "/auth/sessions.sqlite"),
            )
        serve(
            app,
            host=args.host,
            port=args.port,
            threads=4,
            ident="Synapse",
            max_request_body_size=1024,
            max_request_header_size=8192,
        )
    elif args.command == "demo":
        from .demo import generate

        generate(args.db)
        print(f"Synthetic demo written to {args.db}")
    elif args.command == "backup":
        if Path(args.destination).exists():
            raise ValueError("Backup destination already exists")
        with contextlib.closing(connect(args.db, readonly=True)) as source:
            with contextlib.closing(sqlite3.connect(args.destination)) as dest:
                source.backup(dest)
        print("Consistent database backup completed.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        logging.error(
            "%s: %s",
            type(exc).__name__,
            (
                str(exc)
                if isinstance(exc, (ValueError, RuntimeError))
                else "Command failed; check configuration and credentials"
            ),
        )
        sys.exit(1)
