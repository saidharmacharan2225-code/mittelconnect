#!/usr/bin/env bash
# =============================================================================
# One-time sandbox bootstrap:
#   1. builds the middleware image
#   2. generates the Fernet master key into secrets/master.key
#   3. generates random passwords for SQL Server, mc_reader and the mock SAP
#   4. encrypts the middleware's secrets into ENC[...] values
#   5. writes .env (mode 0600) for docker compose
# Re-running is refused while secrets/master.key exists, so existing
# encrypted values are never orphaned. Use --force to start from scratch
# (this also requires `docker compose down -v` to reset the databases).
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

IMAGE="mittelconnect:1.0.0"
FORCE="${1:-}"

command -v docker > /dev/null || { echo "docker is not installed" >&2; exit 1; }
command -v openssl > /dev/null || { echo "openssl is not installed" >&2; exit 1; }

if [ -f secrets/master.key ] && [ "$FORCE" != "--force" ]; then
  echo "secrets/master.key already exists; sandbox is bootstrapped."
  echo "Use '$0 --force' (and 'docker compose down -v') to regenerate everything."
  exit 0
fi

rand_password() {
  # 24 alphanumerics plus a fixed suffix satisfying SQL Server's complexity policy.
  local base
  base="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 24)"
  printf '%sAa1#' "$base"
}

echo "==> Building $IMAGE"
docker build -t "$IMAGE" .

echo "==> Generating master key"
umask 077
mkdir -p secrets
chmod 0700 secrets
rm -f secrets/master.key
MASTER_KEY="$(docker run --rm --network none "$IMAGE" generate-key)"
printf '%s\n' "$MASTER_KEY" > secrets/master.key
# Readable by the container user (uid 10001); the 0700 directory keeps other host users out.
chmod 0444 secrets/master.key

encrypt() {
  printf '%s' "$1" | docker run --rm -i --network none \
    -e MITTELCONNECT_MASTER_KEY="$MASTER_KEY" "$IMAGE" encrypt-secret --stdin
}

echo "==> Generating and encrypting secrets"
SA_PASSWORD="$(rand_password)"
READER_PASSWORD="$(rand_password)"
SAP_SECRET="$(openssl rand -hex 24)"
PSEUDO_KEY="$(openssl rand -hex 32)"

cat > .env <<EOF
MSSQL_SA_PASSWORD=${SA_PASSWORD}
MC_READER_PASSWORD=${READER_PASSWORD}
MOCK_SAP_CLIENT_SECRET=${SAP_SECRET}
MC_MSSQL_PASSWORD_ENC=$(encrypt "$READER_PASSWORD")
MC_SAP_CLIENT_SECRET_ENC=$(encrypt "$SAP_SECRET")
MC_PSEUDONYMIZATION_KEY_ENC=$(encrypt "$PSEUDO_KEY")
MC_LOG_LEVEL=INFO
MC_LOG_FORMAT=json
SIMULATOR_INTERVAL=15
EOF
chmod 0600 .env

unset MASTER_KEY SA_PASSWORD READER_PASSWORD SAP_SECRET PSEUDO_KEY

echo "==> Done. Start the sandbox with: docker compose up -d --build"
