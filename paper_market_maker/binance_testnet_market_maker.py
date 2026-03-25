#!/usr/bin/env python3
"""Simple market-making bot for Binance Spot Testnet.

The bot places one bid and one ask around the mid price, keeps them fresh,
and skews quoting when inventory drifts away from the configured target.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP, getcontext
from pathlib import Path
from typing import Any


getcontext().prec = 28
LOGGER = logging.getLogger("paper_market_maker")


class BinanceApiError(RuntimeError):
    """Raised when Binance returns a non-success response."""


def decimal_from_value(value: Any) -> Decimal:
    return Decimal(str(value))


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def format_decimal(value: Decimal, step: Decimal) -> str:
    if step <= 0:
        return format(value.normalize(), "f")
    decimals = max(0, -step.normalize().as_tuple().exponent)
    quantized = value.quantize(step)
    return f"{quantized:.{decimals}f}"


@dataclass(slots=True)
class BotConfig:
    api_key: str
    api_secret: str
    symbol: str
    base_url: str = "https://testnet.binance.vision"
    target_base_ratio: Decimal = Decimal("0.50")
    base_spread_bps: Decimal = Decimal("45")
    min_spread_bps: Decimal = Decimal("20")
    inventory_skew_bps_per_ratio: Decimal = Decimal("80")
    order_quote_size: Decimal = Decimal("25")
    loop_interval_seconds: int = 5
    reprice_tolerance_bps: Decimal = Decimal("12")
    max_order_age_seconds: int = 30
    recv_window_ms: int = 5000
    dry_run: bool = False
    log_level: str = "INFO"

    @classmethod
    def from_json(cls, path: Path) -> "BotConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            api_key=raw["api_key"],
            api_secret=raw["api_secret"],
            symbol=str(raw["symbol"]).replace("/", "").upper(),
            base_url=raw.get("base_url", cls.base_url),
            target_base_ratio=decimal_from_value(raw.get("target_base_ratio", cls.target_base_ratio)),
            base_spread_bps=decimal_from_value(raw.get("base_spread_bps", cls.base_spread_bps)),
            min_spread_bps=decimal_from_value(raw.get("min_spread_bps", cls.min_spread_bps)),
            inventory_skew_bps_per_ratio=decimal_from_value(
                raw.get("inventory_skew_bps_per_ratio", cls.inventory_skew_bps_per_ratio)
            ),
            order_quote_size=decimal_from_value(raw.get("order_quote_size", cls.order_quote_size)),
            loop_interval_seconds=int(raw.get("loop_interval_seconds", cls.loop_interval_seconds)),
            reprice_tolerance_bps=decimal_from_value(raw.get("reprice_tolerance_bps", cls.reprice_tolerance_bps)),
            max_order_age_seconds=int(raw.get("max_order_age_seconds", cls.max_order_age_seconds)),
            recv_window_ms=int(raw.get("recv_window_ms", cls.recv_window_ms)),
            dry_run=bool(raw.get("dry_run", cls.dry_run)),
            log_level=str(raw.get("log_level", cls.log_level)).upper(),
        )


@dataclass(slots=True)
class SymbolRules:
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal
    base_asset: str
    quote_asset: str


@dataclass(slots=True)
class OrderIntent:
    side: str
    price: Decimal
    quantity: Decimal


class BinanceRestClient:
    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._base_url = config.base_url.rstrip("/")
        self._api_key = config.api_key
        self._api_secret = config.api_secret.encode("utf-8")
        self._time_offset_ms = 0
        self.sync_time()

    def sync_time(self) -> None:
        payload = self.public_request("GET", "/api/v3/time")
        server_time_ms = int(payload["serverTime"])
        self._time_offset_ms = server_time_ms - int(time.time() * 1000)
        LOGGER.debug("Synced time offset: %sms", self._time_offset_ms)

    def public_request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request(method, path, params=params, signed=False)

    def signed_request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
        params["recvWindow"] = self._config.recv_window_ms
        query = urllib.parse.urlencode(params, doseq=True)
        signature = hmac.new(self._api_secret, query.encode("utf-8"), hashlib.sha256).hexdigest()
        params["signature"] = signature
        return self._request(method, path, params=params, signed=True)

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        signed: bool,
    ) -> Any:
        params = params or {}
        encoded = urllib.parse.urlencode(params, doseq=True)
        method = method.upper()
        headers = {"User-Agent": "paper-market-maker/1.0"}
        if signed:
            headers["X-MBX-APIKEY"] = self._api_key

        if method == "GET" or method == "DELETE":
            url = f"{self._base_url}{path}"
            if encoded:
                url = f"{url}?{encoded}"
            data = None
        else:
            url = f"{self._base_url}{path}"
            data = encoded.encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            try:
                payload = json.loads(body)
                message = payload.get("msg", body)
                code = payload.get("code")
            except json.JSONDecodeError:
                code = exc.code
                message = body
            raise BinanceApiError(f"HTTP {exc.code} / code={code}: {message}") from exc
        except urllib.error.URLError as exc:
            raise BinanceApiError(f"Network error: {exc.reason}") from exc

        payload = json.loads(body)
        if isinstance(payload, dict) and payload.get("code", 0) not in (0, None) and "msg" in payload:
            raise BinanceApiError(f"code={payload['code']}: {payload['msg']}")
        return payload


class MarketMakerBot:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.client = BinanceRestClient(config)
        self.rules = self._load_rules()

    def _load_rules(self) -> SymbolRules:
        payload = self.client.public_request("GET", "/api/v3/exchangeInfo", {"symbol": self.config.symbol})
        symbol_info = payload["symbols"][0]
        filters = {entry["filterType"]: entry for entry in symbol_info["filters"]}
        return SymbolRules(
            tick_size=decimal_from_value(filters["PRICE_FILTER"]["tickSize"]),
            step_size=decimal_from_value(filters["LOT_SIZE"]["stepSize"]),
            min_qty=decimal_from_value(filters["LOT_SIZE"]["minQty"]),
            min_notional=decimal_from_value(filters["MIN_NOTIONAL"]["minNotional"]),
            base_asset=symbol_info["baseAsset"],
            quote_asset=symbol_info["quoteAsset"],
        )

    def _get_account_balances(self) -> dict[str, dict[str, Decimal]]:
        payload = self.client.signed_request("GET", "/api/v3/account")
        balances: dict[str, dict[str, Decimal]] = {}
        for item in payload["balances"]:
            balances[item["asset"]] = {
                "free": decimal_from_value(item["free"]),
                "locked": decimal_from_value(item["locked"]),
            }
        return balances

    def _get_book_ticker(self) -> tuple[Decimal, Decimal]:
        payload = self.client.public_request("GET", "/api/v3/ticker/bookTicker", {"symbol": self.config.symbol})
        return decimal_from_value(payload["bidPrice"]), decimal_from_value(payload["askPrice"])

    def _get_open_orders(self) -> list[dict[str, Any]]:
        return self.client.signed_request("GET", "/api/v3/openOrders", {"symbol": self.config.symbol})

    def _cancel_order(self, order_id: int) -> None:
        if self.config.dry_run:
            LOGGER.info("[DRY RUN] cancel orderId=%s", order_id)
            return
        self.client.signed_request("DELETE", "/api/v3/order", {"symbol": self.config.symbol, "orderId": order_id})
        LOGGER.info("Canceled order %s", order_id)

    def _place_order(self, intent: OrderIntent) -> None:
        payload = {
            "symbol": self.config.symbol,
            "side": intent.side,
            "type": "LIMIT_MAKER",
            "quantity": format_decimal(intent.quantity, self.rules.step_size),
            "price": format_decimal(intent.price, self.rules.tick_size),
        }
        if self.config.dry_run:
            LOGGER.info("[DRY RUN] place %s %s @ %s", intent.side, payload["quantity"], payload["price"])
            return
        response = self.client.signed_request("POST", "/api/v3/order", payload)
        LOGGER.info("Placed %s orderId=%s qty=%s price=%s", intent.side, response["orderId"], payload["quantity"], payload["price"])

    def _compute_inventory_ratio(
        self,
        mid_price: Decimal,
        balances: dict[str, dict[str, Decimal]],
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        base = balances.get(self.rules.base_asset, {"free": Decimal("0"), "locked": Decimal("0")})
        quote = balances.get(self.rules.quote_asset, {"free": Decimal("0"), "locked": Decimal("0")})
        base_total = base["free"] + base["locked"]
        quote_total = quote["free"] + quote["locked"]
        portfolio_value = base_total * mid_price + quote_total
        if portfolio_value <= 0:
            raise BinanceApiError(
                f"No balance found for {self.rules.base_asset}/{self.rules.quote_asset}. Fund the Spot Testnet account first."
            )
        base_ratio = (base_total * mid_price) / portfolio_value
        return base_ratio, base["free"], quote["free"], portfolio_value

    def _build_order_intents(
        self,
        best_bid: Decimal,
        best_ask: Decimal,
        balances: dict[str, dict[str, Decimal]],
    ) -> dict[str, OrderIntent | None]:
        mid_price = (best_bid + best_ask) / Decimal("2")
        base_ratio, free_base, free_quote, portfolio_value = self._compute_inventory_ratio(mid_price, balances)

        inventory_error = base_ratio - self.config.target_base_ratio
        skew_bps = self.config.inventory_skew_bps_per_ratio * inventory_error
        bid_spread_bps = max(self.config.min_spread_bps, self.config.base_spread_bps + skew_bps)
        ask_spread_bps = max(self.config.min_spread_bps, self.config.base_spread_bps - skew_bps)

        desired_bid = min(mid_price * (Decimal("1") - bid_spread_bps / Decimal("10000")), best_bid)
        desired_ask = max(mid_price * (Decimal("1") + ask_spread_bps / Decimal("10000")), best_ask)

        bid_price = floor_to_step(desired_bid, self.rules.tick_size)
        ask_price = ceil_to_step(desired_ask, self.rules.tick_size)

        bid_qty = floor_to_step(self.config.order_quote_size / bid_price, self.rules.step_size)
        ask_qty = floor_to_step(self.config.order_quote_size / ask_price, self.rules.step_size)

        intents: dict[str, OrderIntent | None] = {"BUY": None, "SELL": None}

        if free_quote >= self.config.order_quote_size and bid_qty >= self.rules.min_qty and bid_qty * bid_price >= self.rules.min_notional:
            intents["BUY"] = OrderIntent(side="BUY", price=bid_price, quantity=bid_qty)

        if free_base >= ask_qty and ask_qty >= self.rules.min_qty and ask_qty * ask_price >= self.rules.min_notional:
            intents["SELL"] = OrderIntent(side="SELL", price=ask_price, quantity=ask_qty)

        LOGGER.info(
            "mid=%s base_ratio=%.2f%% portfolio=%s %s target=%.2f%% bid_spread=%.1fbps ask_spread=%.1fbps",
            format_decimal(mid_price, self.rules.tick_size),
            float(base_ratio * Decimal("100")),
            portfolio_value.quantize(Decimal("0.01")),
            self.rules.quote_asset,
            float(self.config.target_base_ratio * Decimal("100")),
            float(bid_spread_bps),
            float(ask_spread_bps),
        )
        return intents

    def _bips_from_price_delta(self, current: Decimal, target: Decimal) -> Decimal:
        if target <= 0:
            return Decimal("0")
        return abs((current - target) / target) * Decimal("10000")

    def _select_and_clean_orders(
        self,
        open_orders: list[dict[str, Any]],
        intents: dict[str, OrderIntent | None],
    ) -> dict[str, dict[str, Any] | None]:
        keep: dict[str, dict[str, Any] | None] = {"BUY": None, "SELL": None}
        now_ms = int(time.time() * 1000)

        grouped: dict[str, list[dict[str, Any]]] = {"BUY": [], "SELL": []}
        for order in open_orders:
            grouped[order["side"]].append(order)

        for side, orders in grouped.items():
            target = intents.get(side)
            if not orders:
                continue

            if target is None:
                for order in orders:
                    self._cancel_order(int(order["orderId"]))
                continue

            orders.sort(
                key=lambda item: self._bips_from_price_delta(decimal_from_value(item["price"]), target.price)
            )
            primary = orders[0]
            keep[side] = primary

            for extra in orders[1:]:
                self._cancel_order(int(extra["orderId"]))

            current_price = decimal_from_value(primary["price"])
            price_delta_bps = self._bips_from_price_delta(current_price, target.price)
            age_seconds = (now_ms - int(primary["time"])) / 1000
            if price_delta_bps > self.config.reprice_tolerance_bps or age_seconds > self.config.max_order_age_seconds:
                LOGGER.info(
                    "Refreshing %s orderId=%s age=%.1fs delta=%.1fbps",
                    side,
                    primary["orderId"],
                    age_seconds,
                    float(price_delta_bps),
                )
                self._cancel_order(int(primary["orderId"]))
                keep[side] = None

        return keep

    def run_once(self) -> None:
        try:
            best_bid, best_ask = self._get_book_ticker()
            balances = self._get_account_balances()
            intents = self._build_order_intents(best_bid, best_ask, balances)
            open_orders = self._get_open_orders()
            active_orders = self._select_and_clean_orders(open_orders, intents)

            if any(active_orders[side] is None and intents[side] is not None for side in ("BUY", "SELL")):
                balances = self._get_account_balances()
                best_bid, best_ask = self._get_book_ticker()
                intents = self._build_order_intents(best_bid, best_ask, balances)

            for side in ("BUY", "SELL"):
                if active_orders[side] is None and intents[side] is not None:
                    self._place_order(intents[side])
        except BinanceApiError as exc:
            if "code=-1021" in str(exc):
                LOGGER.warning("Timestamp drift detected, re-syncing time")
                self.client.sync_time()
                return
            LOGGER.error("Bot loop failed: %s", exc)

    def run_forever(self) -> None:
        LOGGER.info("Starting market-maker for %s on %s", self.config.symbol, self.config.base_url)
        while True:
            self.run_once()
            time.sleep(self.config.loop_interval_seconds)


def public_smoke_test(symbol: str, base_url: str) -> None:
    url = f"{base_url.rstrip('/')}/api/v3/ticker/bookTicker?symbol={symbol}"
    with urllib.request.urlopen(url, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    print(json.dumps(payload, indent=2))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simple Binance Spot Testnet market maker")
    parser.add_argument("--config", type=Path, default=Path("paper_market_maker/config.example.json"))
    parser.add_argument("--once", action="store_true", help="Run a single loop and exit")
    parser.add_argument("--public-smoke-test", action="store_true", help="Fetch bookTicker without API credentials")
    parser.add_argument("--symbol", default="BTCUSDT", help="Symbol for --public-smoke-test")
    parser.add_argument("--base-url", default="https://testnet.binance.vision", help="Base URL for --public-smoke-test")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.public_smoke_test:
        public_smoke_test(args.symbol.replace("/", "").upper(), args.base_url)
        return 0

    config = BotConfig.from_json(args.config)
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    bot = MarketMakerBot(config)
    if args.once:
        bot.run_once()
        return 0
    bot.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
