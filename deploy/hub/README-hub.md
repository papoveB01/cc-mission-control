# cc-mission-control history hub

The hub stores the history of every Claude Code session your machines relay to it
(SQLite, append-only) and serves a history UI. It runs in Docker next to a Tailscale
container, so it is reachable **only over your tailnet**. See SPEC.md section 13.

```
Mac (local server, CCMC_UPSTREAM_URL) --WireGuard--> tailscale container <--shared netns--> hub :4318
```

## Prerequisites

- A server with Docker and Compose v2+ and SSH key login (password auth disabled).
- `/dev/net/tun` on the server. If `test -c /dev/net/tun` fails, an admin with sudo must run
  `sudo modprobe tun` and persist it with `echo tun | sudo tee /etc/modules-load.d/tun.conf`.
  `deploy.sh` checks this first.
- A Tailscale account, and Tailscale installed and logged in on each client Mac.
- A Tailscale auth key from <https://login.tailscale.com/admin/settings/keys>:
  reusable **off**, ephemeral **off**. A tag is optional (if you use one, also put
  `TS_EXTRA_ARGS=--advertise-tags=tag:ccmc-hub` in `.env`).
- `rsync` and `ssh` on the machine you deploy from.
- If a machine named `ccmc-hub` already exists in the Tailscale admin console (an old install),
  **delete it first**. Otherwise the new node registers as `ccmc-hub-1` and the
  `ccmc-hub.<tailnet>.ts.net` name (and the Host check) will not match.

## Server setup

1. Sync the files once (this will stop at the missing `.env`):

   ```bash
   deploy/hub/deploy.sh admin@myserver
   ```

2. On the server, create the env file and lock it down:

   ```bash
   ssh admin@myserver
   cd ~/ccmc-hub/deploy/hub
   cp .env.example .env && chmod 600 .env
   python3 -c 'import secrets;print(secrets.token_urlsafe(32))'   # paste as CCMC_HUB_TOKEN
   $EDITOR .env                                                   # also set TS_AUTHKEY
   ```

3. Deploy again. It builds, starts, waits up to 90 s for the hub to be healthy and
   prints `docker compose ps` and `tailscale status`:

   ```bash
   deploy/hub/deploy.sh admin@myserver          # optional second arg: remote dir (default ~/ccmc-hub)
   deploy/hub/deploy.sh --check admin@myserver  # read-only status
   ```

Only an allowlist is sent to the server: `pyproject.toml`, `README.md`, `LICENSE`,
`NOTICE`, `cc_mission_control/` and `deploy/hub/` (never `.env`, `keys/`, `.git`).
The server's own `.env` is never overwritten. Local edits you make to other files in
`deploy/hub/` on the server are overwritten by every deploy; change them in your checkout.
The script also refuses a remote dir that is empty, `.`, `~`, `/` or contains spaces. Find your tailnet name in the Tailscale
admin console (`<tailnet>.ts.net`); the hub is `ccmc-hub.<tailnet>.ts.net`.

The hub accepts the Host names `ccmc-hub` and `ccmc-hub.<tailnet>.ts.net` by default.
Leave `CCMC_HUB_HOSTS` empty unless you need more; setting it replaces that default.

## Client setup (each Mac)

Add to `~/.claude/settings.json`:

```json
{
  "env": {
    "CCMC_UPSTREAM_URL": "http://ccmc-hub.<tailnet>.ts.net:4318/ingest",
    "CCMC_UPSTREAM_TOKEN": "<the CCMC_HUB_TOKEN value>"
  }
}
```

Restart Claude Code (the local server reads these at start). Confirm:

```bash
curl -s http://127.0.0.1:4317/health | python3 -m json.tool   # relay.last_ok should be a recent time
```

Records that cannot be sent are spooled on disk and retried, so a hub outage loses nothing.

## Using it

Open `http://ccmc-hub.<tailnet>.ts.net:4318/history` from any device on the tailnet and
sign in with the token. (The history UI ships with milestone M15; until then the JSON
API under `/api/history/*` works with `Authorization: Bearer <token>`.)

## Upgrade

```bash
git pull   # or update your checkout
deploy/hub/deploy.sh admin@myserver
```

This rebuilds the image from the new code. The `hub-data` and `ts-state` volumes are kept.
Bump the version in `pyproject.toml` / `__init__.py` and the `image:` tag and the
Dockerfile `VERSION` arg together for releases.

## Logs and status

If the tailscale container crashes and restarts on its own, the hub keeps a stale network
namespace; run `docker compose -f deploy/hub/docker-compose.yml restart hub` on the server.
(`deploy.sh` and `docker compose restart tailscale` handle this for you.)

