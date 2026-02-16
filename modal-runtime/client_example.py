"""Example websocket client for the Modal ClickHouse runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from typing import Any

import websockets

WEB_TABLE_ENDPOINT = (
    "https://raw.githubusercontent.com/ClickHouse/web-tables-demo/table_disk/uk_price_paid/"
)

CONNECT_TABLE_QUERY = """
CREATE TABLE IF NOT EXISTS uk_price_paid
(
    price UInt32,
    date Date,
    postcode1 LowCardinality(String),
    postcode2 LowCardinality(String),
    type Enum8('other' = 0, 'terraced' = 1, 'semi-detached' = 2, 'detached' = 3, 'flat' = 4),
    is_new UInt8,
    duration Enum8('unknown' = 0, 'freehold' = 1, 'leasehold' = 2),
    addr1 String,
    addr2 String,
    street LowCardinality(String),
    locality LowCardinality(String),
    town LowCardinality(String),
    district LowCardinality(String),
    county LowCardinality(String)
)
ENGINE = MergeTree
ORDER BY (postcode1, postcode2, addr1, addr2)
SETTINGS
    table_disk = 1,
    disk = disk(type = web, endpoint = '{endpoint}')
""".format(endpoint=WEB_TABLE_ENDPOINT)

COUNT_QUERY = "SELECT count() AS row_count FROM uk_price_paid"
UNIQ_DATES_QUERY = "SELECT uniq(date) AS uniq_dates FROM uk_price_paid"


async def _send_payload(
    websocket: Any,
    payload: dict[str, Any],
    request_label: str,
) -> tuple[dict[str, Any], float]:
    start = time.perf_counter()
    await websocket.send(json.dumps(payload))
    response_raw = await websocket.recv()
    elapsed_s = time.perf_counter() - start
    response = json.loads(response_raw)
    if not response.get("ok"):
        raise RuntimeError(
            f"Request {request_label} failed: {response.get('error', response)}"
        )
    return response, elapsed_s


def _elapsed_server_ms(response: dict[str, Any]) -> float | None:
    summary = response.get("summary")
    if not isinstance(summary, dict):
        return None
    elapsed_ns = summary.get("elapsed_ns")
    if elapsed_ns is None:
        return None
    try:
        return float(elapsed_ns) / 1_000_000.0
    except (TypeError, ValueError):
        return None


def _print_single_result_timing(
    name: str,
    response: dict[str, Any],
    roundtrip_s: float | None = None,
) -> None:
    server_ms = _elapsed_server_ms(response)
    timing_parts = []
    if roundtrip_s is not None:
        timing_parts.append(f"{name}_roundtrip_ms={roundtrip_s * 1000:.1f}")
    timing_parts.append(
        f"{name}_server_ms={(f'{server_ms:.1f}' if server_ms is not None else 'n/a')}"
    )
    print("TIMING " + " ".join(timing_parts))


async def run(ws_url: str, open_timeout: float) -> None:
    total_start = time.perf_counter()
    connect_start = time.perf_counter()
    async with websockets.connect(
        ws_url,
        max_size=10_000_000,
        open_timeout=open_timeout,
    ) as websocket:
        connect_elapsed_ms = (time.perf_counter() - connect_start) * 1000
        print(f"TIMING connect_ms={connect_elapsed_ms:.1f}")

        connect_table_payload = {"id": "connect-table", "query": CONNECT_TABLE_QUERY}
        connect_table_resp, connect_table_elapsed_s = await _send_payload(
            websocket,
            connect_table_payload,
            request_label="connect-table",
        )
        print("CONNECT TABLE response:")
        print(json.dumps(connect_table_resp, indent=2))
        _print_single_result_timing(
            "connect_table",
            connect_table_resp,
            connect_table_elapsed_s,
        )

        batch_payload = {
            "id": "demo-queries",
            "queries": [COUNT_QUERY, UNIQ_DATES_QUERY],
        }
        batch_resp, batch_elapsed_s = await _send_payload(
            websocket,
            batch_payload,
            request_label="demo-queries",
        )
        print("BATCH response:")
        print(json.dumps(batch_resp, indent=2))

        results = batch_resp.get("results") or []
        print(f"TIMING demo_queries_batch_roundtrip_ms={batch_elapsed_s * 1000:.1f}")
        for idx, result in enumerate(results):
            query_name = "count" if idx == 0 else "uniq_dates" if idx == 1 else f"query_{idx + 1}"
            _print_single_result_timing(query_name, result)

        if results and isinstance(results[0], dict):
            count_rows = results[0].get("rows") or []
            if count_rows and count_rows[0]:
                print(f"row_count={count_rows[0][0]}")
        if len(results) > 1 and isinstance(results[1], dict):
            uniq_rows = results[1].get("rows") or []
            if uniq_rows and uniq_rows[0]:
                print(f"uniq_dates={uniq_rows[0][0]}")

    total_elapsed_ms = (time.perf_counter() - total_start) * 1000
    print(f"TIMING total_ms={total_elapsed_ms:.1f}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ws-url",
        default=os.environ.get(
            "MODAL_WS_URL",
            "wss://tangia--clickhouse-ws-demo-ws-runtime-dev.modal.run/ws",
        ),
        help="WebSocket URL for the deployed Modal app (must end with /ws).",
    )
    parser.add_argument(
        "--open-timeout",
        type=float,
        default=120,
        help="WebSocket handshake timeout in seconds.",
    )
    args = parser.parse_args()
    asyncio.run(run(args.ws_url, open_timeout=args.open_timeout))


if __name__ == "__main__":
    main()
