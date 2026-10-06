#!/usr/bin/env python3
"""
Windows-friendly port of scripts/bootstrap_sandbox.sh and scripts/outage_drill.sh.

Needs only Docker Desktop (running) and Python 3. No bash, openssl or curl.

Usage (from anywhere):
    python sandbox.py find                 # locate the MittelConnect folder
    python sandbox.py bootstrap [--force]  # build image, master key, encrypted .env
    python sandbox.py up                   # docker compose up -d --build
    python sandbox.py drill [seconds]      # SAP outage drill (default 90 s)
    python sandbox.py all                  # bootstrap + up + wait healthy + drill
    python sandbox.py down                 # stop containers, keep data volumes
    python sandbox.py reset                # stop, wipe volumes, secrets/ and .env
"""
import json
import os
import secrets
import shutil
import string
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

IMAGE = "mittelconnect:1.0.0"
STATS_URL = "http://127.0.0.1:8080/_mock/stats"


# ------------------------------------------------------------------ helpers
def find_project() -> Path:
    """Search this script's parent, then Desktop/Downloads/Documents (incl. OneDrive)."""
    here = Path(__file__).resolve().parent.parent
    if (here / "scripts" / "bootstrap_sandbox.sh").exists():
        return here
    home = Path.home()
    roots = [home / d for d in ("Desktop", "Downloads", "Documents")]
    roots += [home / "OneDrive" / d for d in ("Desktop", "Documents")]
    for root in roots:
        if not root.exists():
            continue
        for hit in root.rglob("bootstrap_sandbox.sh"):
            if hit.parent.name == "scripts":
                return hit.parent.parent
    sys.exit("Could not find a folder containing scripts/bootstrap_sandbox.sh")


def run(args, *, input=None, check=True, capture=True, cwd=None) -> str:
    res = subprocess.run(args, input=input, text=True, cwd=cwd,
                         capture_output=capture, check=False)
    if check and res.returncode != 0:
        msg = (res.stderr or "").strip() if capture else ""
        raise SystemExit(f"Command failed ({res.returncode}): {' '.join(args)}\n{msg}")
    return (res.stdout or "").strip() if capture else ""


def require_docker():
    if not shutil.which("docker"):
        sys.exit("docker is not installed (install Docker Desktop)")
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        sys.exit("Docker is installed but not running: start Docker Desktop first")


def rand_password() -> str:
    # 24 alphanumerics plus a fixed suffix satisfying SQL Server's complexity policy.
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(24)) + "Aa1#"


# ---------------------------------------------------------------- bootstrap
def bootstrap(root: Path, force: bool = False):
    require_docker()
    key_file = root / "secrets" / "master.key"
    if key_file.exists() and not force:
        print("secrets/master.key already exists; sandbox is bootstrapped.")
        print("Use 'bootstrap --force' (and 'docker compose down -v') to regenerate everything.")
        return

    print(f"==> Building {IMAGE}")
    run(["docker", "build", "-t", IMAGE, "."], cwd=root, capture=False)

    print("==> Generating master key")
    key_file.parent.mkdir(exist_ok=True)
    if key_file.exists():
        os.chmod(key_file, 0o600)
        key_file.unlink()
    master_key = run(["docker", "run", "--rm", "--network", "none", IMAGE, "generate-key"])
    key_file.write_text(master_key + "\n", newline="\n")
    os.chmod(key_file, 0o444)

    def encrypt(value: str) -> str:
        return run(["docker", "run", "--rm", "-i", "--network", "none",
                    "-e", f"MITTELCONNECT_MASTER_KEY={master_key}",
                    IMAGE, "encrypt-secret", "--stdin"], input=value)

    print("==> Generating and encrypting secrets")
    sa_password = rand_password()
    reader_password = rand_password()
    sap_secret = secrets.token_hex(24)
    pseudo_key = secrets.token_hex(32)

    env = "\n".join([
        f"MSSQL_SA_PASSWORD={sa_password}",
        f"MC_READER_PASSWORD={reader_password}",
        f"MOCK_SAP_CLIENT_SECRET={sap_secret}",
        f"MC_MSSQL_PASSWORD_ENC={encrypt(reader_password)}",
        f"MC_SAP_CLIENT_SECRET_ENC={encrypt(sap_secret)}",
        f"MC_PSEUDONYMIZATION_KEY_ENC={encrypt(pseudo_key)}",
        "MC_LOG_LEVEL=INFO",
        "MC_LOG_FORMAT=json",
        "SIMULATOR_INTERVAL=15",
    ]) + "\n"
    env_file = root / ".env"
    env_file.write_text(env, newline="\n")
    os.chmod(env_file, 0o600)
    print("==> Done. Start the sandbox with: python sandbox.py up")


