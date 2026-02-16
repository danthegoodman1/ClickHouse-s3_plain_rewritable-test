"""Modal websocket runtime backed by a container-local ClickHouse server."""

from __future__ import annotations

import json
import os
import pwd
import socket
import subprocess
import time
from datetime import date, datetime, time as datetime_time
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import clickhouse_connect
import modal
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = modal.App("clickhouse-ws-demo")

CLICKHOUSE_HTTP_PORT = 8123
CLICKHOUSE_CONFIG_PATH = "/etc/clickhouse-server/config.xml"
CLICKHOUSE_ASYNC_CONFIG_PATH = "/etc/clickhouse-server/config.d/async-load-databases.xml"
LOCAL_ASYNC_CONFIG_PATH = Path(__file__).parent / "config" / "async-load-databases.xml"
CLICKHOUSE_LOG_PATH = Path("/tmp/clickhouse-server.log")
CLICKHOUSE_ERR_LOG_PATH = Path("/var/log/clickhouse-server/clickhouse-server.err.log")
CLICKHOUSE_STARTUP_TIMEOUT_S = 120

image = (
    modal.Image.from_registry("clickhouse/clickhouse-server", add_python="3.12")
    .pip_install("clickhouse-connect", "fastapi")
    .run_commands(
        "mkdir -p /etc/clickhouse-server/config.d /var/lib/clickhouse /var/log/clickhouse-server /var/run/clickhouse-server",
    )
    .add_local_file(
        str(LOCAL_ASYNC_CONFIG_PATH),
        remote_path=CLICKHOUSE_ASYNC_CONFIG_PATH,
    )
)

_clickhouse_process: subprocess.Popen[str] | None = None
_clickhouse_client: clickhouse_connect.driver.client.Client | None = None
_clickhouse_startup_error: str | None = None


def _read_log_tail(lines: int = 80) -> str:
    if not CLICKHOUSE_LOG_PATH.exists():
        return "No ClickHouse log file found."
    content = CLICKHOUSE_LOG_PATH.read_text(encoding="utf-8", errors="replace")
    tail = content.splitlines()[-lines:]
    return "\n".join(tail) if tail else "ClickHouse log file is empty."


def _read_clickhouse_err_log_tail(lines: int = 80) -> str:
    if not CLICKHOUSE_ERR_LOG_PATH.exists():
        return "No ClickHouse error log file found."
    content = CLICKHOUSE_ERR_LOG_PATH.read_text(encoding="utf-8", errors="replace")
    tail = content.splitlines()[-lines:]
    return "\n".join(tail) if tail else "ClickHouse error log file is empty."


def _startup_diagnostics() -> str:
    return (
        "Bootstrap log tail:\n"
        f"{_read_log_tail()}\n\n"
        "ClickHouse error log tail:\n"
        f"{_read_clickhouse_err_log_tail()}"
    )


def _is_tcp_open(host: str, port: int, timeout_s: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _start_clickhouse_server() -> None:
    global _clickhouse_process, _clickhouse_client
    if _clickhouse_client is not None:
        return

    if _clickhouse_process is not None and _clickhouse_process.poll() is not None:
        _clickhouse_process = None

    if _clickhouse_process is None:
        clickhouse_user = pwd.getpwnam("clickhouse")
        log_file = CLICKHOUSE_LOG_PATH.open("a", encoding="utf-8")
        _clickhouse_process = subprocess.Popen(
            [
                "clickhouse-server",
                f"--config-file={CLICKHOUSE_CONFIG_PATH}",
            ],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            user=clickhouse_user.pw_uid,
            group=clickhouse_user.pw_gid,
            env={**os.environ, "CLICKHOUSE_WATCHDOG_ENABLE": "0"},
        )
        log_file.close()

    deadline = time.monotonic() + CLICKHOUSE_STARTUP_TIMEOUT_S
    last_error = "unknown startup error"
    while time.monotonic() < deadline:
        if _clickhouse_process.poll() is not None:
            raise RuntimeError(
                "ClickHouse exited during startup.\n"
                f"Exit code: {_clickhouse_process.returncode}\n"
                f"{_startup_diagnostics()}"
            )
        try:
            if not _is_tcp_open("127.0.0.1", CLICKHOUSE_HTTP_PORT, timeout_s=0.5):
                time.sleep(0.5)
                continue
            client = clickhouse_connect.get_client(
                host="localhost",
                port=CLICKHOUSE_HTTP_PORT,
                connect_timeout=1,
                send_receive_timeout=5,
            )
            client.query("SELECT 1")
            _clickhouse_client = client
            return
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            time.sleep(0.5)

    raise TimeoutError(
        f"Timed out waiting for ClickHouse HTTP on localhost:{CLICKHOUSE_HTTP_PORT}. "
        f"Last client error: {last_error}\n"
        f"{_startup_diagnostics()}"
    )


def _ensure_clickhouse_server() -> None:
    global _clickhouse_startup_error
    if _clickhouse_client is not None:
        return
    try:
        _start_clickhouse_server()
        if _clickhouse_client is None:
            raise RuntimeError("ClickHouse startup did not initialize an HTTP client")
        _clickhouse_startup_error = None
    except Exception as exc:  # noqa: BLE001
        _clickhouse_startup_error = str(exc)
        raise


def _stop_clickhouse_server() -> None:
    global _clickhouse_process, _clickhouse_client
    _clickhouse_client = None
    if _clickhouse_process is None:
        return
    if _clickhouse_process.poll() is None:
        _clickhouse_process.terminate()
        try:
            _clickhouse_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _clickhouse_process.kill()
            _clickhouse_process.wait(timeout=5)
    _clickhouse_process = None


READ_ONLY_QUERY_PREFIXES = ("SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXISTS")


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime, datetime_time)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(v) for v in value]
    return str(value)


