# hwid-blocker

Hard HWID device-blocking for Remnawave. A small, dependency-free Go reverse
proxy that sits between nginx/Caddy and the Remnawave subscription page. It
intercepts subscription `GET` requests, reads the client hardware id
(`x-hwid` header) together with the subscription short-UUID, and — if that
`(user_id, hwid, short_uuid)` tuple is on the block list — serves a
"device blocked" page instead of the subscription. Every other request is
proxied straight through to the subscription page.

The **bot** is the single source of truth. It owns the `device_blocks` table
and pushes the full state to this proxy; the proxy keeps a small on-disk
snapshot (so a restart does not drop blocks) and re-pulls the authoritative
state from the bot on startup.

> The whole feature is gated behind the bot's `DEVICE_BLOCK_ENABLED` env flag
> (default `false`). With it off you can run the bot + cabinet **without**
> this middleware at all.

## Architecture

```
Data plane:
  nginx (sub.domain) -> hwid-blocker :3020 -> subscription-page :3010

Control plane:
  bot :8080  --PUT /v1/state-->  hwid-blocker
  hwid-blocker --GET /device-blocks--> bot :8080   (startup bootstrap)
```

## Environment variables

| Variable          | Default                 | Required | Description |
|-------------------|-------------------------|----------|-------------|
| `LISTEN_ADDR`     | `127.0.0.1:3020`        | no       | Primary listener (nginx path). Fatal if it cannot bind. |
| `BOT_LISTEN_ADDR` | *(empty)*               | no       | Optional 2nd listener for the bot bridge network. |
| `UPSTREAM_URL`    | `http://127.0.0.1:3010` | yes      | The Remnawave subscription page. |
| `STATE_FILE`      | `/data/state.json`      | no       | Where the state snapshot is persisted. |
| `ADMIN_TOKEN`     | *(empty)*               | yes*     | Token required for `PUT /v1/state`. Must match the bot's `MIDDLEWARE_ADMIN_TOKEN`. |
| `BOT_API_URL`     | `http://127.0.0.1:8080` | no       | Bot webapi (used for the startup bootstrap pull). |
| `BOT_API_TOKEN`   | *(empty)*               | no*      | Token accepted by the bot webapi for the bootstrap pull. |
| `FAIL_OPEN`       | `true`                  | no       | If the state file is unreadable: start empty (`true`) vs refuse to start (`false`). |

\* Required for the bot to push/seed state. Without them the proxy still runs
(and proxies traffic) but stays on its last snapshot.

## Install — Docker

```bash
cd middleware/docker
cp .env.example .env        # then edit: set the two tokens
docker compose up -d --build
```

- Builds from the `middleware/` root (Go 1.24, static binary, non-root user).
- Uses `network_mode: host` so it can reach the subscription page
  (`127.0.0.1:3010`) and the bot webapi (`127.0.0.1:8080`) on the host.
- State is stored in `middleware/data/state.json`.

```bash
docker compose logs -f     # follow logs
docker compose restart     # restart
docker compose down        # stop
```

## Install — Dockerless

Requires the Go toolchain (>= 1.24) on the host.

**Run in the foreground (dev / quick test):**

```bash
cd middleware/dockerless
cp .env.example .env        # then edit
./run.sh
```

`run.sh` builds a static binary into `middleware/bin/` if it is missing, then
execs it in the foreground. Set `REBUILD=1` to force a rebuild.

**Install as a systemd service (production):**

```bash
cd middleware/dockerless
cp .env.example .env        # then edit (real tokens)
sudo ./install-service.sh
sudo systemctl start hwid-blocker
```

`install-service.sh` builds a static binary into `/opt/hwid-blocker`, copies
your env to `/etc/hwid-blocker/hwid-blocker.env` (0600), creates a non-root
`hwid` user, and enables `hwid-blocker.service`.

## Bot integration

On the bot side, set (in the bot's `.env`):

```
DEVICE_BLOCK_ENABLED=true
MIDDLEWARE_URL=http://127.0.0.1:3020 (have https support!)
MIDDLEWARE_ADMIN_TOKEN=<same as this proxy's ADMIN_TOKEN>
MIDDLEWARE_PUSH_INTERVAL_MIN=30
```

- `MIDDLEWARE_ADMIN_TOKEN` MUST equal this proxy's `ADMIN_TOKEN`.
- `MIDDLEWARE_URL` must point at a listener this proxy is bound to. If the bot
  runs in a container on a bridge network, use the bridge gateway
  (e.g. `http://172.22.0.1:3020`) and set `BOT_LISTEN_ADDR` here to match.

Pushes are best-effort: a missing middleware never breaks the bot's ban/unban
flow, and a periodic re-sync repairs any drift.

## nginx

Point the subscription server block at the proxy and set the forwarded headers
the subscription page requires:

```nginx
location / {
    proxy_pass http://127.0.0.1:3020;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

## HTTP API

| Method | Path          | Auth            | Purpose |
|--------|---------------|-----------------|---------|
| `GET`  | `/v1/health`  | —               | `{ok, version, count, uptime_s}` |
| `PUT`  | `/v1/state`   | `X-Admin-Token` | Push full state `{version, devices[], updated_at}` |
| `GET`  | `/{shortUUID}` или `/path/{shortUUID}`| —               | Subscription fetch; blocked devices get a block page, others are proxied |

A device is blocked when its `x-hwid` header + the path short-UUID matches a
`(user_id, hwid, short_uuid)` entry in the current state.

## Build the binary manually

```bash
cd middleware
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o bin/hwid-blocker .
```
