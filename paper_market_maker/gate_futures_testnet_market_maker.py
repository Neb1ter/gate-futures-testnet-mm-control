#!/usr/bin/env python3
"""Gate Futures Testnet market maker with persistent state and trade logs."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP, getcontext
from pathlib import Path
from typing import Any


getcontext().prec = 28
LOGGER = logging.getLogger("gate_testnet_mm")
EMPTY_SHA512 = hashlib.sha512(b"").hexdigest()


class GateApiError(RuntimeError):
    """Raised when Gate Futures API returns an error."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def decimal_from_value(value: Any) -> Decimal:
    return Decimal(str(value))


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def floor_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return value
    return (value / tick).to_integral_value(rounding=ROUND_DOWN) * tick


def ceil_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return value
    return (value / tick).to_integral_value(rounding=ROUND_UP) * tick


def format_decimal(value: Decimal, tick: Decimal) -> str:
    decimals = max(0, -tick.normalize().as_tuple().exponent) if tick > 0 else 8
    return f"{value:.{decimals}f}"


@dataclass(slots=True)
class GateBotConfig:
    api_key: str
    api_secret: str
    contract: str
    settle: str = "usdt"
    base_url: str = "https://fx-api-testnet.gateio.ws"
    base_spread_bps: Decimal = Decimal("18")
    min_spread_bps: Decimal = Decimal("8")
    position_skew_bps_per_max_position: Decimal = Decimal("16")
    order_size_contracts: Decimal = Decimal("1")
    max_position_contracts: Decimal = Decimal("3")
    leverage: Decimal = Decimal("2")
    loop_interval_seconds: int = 8
    reprice_tolerance_bps: Decimal = Decimal("3")
    max_order_age_seconds: int = 25
    trade_poll_interval_loops: int = 3
    dry_run: bool = False
    log_level: str = "INFO"
    data_dir: Path = Path("paper_market_maker/data")
    state_file_name: str = "state.json"
    events_file_name: str = "events.jsonl"
    trades_file_name: str = "trades.jsonl"

    @classmethod
    def from_json(cls, path: Path) -> "GateBotConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls._from_mapping(raw)

    @classmethod
    def from_env(cls) -> "GateBotConfig":
        raw: dict[str, Any] = {
            "api_key": os.getenv("GATE_MM_API_KEY", ""),
            "api_secret": os.getenv("GATE_MM_API_SECRET", ""),
            "contract": os.getenv("GATE_MM_CONTRACT", "BTC_USDT"),
            "settle": os.getenv("GATE_MM_SETTLE", "usdt"),
            "base_url": os.getenv("GATE_MM_BASE_URL", "https://fx-api-testnet.gateio.ws"),
            "base_spread_bps": os.getenv("GATE_MM_BASE_SPREAD_BPS", "18"),
            "min_spread_bps": os.getenv("GATE_MM_MIN_SPREAD_BPS", "8"),
            "position_skew_bps_per_max_position": os.getenv("GATE_MM_POSITION_SKEW_BPS", "16"),
            "order_size_contracts": os.getenv("GATE_MM_ORDER_SIZE_CONTRACTS", "1"),
            "max_position_contracts": os.getenv("GATE_MM_MAX_POSITION_CONTRACTS", "3"),
            "leverage": os.getenv("GATE_MM_LEVERAGE", "2"),
            "loop_interval_seconds": os.getenv("GATE_MM_LOOP_INTERVAL_SECONDS", "8"),
            "reprice_tolerance_bps": os.getenv("GATE_MM_REPRICE_TOLERANCE_BPS", "3"),
            "max_order_age_seconds": os.getenv("GATE_MM_MAX_ORDER_AGE_SECONDS", "25"),
            "trade_poll_interval_loops": os.getenv("GATE_MM_TRADE_POLL_INTERVAL_LOOPS", "3"),
            "dry_run": env_bool("GATE_MM_DRY_RUN", False),
            "log_level": os.getenv("GATE_MM_LOG_LEVEL", "INFO"),
            "data_dir": os.getenv("GATE_MM_DATA_DIR", "paper_market_maker/data"),
        }
        return cls._from_mapping(raw)

    @classmethod
    def from_file_or_env(cls, path: Path | None) -> "GateBotConfig":
        if path and path.exists():
            return cls.from_json(path)
        return cls.from_env()

    @classmethod
    def _from_mapping(cls, raw: dict[str, Any]) -> "GateBotConfig":
        api_key = str(raw.get("api_key", "")).strip()
        api_secret = str(raw.get("api_secret", "")).strip()
        contract = str(raw.get("contract", "")).strip().upper()
        if not api_key or not api_secret or not contract:
            raise ValueError("Missing api_key, api_secret, or contract in config/environment.")
        return cls(
            api_key=api_key,
            api_secret=api_secret,
            contract=contract,
            settle=str(raw.get("settle", cls.settle)).lower(),
            base_url=str(raw.get("base_url", cls.base_url)),
            base_spread_bps=decimal_from_value(raw.get("base_spread_bps", cls.base_spread_bps)),
            min_spread_bps=decimal_from_value(raw.get("min_spread_bps", cls.min_spread_bps)),
            position_skew_bps_per_max_position=decimal_from_value(
                raw.get("position_skew_bps_per_max_position", cls.position_skew_bps_per_max_position)
            ),
            order_size_contracts=decimal_from_value(raw.get("order_size_contracts", cls.order_size_contracts)),
            max_position_contracts=decimal_from_value(raw.get("max_position_contracts", cls.max_position_contracts)),
            leverage=decimal_from_value(raw.get("leverage", cls.leverage)),
            loop_interval_seconds=int(raw.get("loop_interval_seconds", cls.loop_interval_seconds)),
            reprice_tolerance_bps=decimal_from_value(raw.get("reprice_tolerance_bps", cls.reprice_tolerance_bps)),
            max_order_age_seconds=int(raw.get("max_order_age_seconds", cls.max_order_age_seconds)),
            trade_poll_interval_loops=int(raw.get("trade_poll_interval_loops", cls.trade_poll_interval_loops)),
            dry_run=bool(raw.get("dry_run", cls.dry_run)),
            log_level=str(raw.get("log_level", cls.log_level)).upper(),
            data_dir=Path(raw.get("data_dir", cls.data_dir)),
            state_file_name=str(raw.get("state_file_name", cls.state_file_name)),
            events_file_name=str(raw.get("events_file_name", cls.events_file_name)),
            trades_file_name=str(raw.get("trades_file_name", cls.trades_file_name)),
        )