def _execute_query(query: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    _ensure_clickhouse_server()
    if _clickhouse_client is None:
        raise RuntimeError("ClickHouse client is not initialized")

    first_keyword = query.lstrip().split(maxsplit=1)[0].upper() if query.strip() else ""

    if first_keyword in READ_ONLY_QUERY_PREFIXES:
        result = _clickhouse_client.query(query, settings=settings)
        return {
            "columns": result.column_names,
            "rows": _to_jsonable(result.result_rows),
            "rows_before_limit": _to_jsonable(result.summary.get("rows_before_limit_at_least")),
            "summary": _to_jsonable(result.summary),
        }

    _clickhouse_client.command(query, settings=settings)
    return {"columns": [], "rows": [], "summary": {}}


def _parse_query_items(
    raw_message: str,
) -> tuple[Any, list[tuple[str, dict[str, Any] | None]], bool, str | None]:
    request_id: Any = None
    default_settings: dict[str, Any] | None = None
    queries_payload: list[Any] | None = None
    was_batch = False

    try:
        payload = json.loads(raw_message)
    except json.JSONDecodeError:
        payload = raw_message

    if isinstance(payload, dict):
        request_id = payload.get("id")
        settings_value = payload.get("settings")
        if isinstance(settings_value, dict):
            default_settings = settings_value

        if isinstance(payload.get("queries"), list):
            queries_payload = payload["queries"]
            was_batch = True
        elif isinstance(payload.get("query"), list):
            queries_payload = payload["query"]
            was_batch = True
        elif isinstance(payload.get("query"), str):
            queries_payload = [payload["query"]]
    elif isinstance(payload, str):
        queries_payload = [payload]

    if not queries_payload:
        return request_id, [], was_batch, "Message must include a non-empty query payload"

    query_items: list[tuple[str, dict[str, Any] | None]] = []
    for raw_item in queries_payload:
        query_settings = default_settings
        if isinstance(raw_item, dict):
            query_text = raw_item.get("query")
            raw_query_settings = raw_item.get("settings")
            if isinstance(raw_query_settings, dict):
                query_settings = raw_query_settings
        else:
            query_text = raw_item

        if not isinstance(query_text, str) or not query_text.strip():
            return request_id, [], was_batch, "Each query must be a non-empty string"
        query_items.append((query_text, query_settings))

    return request_id, query_items, was_batch, None


def _make_asgi_app() -> FastAPI:
    api = FastAPI(title="ClickHouse WebSocket Runtime")

    @api.on_event("startup")
    async def _startup() -> None:
        try:
            _ensure_clickhouse_server()
        except Exception:
            # Keep ASGI app alive so diagnostics can be queried via /healthz or websocket.
            pass

    @api.on_event("shutdown")
    async def _shutdown() -> None:
        _stop_clickhouse_server()

    @api.get("/healthz")
    async def healthz() -> dict[str, Any]:
        try:
            _ensure_clickhouse_server()
            if _clickhouse_client is None:
                raise RuntimeError("ClickHouse client is not initialized after startup")
            _clickhouse_client.query("SELECT 1")
            return {
                "ok": True,
                "startup_error": None,
                "async_load_databases_config": CLICKHOUSE_ASYNC_CONFIG_PATH,
                "server_log_path": str(CLICKHOUSE_LOG_PATH),
                "server_err_log_path": str(CLICKHOUSE_ERR_LOG_PATH),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "startup_error": str(exc),
                "async_load_databases_config": CLICKHOUSE_ASYNC_CONFIG_PATH,
                "server_log_path": str(CLICKHOUSE_LOG_PATH),
                "server_err_log_path": str(CLICKHOUSE_ERR_LOG_PATH),
            }

    @api.websocket("/ws")
    async def websocket_query_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                raw_message = await websocket.receive_text()
                request_id, query_items, was_batch, parse_error = _parse_query_items(raw_message)

                if parse_error:
                    await websocket.send_json(
                        {"id": request_id, "ok": False, "error": parse_error}
                    )
                    continue

                if len(query_items) == 1 and not was_batch:
                    query_text, query_settings = query_items[0]
                    try:
                        result = _execute_query(query_text, settings=query_settings)
                        await websocket.send_json(
                            {"id": request_id, "ok": True, "query": query_text, **result}
                        )
                    except Exception as exc:  # noqa: BLE001
                        await websocket.send_json(
                            {
                                "id": request_id,
                                "ok": False,
                                "query": query_text,
                                "error": str(exc),
                            }
                        )
                    continue

                result_items: list[dict[str, Any]] = []
                all_ok = True
                for query_text, query_settings in query_items:
                    try:
                        result = _execute_query(query_text, settings=query_settings)
                        result_items.append({"ok": True, "query": query_text, **result})
                    except Exception as exc:  # noqa: BLE001
                        all_ok = False
                        result_items.append(
                            {"ok": False, "query": query_text, "error": str(exc)}
                        )

                await websocket.send_json(
                    {
                        "id": request_id,
                        "ok": all_ok,
                        "results": result_items,
                    }
                )
        except WebSocketDisconnect:
            return

    return api


@app.function(
    image=image,
    cpu=8,
    memory=8192 * 4,
    # min_containers=1,
    # buffer_containers=1,
    single_use_containers=True,
    startup_timeout=5 * 60,
    timeout=60 * 60,
    region=["us-east"],
)
@modal.concurrent(max_inputs=1)
@modal.asgi_app()
def ws_runtime() -> FastAPI:
    return _make_asgi_app()
