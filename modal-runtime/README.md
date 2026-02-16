# Modal ClickHouse WebSocket Demo

This runtime deploys a websocket endpoint where each connection gets its own container-local `clickhouse-server` process.

`main.py` configures ClickHouse with:
- `<async_load_databases>true</async_load_databases>`
- websocket query execution at `/ws`
- one connection per container via `@modal.concurrent(max_inputs=1)` and `single_use_containers=True`

The async database-load setting is provided by a local file copied into the image:
- `config/async-load-databases.xml` -> `/etc/clickhouse-server/config.d/async-load-databases.xml`

## Deploy

```bash
cd modal-runtime
uv sync
uv run modal deploy main.py
```

## Endpoint shape

Connect to:

`wss://<your-modal-deployment>/ws`

Send either:
- a raw SQL string text frame, or
- JSON: `{"id":"q1","query":"SELECT 1","settings":{"max_threads":1}}`
- JSON batch: `{"id":"batch1","queries":["SELECT 1","SELECT 2"]}`

Response:

```json
{
  "id": "q1",
  "ok": true,
  "query": "SELECT 1",
  "columns": ["1"],
  "rows": [[1]],
  "summary": {}
}
```

Batch response:

```json
{
  "id": "batch1",
  "ok": true,
  "results": [
    {"ok": true, "query": "SELECT 1", "columns": ["1"], "rows": [[1]], "summary": {}},
    {"ok": true, "query": "SELECT 2", "columns": ["2"], "rows": [[2]], "summary": {}}
  ]
}
```

`GET /healthz` returns runtime diagnostics, including:
- `startup_error` (set when ClickHouse failed to start)
- `async_load_databases_config`
- `server_log_path` (currently `/tmp/clickhouse-server.log`)
- `server_err_log_path` (currently `/var/log/clickhouse-server/clickhouse-server.err.log`)

## Example client

`client_example.py` does:
1. connects to websocket runtime
2. sends a first message that creates/connects `uk_price_paid` on `disk(type=web, endpoint=...)`
3. sends a second message with both demo queries in one batch:
`SELECT count()` and `SELECT uniq(date)`
4. prints response and timings

Run:

```bash
cd modal-runtime
MODAL_WS_URL="wss://<your-modal-deployment>/ws" uv run python client_example.py
```

or

```bash
cd modal-runtime
uv run python client_example.py --ws-url "wss://<your-modal-deployment>/ws"
```

If cold start is slow, increase websocket handshake timeout:

```bash
uv run python client_example.py --ws-url "wss://<your-modal-deployment>/ws" --open-timeout 240
```
