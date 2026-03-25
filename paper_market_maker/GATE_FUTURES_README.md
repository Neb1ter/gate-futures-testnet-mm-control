# Gate Futures Testnet Market Maker

This folder now contains a cloud-ready Gate Futures Testnet market-making bot with:

- conservative `BTC_USDT` default parameters
- persistent local `state.json`
- append-only `events.jsonl`
- append-only `trades.jsonl`
- remote control HTTP API
- Docker + Render deployment files

## Files

- `gate_futures_testnet_market_maker.py`
  The bot itself.
- `gate_futures_service.py`
  Remote control service with `/start`, `/stop`, `/run-once`, `/status`, `/logs`, `/trades`.
- `gate_futures_config.example.json`
  Conservative local config example.
- `Dockerfile`
  Cloud image for deployment.
- `render.yaml`
  Render deployment template.

## Conservative BTC_USDT defaults

These are the baked-in conservative starting values:

- `contract`: `BTC_USDT`
- `base_spread_bps`: `18`
- `min_spread_bps`: `8`
- `position_skew_bps_per_max_position`: `16`
- `order_size_contracts`: `1`
- `max_position_contracts`: `3`
- `leverage`: `2`
- `loop_interval_seconds`: `8`
- `reprice_tolerance_bps`: `3`
- `max_order_age_seconds`: `25`
- `trade_poll_interval_loops`: `3`

The idea is to keep the bot slower, wider, and smaller on first testnet runs.

## Local run

1. Create a Gate Futures Testnet API key and secret.
2. Copy `gate_futures_config.example.json` to `gate_futures_config.local.json`.
3. Fill in your testnet credentials.

Public smoke test:

```bash
python paper_market_maker/gate_futures_testnet_market_maker.py --public-smoke-test --contract BTC_USDT --settle usdt
```

One loop:

```bash
python paper_market_maker/gate_futures_testnet_market_maker.py --config paper_market_maker/gate_futures_config.local.json --once
```

Continuous mode:

```bash
python paper_market_maker/gate_futures_testnet_market_maker.py --config paper_market_maker/gate_futures_config.local.json
```

## Local logs and trade records

By default the bot writes to `paper_market_maker/data`:

- `state.json`
  Latest bot state snapshot.
- `events.jsonl`
  Order placement, cancellation, loop success, loop errors.
- `trades.jsonl`
  New fills pulled from Gate testnet trade history.

These files are append-only except `state.json`, which always stores the latest snapshot.

## Remote control service

Start the control service locally:

```bash
$env:GATE_MM_CONFIG_PATH="paper_market_maker/gate_futures_config.local.json"
$env:GATE_MM_ADMIN_TOKEN="replace-with-a-secret-token"
python paper_market_maker/gate_futures_service.py
```

Endpoints:

- `GET /health`
  No auth, for health checks.
- `GET /status`
  Requires header `X-Admin-Token`.
- `GET /logs?limit=100`
  Requires header `X-Admin-Token`.
- `GET /trades?limit=100`
  Requires header `X-Admin-Token`.
- `POST /start`
  Requires header `X-Admin-Token`.
- `POST /stop`
  Requires header `X-Admin-Token`.
- `POST /run-once`
  Requires header `X-Admin-Token`.

Example:

```bash
curl -H "X-Admin-Token: replace-with-a-secret-token" http://127.0.0.1:8080/status
curl -X POST -H "X-Admin-Token: replace-with-a-secret-token" http://127.0.0.1:8080/stop
curl -X POST -H "X-Admin-Token: replace-with-a-secret-token" http://127.0.0.1:8080/start
```

## Cloud deployment

The service is ready for Render using `paper_market_maker/render.yaml`.

Set these env vars in the cloud service:

- `GATE_MM_API_KEY`
- `GATE_MM_API_SECRET`
- `GATE_MM_ADMIN_TOKEN`

Optional env vars if you want to override defaults:

- `GATE_MM_CONTRACT`
- `GATE_MM_SETTLE`
- `GATE_MM_BASE_SPREAD_BPS`
- `GATE_MM_MIN_SPREAD_BPS`
- `GATE_MM_POSITION_SKEW_BPS`
- `GATE_MM_ORDER_SIZE_CONTRACTS`
- `GATE_MM_MAX_POSITION_CONTRACTS`
- `GATE_MM_LEVERAGE`
- `GATE_MM_LOOP_INTERVAL_SECONDS`
- `GATE_MM_REPRICE_TOLERANCE_BPS`
- `GATE_MM_MAX_ORDER_AGE_SECONDS`
- `GATE_MM_TRADE_POLL_INTERVAL_LOOPS`
- `GATE_MM_AUTO_START`
- `GATE_MM_DRY_RUN`
- `GATE_MM_DATA_DIR`

Render will persist logs and state under `/app/data` via the attached disk.

## Important notes

- This project targets **Gate Futures Testnet**, not Gate spot.
- Orders are submitted with `tif=poc`, which is Gate's post-only mode.
- Positive `size` means bid / buy, negative `size` means ask / sell.
- Gate testnet can intermittently return `502 Bad Gateway`. If that happens, retry later.
- I prepared the service for cloud deployment, but I cannot complete an actual deployment without access to your cloud account or deployment target.
