#!/bin/sh
# Deploy the cc-mission-control hub to a server over SSH + rsync.
#
#   deploy/hub/deploy.sh <ssh-host> [remote-dir]           sync, build, start, wait healthy
#   deploy/hub/deploy.sh --check <ssh-host> [remote-dir]   read-only status only
#
# remote-dir defaults to ~/ccmc-hub (relative to the remote home).
# Only an allowlist of files is sent. The local .env and keys/ are never read or copied.
set -eu

CHECK=0
if [ "${1:-}" = "--check" ]; then
  CHECK=1
  shift
fi
if [ $# -lt 1 ] || [ $# -gt 2 ]; then
  echo "usage: $0 [--check] <ssh-host> [remote-dir]" >&2
  exit 64
fi
HOST=$1
DIR=${2-ccmc-hub}
# Keep a leading "~/" relative to the remote home (the remote shell would not expand it in quotes).
tilde='~'
case "$DIR" in
  "$tilde"/*) DIR=${DIR#"$tilde"/} ;;
esac
case "$DIR" in
  "" | . | ./ | / | "$tilde" | "$tilde"/ | .. | ../*)
    echo "refusing remote dir '$DIR': pick a dedicated subdirectory (e.g. ccmc-hub)" >&2
    exit 64
    ;;
  *[[:space:]]*)
    echo "refusing remote dir '$DIR': spaces are not supported" >&2
    exit 64
    ;;
esac

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
COMPOSE="docker compose -f deploy/hub/docker-compose.yml"

status() {
  # shellcheck disable=SC2029  # DIR/COMPOSE are meant to expand locally
  ssh "$HOST" "cd '$DIR' && $COMPOSE ps && echo && { $COMPOSE exec -T tailscale tailscale status || echo '(tailscale status failed or not running)'; }"
}

if [ "$CHECK" -eq 1 ]; then
  status
  exit 0
fi

echo "==> checking /dev/net/tun on $HOST"
if ! ssh "$HOST" "test -c /dev/net/tun"; then
  cat >&2 <<EOF
Stopped: /dev/net/tun is missing on $HOST.
An admin with sudo must run:  sudo modprobe tun
and persist it:               echo tun | sudo tee /etc/modules-load.d/tun.conf
EOF
  exit 1
fi

echo "==> syncing allowlisted files to $HOST:$DIR"
# shellcheck disable=SC2029
ssh "$HOST" "mkdir -p '$DIR/deploy/hub'"
cd "$ROOT"
# Never send secrets or bytecode, anywhere in the tree (.env.example is fine).
send() {
  rsync -a --human-readable \
    --exclude '__pycache__/' --exclude '*.pyc' --exclude '*.pem' --exclude '*.key' \
    --include '.env.example' --exclude '.env*' "$@"
}
# Top-level files (no --delete: nothing else lives at these paths).
send pyproject.toml README.md LICENSE NOTICE "$HOST:$DIR/"
# Package: --delete scoped to this directory.
send --delete cc_mission_control/ "$HOST:$DIR/cc_mission_control/"
# Deploy folder: the server's own .env is kept (excluded files are not deleted).
# Local edits made on the server inside deploy/hub/ are overwritten.
send --delete deploy/hub/ "$HOST:$DIR/deploy/hub/"

echo "==> checking remote .env"
# shellcheck disable=SC2029
if ! ssh "$HOST" "test -f '$DIR/deploy/hub/.env'"; then
  cat >&2 <<EOF
Stopped: $DIR/deploy/hub/.env does not exist on $HOST.
Create it from the example, fill in TS_AUTHKEY and CCMC_HUB_TOKEN, then re-run:
  ssh $HOST
  cd $DIR/deploy/hub && cp .env.example .env && chmod 600 .env && \$EDITOR .env
EOF
  exit 1
fi

echo "==> docker compose up -d --build"
# The hub waits for a healthy tailscale, so a bad TS_AUTHKEY makes `up` fail; carry on to the
# tailscale wait below, which explains it.
# shellcheck disable=SC2029
ssh "$HOST" "cd '$DIR' && chmod 600 deploy/hub/.env && $COMPOSE up -d --build" ||
  echo "(docker compose up reported a failure; checking tailscale)" >&2

echo "==> waiting for tailscale to be Running (60 s)"
# shellcheck disable=SC2029
if ! ssh "$HOST" "cd '$DIR' && i=0 && \
  while [ \$i -lt 30 ]; do \
    $COMPOSE exec -T tailscale tailscale status --json 2>/dev/null | grep -q '\"BackendState\": *\"Running\"' && echo Running && exit 0; \
    i=\$((i+1)); sleep 2; \
  done; exit 1"; then
  {
    echo "tailscale did not reach BackendState=Running within 60 s. Last logs:"
    # shellcheck disable=SC2029
    ssh "$HOST" "cd '$DIR' && $COMPOSE logs --tail 20 tailscale" || true
    echo "Hint: TS_AUTHKEY may be missing, expired or already used (a non-reusable key works once)."
    echo "Create a new key, put it in $DIR/deploy/hub/.env and re-run. Also delete any old 'ccmc-hub' machine in the Tailscale admin console."
  } >&2
  exit 1
fi

echo "==> waiting for the hub to be healthy (90 s)"
# shellcheck disable=SC2029
ssh "$HOST" "cd '$DIR' && i=0 && s=none && \
  while [ \$i -lt 45 ]; do \
    cid=\$($COMPOSE ps -q hub 2>/dev/null); \
    if [ -n \"\$cid\" ]; then s=\$(docker inspect -f '{{.State.Health.Status}}' \"\$cid\" 2>/dev/null || echo none); fi; \
    [ \"\$s\" = healthy ] && echo healthy && exit 0; \
    i=\$((i+1)); sleep 2; \
  done; echo \"hub not healthy after 90 s (last: \$s)\" >&2; $COMPOSE logs --tail 30 hub >&2; exit 1"

status