def up(root: Path):
    require_docker()
    run(["docker", "compose", "up", "-d", "--build"], cwd=root, capture=False)


def down(root: Path, wipe: bool = False):
    """Stop the sandbox. wipe=True also deletes volumes, secrets/ and .env, so the
    next 'bootstrap' starts from scratch (the SQL Server volume keeps the old SA
    password, which is why volumes and secrets must be reset together)."""
    require_docker()
    args = ["docker", "compose", "down", "--remove-orphans"]
    if wipe:
        args.append("-v")
    run(args, cwd=root, capture=False)
    if wipe:
        key_file = root / "secrets" / "master.key"
        if key_file.exists():
            os.chmod(key_file, 0o600)
        shutil.rmtree(root / "secrets", ignore_errors=True)
        (root / ".env").unlink(missing_ok=True)
        print("==> Volumes, secrets/ and .env removed. Next: python sandbox.py all")


# -------------------------------------------------------------------- drill
class Drill:
    def __init__(self, root: Path, outage_seconds: int):
        self.root = root
        self.outage = outage_seconds
        self.recovery_timeout = int(os.environ.get("RECOVERY_TIMEOUT", "300"))
        cfg = json.loads(run(["docker", "compose", "config", "--format", "json"], cwd=root))
        self.sap_network = f"{cfg['name']}_sap"
        self.failed = False
        self.link_cut = False

    # --- probes
    def dc(self, *args, check=True):
        return run(["docker", "compose", *args], cwd=self.root, check=check)

    def middleware_id(self) -> str:
        return self.dc("ps", "-q", "middleware", check=False)

    def status_field(self, field: str) -> int:
        out = self.dc("exec", "-T", "middleware", "python", "/app/main.py", "status")
        return int(json.loads(out)[field])

    def received(self) -> int:
        with urllib.request.urlopen(STATS_URL, timeout=10) as r:
            return int(json.load(r)["received"])

    def health(self) -> str:
        return run(["docker", "inspect", "--format",
                    "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                    self.middleware_id()], check=False)

    # --- reporting
    @staticmethod
    def log(msg):
        print(f"\n[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    def check(self, ok: bool, good: str, bad: str):
        if ok:
            print(f"  PASS  {good}", flush=True)
        else:
            print(f"  FAIL  {bad}", flush=True)
            self.failed = True

    def cut_link(self):
        run(["docker", "network", "disconnect", self.sap_network, self.middleware_id()], check=False)

    def restore_link(self):
        if self.link_cut:
            self.log("Restoring the SAP network link")
            run(["docker", "network", "connect", self.sap_network, self.middleware_id()], check=False)
            self.link_cut = False

    def run(self) -> int:
        try:
            return self._run()
        finally:
            self.restore_link()

    def _run(self) -> int:
        self.log("Preflight")
        if not self.middleware_id():
            print("The sandbox is not running. Start it with: python sandbox.py up", file=sys.stderr)
            return 1
        h = self.health()
        self.check(h == "healthy", "middleware is healthy", f"middleware is {h} before the drill")
        outbox_before = self.status_field("outbox_records")
        received_before = self.received()
        print(f"  outbox_records={outbox_before}  sap_received={received_before}")
        self.check(outbox_before == 0, "outbox is empty", f"outbox already holds {outbox_before} records")

        self.log(f"Cutting the middleware's link to SAP for {self.outage}s "
                 "(database link and simulator keep running)")
        run(["docker", "network", "disconnect", self.sap_network, self.middleware_id()])
        self.link_cut = True

        half = self.outage // 2
        time.sleep(half)
        outbox_mid = self.status_field("outbox_records")
        print(f"  outbox_records={outbox_mid} after {half}s")
        self.check(outbox_mid > 0, "new records are cached locally", "nothing was cached during the outage")

        self.log("Restarting the middleware during the outage (cache must survive)")
        self.dc("restart", "middleware")
        self.cut_link()  # make sure the link is still cut after the restart
        outbox_restart = self.status_field("outbox_records")
        self.check(outbox_restart >= outbox_mid,
                   f"cache survived the restart ({outbox_restart} records)",
                   f"cache shrank across the restart ({outbox_mid} -> {outbox_restart})")

        time.sleep(self.outage - half)
        state = run(["docker", "inspect", "--format", "{{.State.Status}}", self.middleware_id()], check=False)
        self.check(state == "running", "middleware kept running during the outage", f"middleware is {state}")
        outbox_peak = self.status_field("outbox_records")
        print(f"  outbox_records={outbox_peak} at the end of the outage")
        logs = self.dc("logs", "--since", f"{self.outage}s", "middleware", check=False)
        self.check("Circuit 'sap' opened" in logs, "circuit breaker opened", "circuit breaker did not open")

        self.restore_link()
        self.log(f"Waiting up to {self.recovery_timeout}s for the outbox to drain")
        deadline = time.time() + self.recovery_timeout
        while True:
            outbox_now = self.status_field("outbox_records")
            if outbox_now == 0 or time.time() >= deadline:
                break
            print(f"  outbox_records={outbox_now}, waiting", flush=True)
            time.sleep(10)

        delivered = self.received() - received_before
        self.check(outbox_now == 0, "outbox drained", f"outbox still holds {outbox_now} records")
        self.check(delivered >= outbox_peak,
                   f"SAP received {delivered} new records (at least the {outbox_peak} that were cached)",
                   f"SAP received only {delivered} records, {outbox_peak} were cached")
        health_deadline = time.time() + 150
        while self.health() != "healthy" and time.time() < health_deadline:
            time.sleep(5)
        h = self.health()
        self.check(h == "healthy", "middleware is healthy after recovery", f"middleware is {h} after recovery")

        self.log("Result")
        print("  DRILL FAILED: see the FAIL lines above and 'docker compose logs middleware'"
              if self.failed else "  DRILL PASSED")
        return 1 if self.failed else 0


def wait_healthy(root: Path, timeout: int = 300):
    print(f"==> Waiting up to {timeout}s for the middleware to become healthy")
    deadline = time.time() + timeout
    while time.time() < deadline:
        cid = run(["docker", "compose", "ps", "-q", "middleware"], cwd=root, check=False)
        if cid and run(["docker", "inspect", "--format", "{{.State.Health.Status}}", cid], check=False) == "healthy":
            print("    middleware is healthy")
            return
        time.sleep(5)
    sys.exit("middleware did not become healthy; check 'docker compose logs middleware'")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    root = find_project()
    print(f"Project folder: {root}")
    if cmd == "find":
        return
    if cmd == "bootstrap":
        bootstrap(root, force="--force" in sys.argv)
    elif cmd == "up":
        up(root)
    elif cmd in ("down", "reset"):
        down(root, wipe=cmd == "reset")
    elif cmd == "drill":
        require_docker()
        seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 90
        sys.exit(Drill(root, seconds).run())
    elif cmd == "all":
        bootstrap(root)
        up(root)
        wait_healthy(root)
        sys.exit(Drill(root, 90).run())
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
