#!/usr/bin/env python3
"""Remote control service for the Gate Futures Testnet market maker."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from gate_futures_testnet_market_maker import BotRecorder, GateBotConfig, GateMarketMakerBot


LOGGER = logging.getLogger("gate_testnet_mm_service")


class BotSupervisor:
    def __init__(self, config_path: Path | None = None) -> None:
        self._config_path = config_path
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._bot: GateMarketMakerBot | None = None
        self._last_service_error: str | None = None
        self._recorder: BotRecorder | None = None

    def _build_bot(self) -> GateMarketMakerBot:
        config = GateBotConfig.from_file_or_env(self._config_path if self._config_path and self._config_path.exists() else None)
        recorder = BotRecorder(
            config.data_dir,
            config.state_file_name,
            config.events_file_name,
            config.trades_file_name,
        )
        self._recorder = recorder
        bot = GateMarketMakerBot(config, recorder=recorder)
        return bot

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"running": True, "message": "bot already running"}

            self._stop_event = threading.Event()
            self._last_service_error = None

            def target() -> None:
                try:
                    self._bot = self._build_bot()
                    self._bot.run_forever(self._stop_event)
                except Exception as exc:  # noqa: BLE001
                    self._last_service_error = str(exc)
                    LOGGER.exception("Background bot crashed")
                finally:
                    LOGGER.info("Background bot stopped")

            self._thread = threading.Thread(target=target, name="gate-mm-bot", daemon=True)
            self._thread.start()
            return {"running": True, "message": "bot started"}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                return {"running": False, "message": "bot already stopped"}
            assert self._stop_event is not None
            self._stop_event.set()
            thread = self._thread
        thread.join(timeout=10)
        return {"running": False, "message": "stop requested"}

    def run_once(self) -> dict[str, Any]:
        bot = self._build_bot()
        success = bot.run_once()
        self._bot = bot
        return {"ok": success, "status": bot.status_snapshot()}

    def status(self) -> dict[str, Any]:
        running = bool(self._thread and self._thread.is_alive())
        state = self._recorder.read_state() if self._recorder else {}
        bot_status = self._bot.status_snapshot() if self._bot else {}
        return {
            "running": running,
            "last_service_error": self._last_service_error,
            "bot_status": bot_status,
            "persisted_state": state,
        }

    def logs(self, limit: int = 100) -> dict[str, Any]:
        if not self._recorder:
            return {"items": []}
        return {"items": self._recorder.tail_jsonl(self._recorder.events_path, limit=limit)}

    def trades(self, limit: int = 100) -> dict[str, Any]:
        if not self._recorder:
            return {"items": []}
        return {"items": self._recorder.tail_jsonl(self._recorder.trades_path, limit=limit)}


def build_handler(supervisor: BotSupervisor, admin_token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GateMMControl/1.0"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            LOGGER.info("%s - %s", self.address_string(), format % args)

        def _json_response(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _require_auth(self) -> bool:
            if not admin_token:
                return True
            token = self.headers.get("X-Admin-Token", "")
            if token == admin_token:
                return True
            self._json_response({"error": "unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
            return False

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._json_response({"ok": True, "service": "gate-futures-testnet-market-maker"})
                return
            if not self._require_auth():
                return

            query = parse_qs(parsed.query)
            limit = int(query.get("limit", ["100"])[0])
            if parsed.path == "/status":
                self._json_response(supervisor.status())
                return
            if parsed.path == "/logs":
                self._json_response(supervisor.logs(limit=limit))
                return
            if parsed.path == "/trades":
                self._json_response(supervisor.trades(limit=limit))
                return
            self._json_response({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if not self._require_auth():
                return
            parsed = urlparse(self.path)
            if parsed.path == "/start":
                self._json_response(supervisor.start())
                return
            if parsed.path == "/stop":
                self._json_response(supervisor.stop())
                return
            if parsed.path == "/run-once":
                self._json_response(supervisor.run_once())
                return
            self._json_response({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    return Handler


def main() -> int:
    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("HOST", "0.0.0.0")
    admin_token = os.getenv("GATE_MM_ADMIN_TOKEN", "")
    config_path_raw = os.getenv("GATE_MM_CONFIG_PATH", "")
    config_path = Path(config_path_raw) if config_path_raw else None
    auto_start = os.getenv("GATE_MM_AUTO_START", "true").strip().lower() in {"1", "true", "yes", "on"}
    log_level = os.getenv("GATE_MM_SERVICE_LOG_LEVEL", "INFO").upper()

    logging.basicConfig(level=getattr(logging, log_level, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")

    supervisor = BotSupervisor(config_path=config_path)
    if auto_start:
        supervisor.start()

    server = ThreadingHTTPServer((host, port), build_handler(supervisor, admin_token))
    LOGGER.info("Control service listening on %s:%s", host, port)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