@dataclass(slots=True)
class ContractRules:
    order_price_round: Decimal
    order_size_min: Decimal
    order_size_max: Decimal
    quanto_multiplier: Decimal
    enable_decimal: bool


@dataclass(slots=True)
class OrderIntent:
    side: str
    size: Decimal
    price: Decimal


class BotRecorder:
    def __init__(self, data_dir: Path, state_file_name: str, events_file_name: str, trades_file_name: str) -> None:
        self._data_dir = data_dir
        self._state_path = data_dir / state_file_name
        self._events_path = data_dir / events_file_name
        self._trades_path = data_dir / trades_file_name
        self._lock = threading.Lock()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        if not self._state_path.exists():
            self.write_state({"known_trade_ids": [], "created_at": utc_now_iso()})

    @property
    def state_path(self) -> Path:
        return self._state_path

    @property
    def events_path(self) -> Path:
        return self._events_path

    @property
    def trades_path(self) -> Path:
        return self._trades_path

    def read_state(self) -> dict[str, Any]:
        with self._lock:
            if not self._state_path.exists():
                return {}
            return json.loads(self._state_path.read_text(encoding="utf-8"))

    def write_state(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._state_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")

    def append_event(self, event_type: str, payload: dict[str, Any]) -> None:
        entry = {"timestamp": utc_now_iso(), "type": event_type, **payload}
        with self._lock:
            with self._events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=True) + "\n")

    def append_trade(self, payload: dict[str, Any]) -> None:
        entry = {"timestamp_recorded": utc_now_iso(), **payload}
        with self._lock:
            with self._trades_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=True) + "\n")

    def tail_jsonl(self, path: Path, limit: int = 100) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        result: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            if not line.strip():
                continue
            result.append(json.loads(line))
        return result


