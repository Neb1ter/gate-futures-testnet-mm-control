# Binance Spot Testnet Market Maker

This is a lightweight paper-trading market-making bot that talks directly to the Binance Spot Testnet API.

It is intentionally small and transparent:

- one bid + one ask around the mid price
- inventory-aware spread skewing
- stale order cancellation + repricing
- maker-only orders (`LIMIT_MAKER`)
- no third-party dependency required

## What it is good for

- validating the basic Hummingbot-style market-making loop with testnet funds
- testing API connectivity, order placement, fills, and inventory drift
- iterating on spread / refresh / skew settings before moving to a heavier framework

## Before you run it

1. Create a Binance Spot Testnet account at `https://testnet.binance.vision`.
2. Generate an API key and secret there.
3. Copy `config.example.json` to a private file such as `config.local.json`.
4. Paste your testnet API key + secret into that file.

Binance documents that Spot Testnet uses `https://testnet.binance.vision/api`, only `/api` endpoints are available there, balances are virtual, and the environment is reset periodically.

## Quick start

Public market-data smoke test:

```bash
python paper_market_maker/binance_testnet_market_maker.py --public-smoke-test --symbol BTCUSDT
```

One loop with your testnet credentials:

```bash
python paper_market_maker/binance_testnet_market_maker.py --config paper_market_maker/config.local.json --once
```

Continuous mode:

```bash
python paper_market_maker/binance_testnet_market_maker.py --config paper_market_maker/config.local.json
```

## Recommended first settings

These are deliberately conservative starting points:

- `symbol`: `BTCUSDT` or `ETHUSDT`
- `base_spread_bps`: `40-60`
- `min_spread_bps`: `20-30`
- `order_quote_size`: `10-25`
- `loop_interval_seconds`: `5-10`
- `max_order_age_seconds`: `20-45`
- `target_base_ratio`: `0.5`
- `inventory_skew_bps_per_ratio`: `60-100`

## Important notes

- The bot only uses maker orders. If the market moves into your quote before placement, Binance may reject the order; the next loop will retry.
- The bot keeps one live order per side for the configured symbol.
- If balances are too small for the exchange filters, the bot will skip that side.
- Start with the testnet only. Do not point the script at live endpoints until you have reviewed the logic and tightened the safeguards for your own use.