```bash
ssh admin@myserver 'cd ~/ccmc-hub && docker compose -f deploy/hub/docker-compose.yml logs --tail 100 hub'
ssh admin@myserver 'cd ~/ccmc-hub && docker compose -f deploy/hub/docker-compose.yml logs --tail 50 tailscale'
```

Logs are rotated (10 MB x 3 per container).

**Troubleshooting: `iptables ... (legacy): can't initialize iptables table 'filter'`** in the
tailscale logs (with `firewallmode="ipt-default"`) means the host kernel lacks the legacy
iptables modules (typical on AlmaLinux/RHEL 9+). The compose file sets
`TS_DEBUG_FIREWALL_MODE: nftables` on the tailscale service to avoid this; if you removed it,
put it back and run `docker compose -f deploy/hub/docker-compose.yml up -d tailscale`.

## Backups and restore

The hub writes a daily `VACUUM INTO` copy to `/data/backups/history-YYYYMMDD.db` inside
the `hub-data` volume and keeps the last `CCMC_HUB_BACKUPS` (default 7). Run these on
the server in `~/ccmc-hub`, with `F="-f deploy/hub/docker-compose.yml"`.

Copy a backup out (then off the server with scp/rsync; the volume alone is not a
disaster-recovery copy):

```bash
docker compose $F exec hub ls -l /data/backups
docker compose $F cp hub:/data/backups/history-YYYYMMDD.db ./history-YYYYMMDD.db
```

Restore a backup that is already in the volume (the tailscale container must be running,
because the hub shares its network namespace):

```bash
docker compose $F stop hub
docker compose $F run --rm --no-deps --entrypoint sh hub -c \
  'cp /data/backups/history-YYYYMMDD.db /data/history.db && rm -f /data/history.db-wal /data/history.db-shm'
docker compose $F start hub
```

Restoring a file from elsewhere: `docker compose $F cp ./history-YYYYMMDD.db hub:/data/backups/`
(works on a running or stopped hub), then run the restore above. Run the restore through
`run` as shown, not `cp` straight onto `history.db`, so the file ends up owned by the
hub user (uid 10001).

## Retention

`CCMC_HUB_RETENTION_DAYS` in `.env` (default `0` = keep forever). A daily job deletes
sessions whose last event is older than the limit. Edit `.env`, then re-run `deploy.sh`.

## Uninstall

```bash
cd ~/ccmc-hub && docker compose -f deploy/hub/docker-compose.yml down        # keep data
cd ~/ccmc-hub && docker compose -f deploy/hub/docker-compose.yml down -v     # also deletes history and the tailscale identity
```

Then remove the `ccmc-hub` machine in the Tailscale admin console, delete `~/ccmc-hub`,
and remove `CCMC_UPSTREAM_*` from each Mac's settings.

## Security notes

- **No published port.** The compose file has no `ports:` and no host networking, so
  nothing listens on the server's own interfaces and the internet cannot reach the hub. The
  tailnet reaches it through Tailscale. Note that the containers sit on a Docker bridge
  network, so root or docker-group users on the server can still reach the hub on its
  bridge address. The token is what protects that path.
- **The token is a second layer.** Every endpoint except `/health` needs it. Treat it as
  a password: `.env` is `chmod 600` and gitignored. The Tailscale auth key is only needed on
  first start; you can blank `TS_AUTHKEY` afterwards (the node identity is in `ts-state`).
- **`docker compose config` prints secrets.** Its output contains the resolved `TS_AUTHKEY` and
  `CCMC_HUB_TOKEN`; do not paste it into tickets or chats.
- **Rotate the token:** generate a new one, edit `CCMC_HUB_TOKEN` in the server `.env`, run
  `deploy.sh`, then update `CCMC_UPSTREAM_TOKEN` on every Mac. Old tokens and old
  browser sessions stop working immediately.
- **Hardening:** the hub runs as uid 10001 with a read-only root filesystem, all capabilities
  dropped, `no-new-privileges`, and a 512 MB memory limit.
- **Tailnet ACL suggestion:** tag the node (`tag:ccmc-hub`) and allow only your devices to reach
  it on port 4318, for example:

  ```json
  {"acls": [{"action": "accept", "src": ["autogroup:member"], "dst": ["tag:ccmc-hub:4318"]}]}
  ```

## Local testing (not for servers)

`docker-compose.local.yml` runs the hub alone on `127.0.0.1:48318` with a token from your
environment: `CCMC_HUB_TOKEN=<32+ chars> docker compose -f deploy/hub/docker-compose.local.yml up -d --build`.
