"""MittelConnect daemon and command-line entry point.

Commands:
  run              Start the daemon loop (default). Use --once for one cycle.
  check-config     Load, decrypt and validate config.yaml, print a redacted copy.
  generate-key     Create a new Fernet master key (stdout or 0600 key file).
  encrypt-secret   Encrypt a secret into an ENC[...] value for config.yaml.
  status           Print outbox, dead-letter and watermark state.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from core import __version__
from core.crypto import CryptoError, SecretBox, generate_key, write_key_file
from core.settings import ConfigError, Settings, load_settings

logger = logging.getLogger("mittelconnect")

STANDARD_RECORD_ATTRS = set(
    logging.LogRecord("x", 0, "x", 0, "x", None, None).__dict__.keys()
) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line; extra= fields are merged into the object."""

    def __init__(self, instance: str):
        super().__init__()
        self.instance = instance

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "instance": self.instance,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in STANDARD_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", fmt: str = "text", instance: str = "mittelconnect",
                      log_file: str = "", max_bytes: int = 10485760, backups: int = 10) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        )
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    if fmt == "json":
        formatter = JsonFormatter(instance)
        for handler in logging.getLogger().handlers:
            handler.setFormatter(formatter)
    # httpx logs every request URL at INFO; keep it at WARNING to avoid noise.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def touch_heartbeat(path: str) -> None:
    if not path:
        return
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(int(time.time())), encoding="ascii")
    except OSError as exc:
        logger.warning("Cannot write heartbeat file %s: %s", path, exc)


class Daemon:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.stop_event = asyncio.Event()
        service = settings.service
        self.interval = float(service.get("poll_interval_seconds", 60))
        self.cycle_timeout = float(service.get("cycle_timeout_seconds", 1800))
        self.heartbeat = service.get("heartbeat_file", "")
        self.grace = float(service.get("shutdown_grace_seconds", 30))
        self._signals_received = 0

    def _handle_signal(self, signame: str) -> None:
        self._signals_received += 1
        if self._signals_received == 1:
            logger.info("Received %s: finishing current work and shutting down", signame)
            self.stop_event.set()
        else:
            logger.warning("Received %s again: forcing exit", signame)
            os._exit(130)

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._handle_signal, sig.name)
            except (NotImplementedError, RuntimeError):
                signal.signal(sig, lambda signum, _frame: loop.call_soon_threadsafe(
                    self._handle_signal, signal.Signals(signum).name))

    async def run(self, once: bool = False) -> int:
        from core.pipeline import Pipeline

        self.install_signal_handlers()
        pipeline = Pipeline(self.settings)
        logger.info(
            "MittelConnect %s started", __version__,
            extra={"jobs": [job["name"] for job in self.settings.jobs], "interval_s": self.interval},
        )
        exit_code = 0
        consecutive_failures = 0
        try:
            while not self.stop_event.is_set():
                cycle = asyncio.create_task(pipeline.run_cycle(self.stop_event))
                try:
                    await asyncio.wait_for(asyncio.shield(cycle), timeout=self.cycle_timeout)
                    consecutive_failures = 0
                    touch_heartbeat(self.heartbeat)
                except asyncio.TimeoutError:
                    logger.error("Cycle exceeded %.0fs; cancelling", self.cycle_timeout)
                    cycle.cancel()
                    await asyncio.gather(cycle, return_exceptions=True)
                    consecutive_failures += 1
                except Exception:
                    logger.exception("Cycle crashed; the daemon keeps running")
                    consecutive_failures += 1
                if once:
                    break
                # Back off when cycles keep crashing (e.g. cache disk full).
                wait = self.interval * min(10, 2 ** consecutive_failures) if consecutive_failures else self.interval
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
        except Exception:
            logger.exception("Fatal error in daemon loop")
            exit_code = 1
        finally:
            try:
                await asyncio.wait_for(pipeline.close(), timeout=self.grace)
            except Exception:
                logger.exception("Error during shutdown")
                exit_code = exit_code or 1
            logger.info("MittelConnect stopped")
        return exit_code