class GateRestClient:
    def __init__(self, config: GateBotConfig) -> None:
        self._base_url = config.base_url.rstrip("/")
        self._prefix = "/api/v4"
        self._api_key = config.api_key
        self._api_secret = config.api_secret.encode("utf-8")

    def public_request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request(method, path, params=params, signed=False)

    def signed_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        return self._request(method, path, params=params, body=body, signed=True)

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        method = method.upper()
        params = params or {}
        body = body or {}
        query_string = urllib.parse.urlencode(params, doseq=True)
        request_path = f"{self._prefix}{path}"
        url = f"{self._base_url}{request_path}"
        if query_string:
            url = f"{url}?{query_string}"

        payload_string = json.dumps(body, separators=(",", ":")) if body else ""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "gate-testnet-market-maker/1.0",
        }

        if signed:
            timestamp = str(int(time.time()))
            hashed_payload = hashlib.sha512(payload_string.encode("utf-8")).hexdigest() if payload_string else EMPTY_SHA512
            sign_string = f"{method}\n{request_path}\n{query_string}\n{hashed_payload}\n{timestamp}"
            signature = hmac.new(self._api_secret, sign_string.encode("utf-8"), hashlib.sha512).hexdigest()
            headers.update({"KEY": self._api_key, "Timestamp": timestamp, "SIGN": signature})

        data = payload_string.encode("utf-8") if payload_string else None
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw_body = exc.read().decode("utf-8")
            try:
                payload = json.loads(raw_body)
                message = payload.get("message", raw_body)
                label = payload.get("label", exc.code)
            except json.JSONDecodeError:
                message = raw_body
                label = exc.code
            raise GateApiError(f"HTTP {exc.code} / {label}: {message}") from exc
        except urllib.error.URLError as exc:
            raise GateApiError(f"Network error: {exc.reason}") from exc

        if not raw_body:
            return None
        payload = json.loads(raw_body)
        if isinstance(payload, dict) and payload.get("label") and payload.get("message"):
            raise GateApiError(f"{payload['label']}: {payload['message']}")
        return payload


