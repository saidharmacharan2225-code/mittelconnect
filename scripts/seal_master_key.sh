#!/usr/bin/env bash
# =============================================================================
# Seal a site's master key with systemd-creds so its plaintext never sits on
# the data volume next to the ciphertexts it protects.
#
#   sudo ./scripts/seal_master_key.sh --offline-backup-done [site_dir]
#
# What it does:
#   1. encrypts secrets/master.key into secrets/master.key.cred, bound to this
#      host's TPM2 chip when it has one, otherwise to the host key in
#      /var/lib/systemd/credential.secret on the root disk
#   2. installs mittelconnect-master-key.service, which decrypts the key into
#      tmpfs (/run/mittelconnect/master.key) on every boot, before Docker
#   3. points the site .env at that tmpfs copy (MC_HOST_MASTER_KEY_FILE)
#   4. verifies the round trip, then shreds the plaintext key
#
# The sealed file cannot be decrypted on any other machine. The OFFLINE backup
# of the plaintext key is therefore the only way to recover the site after a
# host loss, which is why the script refuses to run without
# --offline-backup-done.
# Undo: decrypt to secrets/master.key, remove MC_HOST_MASTER_KEY_FILE from .env,
# disable the unit, and run scripts/deploy.sh again.
# =============================================================================
set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

[ "${1:-}" = "--offline-backup-done" ] || die "back up secrets/master.key offline first, then pass --offline-backup-done"
SITE_DIR="${2:-/srv/mittelconnect/site}"
APP_UID=10001
KEY="$SITE_DIR/secrets/master.key"
CRED="$SITE_DIR/secrets/master.key.cred"
RUN_KEY=/run/mittelconnect/master.key
UNIT=mittelconnect-master-key.service
UNIT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/deploy/systemd/$UNIT"

[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
command -v systemd-creds > /dev/null || die "systemd-creds not found (systemd 250+ required)"
[ -f "$UNIT_SRC" ] || die "$UNIT_SRC not found"
[ -f "$SITE_DIR/.env" ] || die "$SITE_DIR/.env not found; run scripts/site_init.sh first"
[ -f "$CRED" ] && die "$CRED already exists; this site is already sealed"
[ -s "$KEY" ] || die "$KEY not found or empty"

echo "==> Sealing $KEY"
systemd-creds setup > /dev/null
umask 077
systemd-creds encrypt --name=mittelconnect-master-key --with-key=auto "$KEY" "$CRED"
chown root:root "$CRED"
chmod 0400 "$CRED"

echo "==> Verifying the sealed copy"
CHECK="$(mktemp)"
trap 'rm -f "$CHECK"' EXIT
systemd-creds decrypt --name=mittelconnect-master-key "$CRED" "$CHECK"
cmp -s "$KEY" "$CHECK" || die "decrypted key differs from the original; nothing was changed"

echo "==> Installing $UNIT"
sed "s|@SITE_DIR@|$SITE_DIR|g" "$UNIT_SRC" > "/etc/systemd/system/$UNIT"
chmod 0644 "/etc/systemd/system/$UNIT"
systemctl daemon-reload
systemctl enable --now "$UNIT"
cmp -s "$KEY" "$RUN_KEY" || die "$RUN_KEY does not match the original key; the plaintext key was kept"
[ "$(stat -c '%u %a' "$RUN_KEY")" = "$APP_UID 400" ] || die "$RUN_KEY must be owned by $APP_UID with mode 0400"

echo "==> Pointing the site .env at $RUN_KEY"
ENV_TMP="$(mktemp "$SITE_DIR/.env.XXXXXX")"
grep -v '^MC_HOST_MASTER_KEY_FILE=' "$SITE_DIR/.env" > "$ENV_TMP" || true
printf 'MC_HOST_MASTER_KEY_FILE=%s\n' "$RUN_KEY" >> "$ENV_TMP"
chown --reference="$SITE_DIR/.env" "$ENV_TMP"
chmod --reference="$SITE_DIR/.env" "$ENV_TMP"
mv "$ENV_TMP" "$SITE_DIR/.env"

echo "==> Removing the plaintext key from the data volume"
shred -u "$KEY"

cat <<MSG
==> Done. Recreate the middleware so it mounts the tmpfs key:
      scripts/deploy.sh <target>     (or: docker compose ... up -d)
    After the next reboot, check: systemctl status $UNIT
MSG