# ------------------------------------------------------------------ commands
def cmd_run(args: argparse.Namespace) -> int:
    settings = _load(args.config)
    log_cfg = settings.logging
    configure_logging(
        level=args.log_level or log_cfg.get("level", "INFO"),
        fmt=log_cfg.get("format", "json"),
        instance=settings.service.get("instance_name", "mittelconnect"),
        log_file=log_cfg.get("file", ""),
        max_bytes=int(log_cfg.get("file_max_bytes", 10485760)),
        backups=int(log_cfg.get("file_backup_count", 10)),
    )
    return asyncio.run(Daemon(settings).run(once=args.once))


def cmd_check_config(args: argparse.Namespace) -> int:
    settings = _load(args.config)
    print(json.dumps(settings.redacted(), indent=2, ensure_ascii=False, default=str))
    print(f"\nOK: {len(settings.jobs)} enabled job(s), {len(settings.databases)} source(s)", file=sys.stderr)
    return 0


def cmd_generate_key(args: argparse.Namespace) -> int:
    if args.out:
        try:
            write_key_file(args.out)
        except CryptoError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"Master key written to {args.out} (mode 0600). Back it up offline.", file=sys.stderr)
    else:
        print(generate_key())
    return 0


def cmd_encrypt_secret(args: argparse.Namespace) -> int:
    env_name = args.key_env
    try:
        box = SecretBox.from_settings(env_name, args.key_file)
    except CryptoError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.stdin:
        secret = sys.stdin.read().rstrip("\n")
    else:
        secret = getpass.getpass("Secret to encrypt: ")
        if secret != getpass.getpass("Repeat: "):
            print("ERROR: values do not match", file=sys.stderr)
            return 2
    if not secret:
        print("ERROR: empty secret", file=sys.stderr)
        return 2
    print(box.wrap(secret))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from core.pipeline import LocalStore

    settings = _load(args.config)
    store = LocalStore(
        settings.cache.get("sqlite_path", "./data/mittelconnect_cache.db"),
        settings.secret_box,
        encrypt=bool(settings.security.get("encrypt_outbox", True)),
    )
    try:
        status = {
            "outbox_entries": store.outbox_entry_count(),
            "outbox_records": store.outbox_record_count(),
            "dead_letters": store.dead_letter_count(),
            "watermarks": {job["name"]: store.get_watermark(job["name"]) for job in settings.jobs},
        }
    finally:
        store.close()
    print(json.dumps(status, indent=2))
    return 0


def _load(path: str) -> Settings:
    try:
        return load_settings(path)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        sys.exit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mittelconnect", description="Legacy DB to SAP S/4HANA middleware")
    parser.add_argument("--version", action="version", version=f"MittelConnect {__version__}")
    parser.add_argument("-c", "--config", default=os.environ.get("MC_CONFIG", "config.yaml"))
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the daemon")
    run.add_argument("--once", action="store_true", help="run a single cycle and exit")
    run.add_argument("--log-level", default=None)
    run.set_defaults(func=cmd_run)

    check = sub.add_parser("check-config", help="validate configuration")
    check.set_defaults(func=cmd_check_config)

    gen = sub.add_parser("generate-key", help="generate a master key")
    gen.add_argument("--out", default="", help="write to a 0600 key file instead of stdout")
    gen.set_defaults(func=cmd_generate_key)

    enc = sub.add_parser("encrypt-secret", help="encrypt a secret for config.yaml")
    enc.add_argument("--key-env", default="MITTELCONNECT_MASTER_KEY")
    enc.add_argument("--key-file", default=os.environ.get("MC_MASTER_KEY_FILE", "/run/secrets/mittelconnect_master.key"))
    enc.add_argument("--stdin", action="store_true", help="read the secret from stdin (for automation)")
    enc.set_defaults(func=cmd_encrypt_secret)

    stat = sub.add_parser("status", help="show local cache state")
    stat.set_defaults(func=cmd_status)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args.func, args.once, args.log_level = cmd_run, False, None
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