class GateMarketMakerBot:
    def __init__(self, config: GateBotConfig, recorder: BotRecorder | None = None) -> None:
        self.config = config
        self.recorder = recorder or BotRecorder(
            config.data_dir,
            config.state_file_name,
            config.events_file_name,
            config.trades_file_name,
        )
        self.client = GateRestClient(config)
        self.rules = self._load_rules()
        self.loop_count = 0
        self.last_error: str | None = None
        self.last_success_at: str | None = None
        self.last_snapshot: dict[str, Any] = {}
        self._ensure_leverage()

    def _load_rules(self) -> ContractRules:
        payload = self.client.public_request(
            "GET",
            f"/futures/{self.config.settle}/contracts/{self.config.contract}",
        )
        return ContractRules(
            order_price_round=decimal_from_value(payload["order_price_round"]),
            order_size_min=decimal_from_value(payload["order_size_min"]),
            order_size_max=decimal_from_value(payload["order_size_max"]),
            quanto_multiplier=decimal_from_value(payload.get("quanto_multiplier", "0")),
            enable_decimal=bool(payload.get("enable_decimal", False)),
        )

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "contract": self.config.contract,
            "settle": self.config.settle,
            "loop_count": self.loop_count,
            "last_error": self.last_error,
            "last_success_at": self.last_success_at,
            "last_snapshot": self.last_snapshot,
            "state_file": str(self.recorder.state_path),
            "events_file": str(self.recorder.events_path),
            "trades_file": str(self.recorder.trades_path),
        }

    def _ensure_leverage(self) -> None:
        if self.config.dry_run:
            self.recorder.append_event("set_leverage_dry_run", {"leverage": str(self.config.leverage)})
            LOGGER.info("[DRY RUN] set leverage=%s", self.config.leverage)
            return
        self.client.signed_request(
            "POST",
            f"/futures/{self.config.settle}/positions/{self.config.contract}/leverage",
            params={"leverage": format_decimal(self.config.leverage, Decimal("0.01")).rstrip("0").rstrip(".")},
        )
        self.recorder.append_event("set_leverage", {"leverage": str(self.config.leverage)})
        LOGGER.info("Leverage set to %s", self.config.leverage)

    def _get_account(self) -> dict[str, Any]:
        return self.client.signed_request("GET", f"/futures/{self.config.settle}/accounts")

    def _get_position(self) -> dict[str, Any]:
        return self.client.signed_request(
            "GET",
            f"/futures/{self.config.settle}/positions/{self.config.contract}",
        )

    def _get_open_orders(self) -> list[dict[str, Any]]:
        return self.client.signed_request(
            "GET",
            f"/futures/{self.config.settle}/orders",
            params={"contract": self.config.contract, "status": "open"},
        )

    def _get_recent_trades(self) -> list[dict[str, Any]]:
        return self.client.signed_request(
            "GET",
            f"/futures/{self.config.settle}/my_trades",
            params={"contract": self.config.contract, "limit": 100},
        )

    def _get_order_book(self) -> tuple[Decimal, Decimal]:
        payload = self.client.public_request(
            "GET",
            f"/futures/{self.config.settle}/order_book",
            params={"contract": self.config.contract, "limit": 1},
        )
        best_ask = decimal_from_value(payload["asks"][0]["p"])
        best_bid = decimal_from_value(payload["bids"][0]["p"])
        return best_bid, best_ask

    def _cancel_order(self, order_id: int) -> None:
        if self.config.dry_run:
            self.recorder.append_event("cancel_order_dry_run", {"order_id": order_id})
            LOGGER.info("[DRY RUN] cancel orderId=%s", order_id)
            return
        self.client.signed_request(
            "DELETE",
            f"/futures/{self.config.settle}/orders/{order_id}",
        )
        self.recorder.append_event("cancel_order", {"order_id": order_id})
        LOGGER.info("Canceled order %s", order_id)

    def _place_order(self, intent: OrderIntent) -> None:
        body = {
            "contract": self.config.contract,
            "size": self._format_size(intent.size),
            "price": format_decimal(intent.price, self.rules.order_price_round),
            "tif": "poc",
            "text": f"t-mm-{intent.side.lower()}",
        }
        if self.config.dry_run:
            self.recorder.append_event("place_order_dry_run", body | {"side": intent.side})
            LOGGER.info("[DRY RUN] place %s size=%s price=%s", intent.side, body["size"], body["price"])
            return
        response = self.client.signed_request(
            "POST",
            f"/futures/{self.config.settle}/orders",
            body=body,
        )
        self.recorder.append_event(
            "place_order",
            {
                "side": intent.side,
                "order_id": response["id"],
                "size": body["size"],
                "price": body["price"],
            },
        )
        LOGGER.info("Placed %s order id=%s size=%s price=%s", intent.side, response["id"], body["size"], body["price"])

    def _format_size(self, size: Decimal) -> str:
        if self.rules.enable_decimal:
            return format(size.normalize(), "f")
        rounding = ROUND_DOWN if size >= 0 else ROUND_UP
        return str(int(size.to_integral_value(rounding=rounding)))

    def _clip_size(self, size: Decimal) -> Decimal:
        absolute = min(abs(size), self.rules.order_size_max)
        if absolute < self.rules.order_size_min:
            absolute = self.rules.order_size_min
        if not self.rules.enable_decimal:
            absolute = absolute.to_integral_value(rounding=ROUND_DOWN)
        return absolute if size >= 0 else -absolute

    def _compute_intents(self, best_bid: Decimal, best_ask: Decimal, position_size: Decimal) -> dict[str, OrderIntent | None]:
        mid = (best_bid + best_ask) / Decimal("2")
        normalized_position = Decimal("0")
        if self.config.max_position_contracts > 0:
            normalized_position = max(
                Decimal("-1"),
                min(Decimal("1"), position_size / self.config.max_position_contracts),
            )
        skew = normalized_position * self.config.position_skew_bps_per_max_position

        bid_spread = max(self.config.min_spread_bps, self.config.base_spread_bps + skew)
        ask_spread = max(self.config.min_spread_bps, self.config.base_spread_bps - skew)

        bid_price = floor_to_tick(
            min(mid * (Decimal("1") - bid_spread / Decimal("10000")), best_bid),
            self.rules.order_price_round,
        )
        ask_price = ceil_to_tick(
            max(mid * (Decimal("1") + ask_spread / Decimal("10000")), best_ask),
            self.rules.order_price_round,
        )

        intents: dict[str, OrderIntent | None] = {"BUY": None, "SELL": None}
        buy_size = self._clip_size(self.config.order_size_contracts)
        sell_size = self._clip_size(-self.config.order_size_contracts)

        if position_size + buy_size <= self.config.max_position_contracts:
            intents["BUY"] = OrderIntent("BUY", buy_size, bid_price)
        if position_size + sell_size >= -self.config.max_position_contracts:
            intents["SELL"] = OrderIntent("SELL", sell_size, ask_price)

        LOGGER.info(
            "mid=%s position=%s contracts bid_spread=%.2fbps ask_spread=%.2fbps",
            format_decimal(mid, self.rules.order_price_round),
            position_size,
            float(bid_spread),
            float(ask_spread),
        )
        return intents

    def _price_delta_bps(self, current: Decimal, target: Decimal) -> Decimal:
        if target <= 0:
            return Decimal("0")
        return abs((current - target) / target) * Decimal("10000")

    def _select_and_cleanup_orders(
        self,
        open_orders: list[dict[str, Any]],
        intents: dict[str, OrderIntent | None],
    ) -> dict[str, dict[str, Any] | None]:
        keep: dict[str, dict[str, Any] | None] = {"BUY": None, "SELL": None}
        now = time.time()
        grouped: dict[str, list[dict[str, Any]]] = {"BUY": [], "SELL": []}
        for order in open_orders:
            side = "BUY" if decimal_from_value(order["size"]) > 0 else "SELL"
            grouped[side].append(order)

        for side, orders in grouped.items():
            target = intents.get(side)
            if not orders:
                continue
            if target is None:
                for order in orders:
                    self._cancel_order(int(order["id"]))
                continue

            orders.sort(key=lambda item: self._price_delta_bps(decimal_from_value(item["price"]), target.price))
            primary = orders[0]
            keep[side] = primary

            for extra in orders[1:]:
                self._cancel_order(int(extra["id"]))

            current_price = decimal_from_value(primary["price"])
            delta_bps = self._price_delta_bps(current_price, target.price)
            age = now - float(primary["create_time"])
            if delta_bps > self.config.reprice_tolerance_bps or age > self.config.max_order_age_seconds:
                LOGGER.info(
                    "Refreshing %s order id=%s age=%.1fs delta=%.2fbps",
                    side,
                    primary["id"],
                    age,
                    float(delta_bps),
                )
                self._cancel_order(int(primary["id"]))
                keep[side] = None

        return keep

    def _poll_new_trades(self) -> None:
        state = self.recorder.read_state()
        known_ids = list(state.get("known_trade_ids", []))
        known_set = set(str(item) for item in known_ids)
        trades = self._get_recent_trades()
        new_ids: list[str] = []
        for trade in trades:
            trade_id = str(trade.get("id") or trade.get("trade_id") or "")
            if not trade_id or trade_id in known_set:
                continue
            self.recorder.append_trade(trade)
            known_set.add(trade_id)
            new_ids.append(trade_id)
        if new_ids:
            self.recorder.append_event("new_trades", {"count": len(new_ids), "trade_ids": new_ids})
        state["known_trade_ids"] = list(known_set)[-500:]
        self.recorder.write_state(state)

    def _write_loop_state(
        self,
        account: dict[str, Any],
        position: dict[str, Any],
        best_bid: Decimal,
        best_ask: Decimal,
        open_orders: list[dict[str, Any]],
    ) -> None:
        snapshot = {
            "updated_at": utc_now_iso(),
            "contract": self.config.contract,
            "settle": self.config.settle,
            "available": str(account.get("available", "0")),
            "total": str(account.get("total", "0")),
            "currency": account.get("currency", self.config.settle.upper()),
            "position_size": str(position.get("size", "0")),
            "best_bid": str(best_bid),
            "best_ask": str(best_ask),
            "open_orders_count": len(open_orders),
            "dry_run": self.config.dry_run,
            "last_error": self.last_error,
            "last_success_at": self.last_success_at,
            "loop_count": self.loop_count,
            "known_trade_ids": self.recorder.read_state().get("known_trade_ids", []),
        }
        self.last_snapshot = snapshot
        self.recorder.write_state(snapshot)

    def run_once(self) -> bool:
        self.loop_count += 1
        try:
            account = self._get_account()
            position = self._get_position()
            best_bid, best_ask = self._get_order_book()
            position_size = decimal_from_value(position.get("size", 0))
            available = decimal_from_value(account.get("available", 0))
            LOGGER.info("available=%s %s", available, account.get("currency", self.config.settle.upper()))

            intents = self._compute_intents(best_bid, best_ask, position_size)
            open_orders = self._get_open_orders()
            active_orders = self._select_and_cleanup_orders(open_orders, intents)

            if any(active_orders[side] is None and intents[side] is not None for side in ("BUY", "SELL")):
                best_bid, best_ask = self._get_order_book()
                position = self._get_position()
                position_size = decimal_from_value(position.get("size", 0))
                intents = self._compute_intents(best_bid, best_ask, position_size)

            for side in ("BUY", "SELL"):
                if active_orders[side] is None and intents[side] is not None:
                    self._place_order(intents[side])

            if self.loop_count % max(1, self.config.trade_poll_interval_loops) == 0:
                self._poll_new_trades()

            self.last_error = None
            self.last_success_at = utc_now_iso()
            self._write_loop_state(account, position, best_bid, best_ask, open_orders)
            self.recorder.append_event(
                "loop_success",
                {
                    "available": str(account.get("available", "0")),
                    "position_size": str(position.get("size", "0")),
                    "best_bid": str(best_bid),
                    "best_ask": str(best_ask),
                    "open_orders_count": len(open_orders),
                },
            )
            return True
        except GateApiError as exc:
            self.last_error = str(exc)
            self.recorder.append_event("loop_error", {"message": self.last_error})
            LOGGER.error("Bot loop failed: %s", exc)
            return False

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        LOGGER.info(
            "Starting Gate Futures Testnet market maker for %s (%s)",
            self.config.contract,
            self.config.settle,
        )
        while stop_event is None or not stop_event.is_set():
            self.run_once()
            if stop_event is not None:
                if stop_event.wait(self.config.loop_interval_seconds):
                    break
            else:
                time.sleep(self.config.loop_interval_seconds)


