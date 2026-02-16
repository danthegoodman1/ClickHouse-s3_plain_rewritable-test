"""Modal websocket runtime backed by chdb (in-process ClickHouse)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from datetime import date, datetime, time as datetime_time
from decimal import Decimal
from typing import Any
from uuid import UUID

import modal
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from modal.exception import ClientClosed


@contextlib.contextmanager
def _silence_stderr() -> Any:
    saved_stderr_fd: int | None = None
    devnull_fd: int | None = None
    try:
        saved_stderr_fd = os.dup(2)
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull_fd, 2)
        yield
    finally:
        if saved_stderr_fd is not None:
            os.dup2(saved_stderr_fd, 2)
            os.close(saved_stderr_fd)
        if devnull_fd is not None:
            os.close(devnull_fd)


with _silence_stderr():
    from chdb.session import Session

app = modal.App("clickhouse-ws-demo")
logger = logging.getLogger(__name__)
FUNCTION_REGIONS = ["us-east"]


class _ModalAsyncioShutdownNoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if (
            "an error occurred during closing of asynchronous generator" in message
            and "_ContainerIOManager.get_data_in" in message
        ):
            return False
        if "unhandled exception during asyncio.run() shutdown" not in message:
            return True
        if not record.exc_info:
            return True
        exception = record.exc_info[1]
        if isinstance(exception, ClientClosed):
            return False
        return exception.__class__.__name__ != "ClientClosed"


def _install_asyncio_logger_filter() -> None:
    asyncio_logger = logging.getLogger("asyncio")
    if getattr(asyncio_logger, "_modal_shutdown_noise_filter_installed", False):
        return
    asyncio_logger.addFilter(_ModalAsyncioShutdownNoiseFilter())
    setattr(asyncio_logger, "_modal_shutdown_noise_filter_installed", True)


_install_asyncio_logger_filter()


image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("chdb", "fastapi")
    .run_commands("python -c \"import chdb; chdb.query('SELECT 1')\" >/dev/null 2>&1")
)

_session: Session | None = None
_placement_logged = False


def _should_ignore_asyncio_shutdown_exception(context: dict[str, Any]) -> bool:
    exception = context.get("exception")
    message = str(context.get("message", ""))
    if isinstance(exception, ClientClosed):
        return True
    if "aclose(): asynchronous generator is already running" not in message:
        return False
    task = context.get("task")
    if task is None:
        return False
    task_repr = repr(task)
    return "_ContainerIOManager.get_data_in" in task_repr


def _install_asyncio_exception_filter() -> None:
    loop = asyncio.get_running_loop()
    if getattr(loop, "_modal_shutdown_filter_installed", False):
        return
    previous_handler = loop.get_exception_handler()

    def _handler(current_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        if _should_ignore_asyncio_shutdown_exception(context):
            return
        if previous_handler is not None:
            previous_handler(current_loop, context)
            return
        current_loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)
    setattr(loop, "_modal_shutdown_filter_installed", True)


def _detect_runtime_region() -> str | None:
    for key in (
        "MODAL_REGION",
        "MODAL_RUNTIME_REGION",
        "MODAL_FUNCTION_REGION",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "GCP_REGION",
    ):
        value = os.environ.get(key)
        if value:
            return value
    return None


def _detect_cloud_provider() -> str | None:
    for key in ("MODAL_CLOUD_PROVIDER", "MODAL_CLOUD", "CLOUD_PROVIDER"):
        value = os.environ.get(key)
        if value:
            upper_value = value.upper()
            if "AZURE" in upper_value:
                return "azure"
            if "AWS" in upper_value:
                return "aws"
            if "GCP" in upper_value or "GOOGLE" in upper_value:
                return "gcp"
            return value.lower()
    if os.environ.get("AWS_EXECUTION_ENV") or os.environ.get("AWS_REGION"):
        return "aws"
    if os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("K_SERVICE"):
        return "gcp"
    if os.environ.get("AZURE_REGION"):
        return "azure"
    return None


def _log_modal_placement_once() -> None:
    global _placement_logged
    if _placement_logged:
        return
    placement = {
        "configured_regions": FUNCTION_REGIONS,
        "runtime_region": _detect_runtime_region() or "unknown",
        "cloud_provider": _detect_cloud_provider() or "unknown",
    }
    payload = json.dumps(placement, sort_keys=True)
    print(f"modal_placement {payload}", flush=True)
    _placement_logged = True


def _get_session() -> Session:
    global _session
    if _session is None:
        with _silence_stderr():
            _session = Session()
            _session.query("SELECT 1")
    return _session


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


def _apply_settings(session: Session, settings: dict[str, Any]) -> None:
    for key, value in settings.items():
        if isinstance(value, bool):
            session.query(f"SET {key} = {1 if value else 0}")
        elif isinstance(value, str):
            escaped = value.replace("\\", "\\\\").replace("'", "\\'")
            session.query(f"SET {key} = '{escaped}'")
        else:
            session.query(f"SET {key} = {value}")


def _execute_query(query: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    session = _get_session()

    if settings:
        _apply_settings(session, settings)

    first_keyword = query.lstrip().split(maxsplit=1)[0].upper() if query.strip() else ""

    if first_keyword in READ_ONLY_QUERY_PREFIXES:
        raw = session.query(query, "JSONCompact")
        raw_str = str(raw).strip()
        if not raw_str:
            return {"columns": [], "rows": [], "rows_before_limit": None, "summary": {}}

        result = json.loads(raw_str)
        columns = [col["name"] for col in result.get("meta", [])]
        rows = result.get("data", [])
        statistics = result.get("statistics", {})

        summary: dict[str, Any] = {}
        if statistics:
            if "elapsed" in statistics:
                summary["elapsed_ns"] = str(int(float(statistics["elapsed"]) * 1_000_000_000))
            if "rows_read" in statistics:
                summary["rows_read"] = str(statistics["rows_read"])
            if "bytes_read" in statistics:
                summary["bytes_read"] = str(statistics["bytes_read"])

        return {
            "columns": columns,
            "rows": _to_jsonable(rows),
            "rows_before_limit": result.get("rows_before_limit_at_least"),
            "summary": _to_jsonable(summary),
        }

    session.query(query)
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
        _install_asyncio_exception_filter()
        _log_modal_placement_once()
        _get_session()

    @api.on_event("shutdown")
    async def _shutdown() -> None:
        global _session
        if _session is not None:
            _session.cleanup()
            _session = None

    @api.get("/healthz")
    async def healthz() -> dict[str, Any]:
        try:
            session = _get_session()
            session.query("SELECT 1")
            return {"ok": True, "engine": "chdb"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "engine": "chdb"}

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
