# WebVault

Self-hosted web archiving with Browsertrix Crawler, Garage S3 storage, and ReplayWeb.page replay.

## Quick Start

```bash
./setup.sh           # First time only
docker compose up -d   # Start all services
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
8. Stops Garage

On subsequent runs, `.env` already exists — `setup.sh` exits immediately.


## Project Layout

```
.env                  Garage credentials (auto-generated, gitignored)
garage/               Garage config
  garage.toml         actual config with secrets
orchestrator/         FastAPI app
crawler/              Browsertrix container
crawls/               crawl outputs
replayweb/            ReplayWeb.page assets
setup.sh              first-run bootstrap
docker-compose.yml
```

## Ports

- 8008 - Web UI
- 3900 - S3 API
- 3901 - Garage RPC
- 3902 - S3 Web
- 3903 - Admin API

## How It Works

1. Enter URLs in the UI and start a crawl
2. Orchestrator writes a YAML config into `./crawls`
3. Browsertrix runs the crawl inside the `webvault-crawler` container
4. Finished `.wacz` files are uploaded to Garage
5. Archives appear in the UI and can be replayed in the browser

## Development

- `orchestrator/app.py` — main FastAPI app
- Replay assets served from `replayweb/`
- Archives read via boto3 S3 client

## Limitations

- Crawl job state is in-memory; restarts during a crawl reset the job list
- Replay requires localhost or HTTPS