def public_smoke_test(contract: str, settle: str, base_url: str) -> None:
    base = base_url.rstrip("/")
    query = urllib.parse.urlencode({"contract": contract.upper(), "limit": 1})
    url = f"{base}/api/v4/futures/{settle.lower()}/order_book?{query}"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        message = body or exc.reason
        raise GateApiError(f"Public smoke test failed with HTTP {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise GateApiError(f"Public smoke test failed: {exc.reason}") from exc
    print(json.dumps(payload, indent=2))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gate Futures Testnet market maker")
    parser.add_argument("--config", type=Path, default=Path("paper_market_maker/gate_futures_config.example.json"))
    parser.add_argument("--once", action="store_true", help="Run one loop and exit")
    parser.add_argument("--public-smoke-test", action="store_true", help="Fetch public order book without credentials")
    parser.add_argument("--contract", default="BTC_USDT", help="Contract for --public-smoke-test")
    parser.add_argument("--settle", default="usdt", help="Settle currency for --public-smoke-test")
    parser.add_argument("--base-url", default="https://fx-api-testnet.gateio.ws", help="Base URL for --public-smoke-test")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.public_smoke_test:
        try:
            public_smoke_test(args.contract, args.settle, args.base_url)
            return 0
        except GateApiError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    config_path = args.config if args.config.exists() else None
    config = GateBotConfig.from_file_or_env(config_path)
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    bot = GateMarketMakerBot(config)
    if args.once:
        bot.run_once()
        return 0
    bot.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
