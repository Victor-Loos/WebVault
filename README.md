# WebVault

Self-hosted web archiving with Browsertrix Crawler, Garage S3 storage, and ReplayWeb.page replay.

## Quick Start

```bash
./setup.sh             # First time only
docker compose up -d   # Start all services
./update.sh             # Pull latest images/dependencies and restart
```

Open http://localhost:8008

## What setup.sh does

1. Copies `garage/garage.toml.example` → `garage/garage.toml`
2. Generates a random `rpc_secret` and injects it
3. Starts Garage, waits for it to be healthy
4. Sets up the cluster layout
5. Creates the `archives` bucket
6. Creates S3 access key and permissions
7. Writes `RPC_SECRET`, `GARAGE_ACCESS_KEY`, `GARAGE_SECRET_KEY` to `.env`
8. Initializes a configurable Garage layout (100 GB by default)
9. Stops Garage

On subsequent runs, `.env` already exists — `setup.sh` exits immediately.


## Project Layout

```
.env                  Garage credentials (auto-generated, gitignored)
garage/               Garage config
  garage.toml         actual config with secrets
orchestrator/         FastAPI app and isolated crawl-worker package
crawler/              Browsertrix container
crawls/               crawl outputs
replayweb/            ReplayWeb.page assets
setup.sh              first-run bootstrap
docker-compose.yml
```

## Ports

- 8008 - Web UI (loopback-only by default)
- 3900 - S3 API
- 3901 - Garage RPC
- 3902 - S3 Web
- 3903 - Admin API
- 9222 - Temporary interactive crawl DevTools (loopback-only)
- 9223 - Temporary browser login-profile UI (loopback-only)

## How It Works

1. Enter URLs in the UI and start a capture
2. FastAPI validates the request, writes its YAML configuration, and queues it in SQLite
3. The isolated `webvault-worker` atomically leases the next queued job
4. The worker controls Browsertrix and renews its lease while the crawl runs
5. Finished `.wacz` files are uploaded to Garage
6. Live progress, cancellation, page counts, current URL, and screencast appear in Activity
7. Archives appear in the searchable Library and can be replayed in the browser

## Current features

- Live progress, view-only screencast, and optional local interactive DevTools control
- Interactive browser login profiles for authenticated sites, SSO, and MFA
- URL-deduplicated library with collection filters, SQLite FTS5 page-text search, and complete capture-version timelines
- Capture information pages with archive metadata and reusable/editable crawl settings
- Previous-capture detection while entering a seed URL
- Server statistics for application resources, worker heartbeat, jobs, and archive usage
- Replay and WACZ download, rerun, cancellation, rename, move, and deletion
- Browsertrix include/exclude URL rules and recurring daily, weekly, or 30-day captures
- Per-schedule retention policies that remove older WACZ versions after a successful capture

## Updates and authentication

Browsertrix defaults to the tested `1.14.0` release so saved login profiles remain
compatible. `./update.sh` rebuilds the Python services with pinned dependencies.
Override `BROWSERTRIX_IMAGE` only after validating profiles against the new release.

The UI and all Garage ports bind only to localhost by default. `setup.sh` generates
an `admin` login and random password in `.env`; authentication is required by default.
A partial credential configuration is rejected, and unauthenticated non-loopback
binding is refused. For an intentionally unauthenticated loopback-only deployment,
set `WEBVAULT_ALLOW_UNAUTHENTICATED=true`. Before using a reverse proxy, set
`WEBVAULT_ALLOWED_HOSTS` to the explicit public hostname and only then set
`WEBVAULT_BIND_ADDRESS=0.0.0.0`. Each login receives a random,
server-expiring session and per-session CSRF token. The HTTP-only cookie lasts
up to 30 days, and **Settings → Log out** revokes it immediately.

## Storage capacity

New installations use a 100 GB Garage layout by default. Set `GARAGE_CAPACITY`
in `.env` (for example `500G` or `2TiB`) and run `./garage/resize.sh` to expand an
existing layout. Running `setup.sh` or `update.sh` also applies the configured
capacity. The statistics page reports configured capacity and estimated remaining
archive space; Garage metadata and replication overhead are not included.

## Development

The FastAPI composition and routes remain in `orchestrator/app.py`. Supporting code is
split into focused modules under `orchestrator/webvault/`:

- `config.py` — validated environment settings
- `models.py` — API request and crawl models
- `jobs.py` — transactional SQLite job persistence and legacy JSON migration
- `state.py` — the web process’s shared SQLite-backed job store
- `services/crawls.py` — validated queue submission and crawl configuration
- `routers/auth.py` — login, logout, browser sessions, Basic API auth, and CSRF
- `routers/pages.py` — home and collection pages
- `routers/jobs.py` — queue, rerun, cancellation, and activity APIs
- `routers/history.py` — URL-deduplicated library and capture timelines
- `routers/profiles.py` — interactive Browsertrix login-profile provisioning
- `routers/stats.py` — resource and archive statistics
- `sessions.py` — random, expiring, revocable browser sessions
- `storage.py` — Garage/S3 and WACZ operations
- `worker.py` — leased crawl execution, progress, recovery, and cancellation
- `utils.py` — URL, naming and formatting helpers
- `views.py` — Jinja template rendering and response helpers
- `templates/` — accessible server-rendered pages
- `static/` — the archival-studio design system and interaction controllers

The interface uses a responsive “archival studio” design, progressively disclosed crawl
options, accessible dialogs, resilient API feedback, and separate page controllers.
Replay assets are served from `replayweb/`. Extracted Browsertrix page text is indexed
in SQLite FTS5 after upload; local archives from older completed jobs are indexed in the
background at startup. Imported Garage-only archives require a future explicit reindex
workflow. Browsertrix JSONL output is streamed
into SQLite-backed job progress. Running jobs expose an authenticated **Watch live**
screencast, and previous jobs can be rerun or safely replace a collection after the new capture uploads.

Install the development dependencies and run all checks:

```bash
python3 -m pip install -r requirements-dev.txt
./check.sh
```

The checks include Ruff linting/formatting, Pyright, pytest, shell syntax and Docker
Compose validation.

## Limitations

- Browsertrix cannot resume midway through a page crawl. If the worker lease expires, the job is safely returned to the queue and restarted. During planned updates, the worker terminates the active crawler process and immediately requeues the job; Compose allows a two-minute shutdown grace period.
- Only the isolated, non-network-facing worker receives the Docker socket; compromising that worker still implies host-level Docker access.
- The shared Browsertrix container intentionally runs one crawl job at a time. Its own `workers` option still parallelizes pages within that crawl.
- Seed hosts are resolved and private, loopback, link-local, and reserved addresses are blocked before queueing and again before execution. A restricted egress proxy is still recommended for hostile redirects and subresources.
- Sites requiring login can use a versioned browser profile from **Settings → Browser login profiles**. Profile creation opens Browsertrix's interactive browser on loopback-only port `9223`; passwords are entered directly there and are never submitted to WebVault. Treat profiles and authenticated WACZ files as secrets.
- Selecting **Interactive control** for a crawl enables Chromium DevTools on loopback-only port `9222`. This allows click/type control, but Browsertrix may navigate tabs while the crawl advances. **Watch live** remains intentionally view-only.
- Browsertrix is pinned to `1.14.0` by default so profile behavior does not change unexpectedly.
- Replay requires localhost or HTTPS. Replay-origin isolation remains a priority before importing archives from untrusted parties.