#!/usr/bin/env bash
# =============================================================================
# Deploy MittelConnect to a site host over SSH (run from your workstation).
#
#   scripts/deploy.sh deploy@<host>                 update code and restart
#   scripts/deploy.sh deploy@<host> --init <site>   first deployment: also
#                                                   creates key and credentials
#   scripts/deploy.sh deploy@<host> --status        show health only
#
# Steps: copy the code (never secrets, data or local .env files) to
# /srv/mittelconnect/app, build both images on the host, validate the site
# configuration with check-config, start the stack, wait for it to report
# healthy. A failed check-config leaves the running version untouched.
# =============================================================================
set -euo pipefail

TARGET="${1:-}"
MODE="${2:-}"
SITE_NAME="${3:-}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_APP="/srv/mittelconnect/app"
REMOTE_SITE="/srv/mittelconnect/site"
COMPOSE="docker compose -f $REMOTE_APP/deploy/docker-compose.prod.yml --env-file $REMOTE_SITE/.env"
HEALTH_TIMEOUT="${MC_HEALTH_TIMEOUT:-300}"

usage() {
  sed -n '4,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

[ -n "$TARGET" ] || usage
case "$MODE" in
  "" | --status) ;;
  --init) [ -n "$SITE_NAME" ] || usage ;;
  *) usage ;;
esac

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=30)
# Remote commands are built locally on purpose (paths and timeouts expand here).
# shellcheck disable=SC2029
remote() { ssh "${SSH_OPTS[@]}" "$TARGET" "$@"; }

status() {
  remote "set -e
    $COMPOSE ps
    echo
    echo '--- middleware status'
    $COMPOSE exec -T middleware python /app/main.py status || true
    echo
    echo '--- mcmon /healthz'
    port=\$(grep -E '^MCMON_PORT=' $REMOTE_SITE/.env | cut -d= -f2)
    bind=\$(grep -E '^MCMON_BIND=' $REMOTE_SITE/.env | cut -d= -f2)
    curl -fsS \"http://\${bind:-127.0.0.1}:\${port:-9464}/healthz\" || true
    echo"
}

if [ "$MODE" = "--status" ]; then
  status
  exit 0
fi

command -v rsync > /dev/null || { echo "rsync is not installed locally" >&2; exit 1; }

echo "==> Checking $TARGET"
remote "command -v docker > /dev/null && docker compose version > /dev/null && test -d $REMOTE_APP" \
  || { echo "host not ready: needs docker, the compose plugin and $REMOTE_APP (cloud-init sets these up)" >&2; exit 1; }

echo "==> Copying code to $TARGET:$REMOTE_APP"
rsync -az --delete --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r -e "ssh ${SSH_OPTS[*]}" \
  --include '.env.example' --exclude '.git/' --exclude '.github/' --exclude '.env' --exclude '.env.*' \
  --exclude 'secrets/' --exclude 'data/' --exclude '.venv/' \
  --exclude '__pycache__/' --exclude '*.pyc' --exclude '*.db*' \
  --exclude 'deploy/hetzner/.terraform/' --exclude '*.tfstate*' --exclude '*.tfvars' \
  "$ROOT/" "$TARGET:$REMOTE_APP/"

echo "==> Building images on the host"
remote "cd $REMOTE_APP && docker build -q -t mittelconnect:1.0.0 . && docker build -q -t mittelconnect-monitor:1.0.0 monitor"

if [ "$MODE" = "--init" ]; then
  echo "==> Initialising site $SITE_NAME (interactive)"
  ssh -t -o ConnectTimeout=15 "$TARGET" "sudo bash $REMOTE_APP/scripts/site_init.sh $SITE_NAME"
  echo
  echo "Adapt $REMOTE_SITE/config.yaml on the host, then run: $0 $TARGET"
  exit 0
fi

remote "test -f $REMOTE_SITE/.env" \
  || { echo "site not initialised; run: $0 $TARGET --init <site-name>" >&2; exit 1; }

echo "==> Validating site configuration"
remote "$COMPOSE run --rm --no-deps -T middleware check-config > /dev/null" \
  || { echo "check-config failed; the running version was left untouched" >&2; exit 1; }

echo "==> Starting the stack"
remote "$COMPOSE up -d --remove-orphans"

echo "==> Waiting up to ${HEALTH_TIMEOUT}s for the middleware to report healthy"
remote "deadline=\$(( \$(date +%s) + $HEALTH_TIMEOUT ))
  while true; do
    id=\$($COMPOSE ps -q middleware)
    state=\$(docker inspect -f '{{.State.Health.Status}}' \"\$id\" 2>/dev/null || echo unknown)
    [ \"\$state\" = healthy ] && { echo 'middleware healthy'; exit 0; }
    [ \$(date +%s) -ge \$deadline ] && { echo \"middleware not healthy (state: \$state)\" >&2; $COMPOSE logs --tail 50 middleware >&2; exit 1; }
    sleep 10
  done"

status
echo "==> Deployed."
