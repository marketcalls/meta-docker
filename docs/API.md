# MT5 Bridge API Reference

Three transports over the same core:

- REST + WebSocket: `http://<host>:8001` (`BRIDGE_PORT`)
- gRPC: `<host>:8002` (`BRIDGE_GRPC_PORT`), contract in
  `bridge/proto/mt5bridge.proto`
- Portal (browser console): `http://<host>:8001/portal`

Interactive documentation (Swagger UI at `/docs`, ReDoc at `/redoc`, raw
spec at `/openapi.json`) is disabled by default. Set `BRIDGE_DOCS=1` in
the environment to enable it temporarily during development.

## Authentication

Every endpoint except `GET /health` requires a key. There is no anonymous
mode unless you ask for one.

- REST: `X-API-Key: <key>` header
- WebSocket: same header, or `?api_key=<key>` query parameter
- gRPC: `x-api-key` metadata entry

**Where the first key comes from.** If you start the bridge without
`BRIDGE_API_KEY` and with an empty key store, it mints an admin key and
prints it once to the log:

```
WARNING mt5bridge.keys No API key was configured, so one was generated and saved to /data/api_keys.json.
    Use this key in the X-API-Key header (shown only once):
    mtb_9f3c...
```

In Docker: `docker compose logs mt5 | grep -A2 "X-API-Key"`.

To set your own instead, generate a strong one and put it in
`BRIDGE_API_KEY`. Anything shorter than 24 characters aborts startup.

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**Two scopes.** An admin key can issue and revoke keys through the portal
endpoints. An ordinary key can read data and trade but cannot touch key
material. `BRIDGE_API_KEY` and the bootstrap key are admin; keys created
in the portal are ordinary unless you ask for admin.

Keys are stored as SHA-256 digests, so a lost key cannot be recovered,
only revoked and replaced.

**Failure responses.** `401` for a missing or unknown key. `429` after 10
bad keys from one address within 5 minutes, which blocks that address for
5 minutes. `429` also when the per-client rate limit (600 requests per
minute by default) is exceeded.

**Local development.** `BRIDGE_ALLOW_OPEN=1` disables authentication
entirely. The bridge refuses to start if you combine it with a
non-loopback `BRIDGE_HOST`.

## Conventions

- All timestamps in responses are unix epoch seconds (`time`) or
  milliseconds (`time_msc`), broker server time.
- Endpoints accepting times (`time_from`, `time_to`) take unix seconds or
  ISO-8601 strings (`2026-06-12T00:00:00`).
- Symbols must match `^[A-Za-z0-9][A-Za-z0-9._#&-]{0,31}$`. Anything else
  is rejected by the router with `422` before it reaches the terminal.
- Request bodies are capped at 256 KB (`BRIDGE_MAX_BODY_BYTES`); larger
  ones get `413`. The body is rejected on its `Content-Length` rather
  than read and discarded, so a client sending a very large body may see
  the connection close before it reads the `413`.
- Errors return JSON: `400` invalid input, `401` bad API key, `403`
  read-only mode or disallowed origin, `404` not found, `413` body too
  large, `422` malformed parameter, `429` rate limited or locked out,
  `502` the terminal rejected the call, `503` bridge not connected to a
  terminal yet. Terminal errors carry the MT5 code:
  `{"detail": {"message": "...", "code": -2, "description": "..."}}`
- Every response carries `Content-Security-Policy`, `X-Frame-Options:
  DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`
  and `Cache-Control: no-store`.

---

## Status and session

### GET /health

Liveness probe, the only endpoint that never requires auth.

```json
{"status": "ok", "connected": true, "session": 1, "connected_since": 1781227214.6}
```

`connected: false` means the bridge is up but still attaching to the
terminal (or the terminal is not logged in). Poll until true after startup.

`session` increments every time the bridge attaches to a terminal it was
not attached to a moment ago. It does not move while a connection simply
stays up, so a client that reconnects can compare it with the value it
saw before: unchanged means it was merely idle, higher means the terminal
restarted and there is a hole in the data. `connected_since` is the unix
time that session began, or `null` before the first connection.

The response is served from cached state, so it never makes the terminal
do work and can be polled freely.

The bridge supervises the connection: if the terminal dies it re-attaches
on its own, and `POST /initialize` is only needed when `MT5_INIT_RETRIES`
is set to a finite number and the retries were exhausted. See
[recovering after a restart](#recovering-after-a-terminal-restart).

### POST /initialize

Force a reconnect attempt to the terminal. Returns `{"connected": true}`
or a 502 with the MT5 error.

### GET /terminal

`terminal_info()` passthrough: build, path, data_path, connected flags,
ping, etc.

### GET /account

`account_info()` passthrough.

```json
{
  "login": 5051669250,
  "server": "MetaQuotes-Demo",
  "balance": 100000.0,
  "equity": 100000.0,
  "currency": "USD",
  "leverage": 100,
  "margin_free": 100000.0,
  "trade_allowed": true
}
```

---

## Symbols

### GET /symbols?search=\*EUR\*&limit=500

List symbol names. `search` uses MT5 group syntax: `*` all, `*EUR*`
contains EUR, `!*JPY*` exclude. Maximum 64 characters. `limit` is 1 to
10000, default 500.

```json
{"total": 12, "symbols": ["EURUSD", "EURGBP", "EURJPY"]}
```

### GET /symbols/{symbol}

Full `symbol_info()`: digits, point, spread, volume_min/max/step,
trade_mode, filling modes, session times, margin rates, and so on.
Returns 404 for an unknown symbol, 422 for a malformed one.

### POST /symbols/{symbol}/select?enable=true

Add or remove the symbol from Market Watch. A symbol must be selected
before ticks and rates are available; the rates/ticks/order endpoints
select automatically where needed.

### GET /symbols/{symbol}/tick

Latest tick.

```json
{
  "time": 1781227214,
  "bid": 1.15753,
  "ask": 1.15759,
  "last": 0.0,
  "volume": 0,
  "time_msc": 1781227214244,
  "flags": 1028,
  "volume_real": 0.0
}
```

---

## Market data

### GET /rates/{symbol}

Candles/bars. Three modes depending on parameters:

| Parameters given | MT5 call |
| --- | --- |
| `start_pos` + `count` (default) | `copy_rates_from_pos` |
| `time_from` + `count` | `copy_rates_from` |
| `time_from` + `time_to` | `copy_rates_range` |

Query parameters:

- `timeframe`: M1 M2 M3 M4 M5 M6 M10 M12 M15 M20 M30 H1 H2 H3 H4 H6 H8
  H12 D1 W1 MN1 (default M1)
- `count`: 1 to 100000 (default 100)
- `start_pos`: bars back from the newest bar, 0 = newest (default 0)
- `time_from`, `time_to`: unix seconds or ISO-8601

```
GET /rates/EURUSD?timeframe=M5&count=3
```

```json
{
  "symbol": "EURUSD",
  "timeframe": "M5",
  "count": 3,
  "rates": [
    {"time": 1781226600, "open": 1.1578, "high": 1.1578, "low": 1.15757,
     "close": 1.15758, "tick_volume": 80, "spread": 3, "real_volume": 0}
  ]
}
```

### GET /ticks/{symbol}

Historical ticks.

- `time_from` (required), `time_to` (optional): with both, uses
  `copy_ticks_range`; with only `time_from`, uses `copy_ticks_from` with
  `count`
- `count`: 1 to 1000000 (default 1000)
- `flags`: `all` | `info` (bid/ask changes) | `trade` (last/volume
  changes) (default `all`)

```
GET /ticks/EURUSD?time_from=2026-06-12T00:00:00&count=5000&flags=info
```

```json
{"symbol": "EURUSD", "count": 5000, "ticks": [{"time": 1781222400, "bid": 1.15775, "ask": 1.15793, "time_msc": 1781222400123, "flags": 6}]}
```

---

## Trading

All endpoints in this section return `403` when `BRIDGE_READ_ONLY=1`.

### POST /orders/market

Place a market order. Filling mode is negotiated automatically
(IOC, then FOK, then RETURN) because brokers differ.

Request body. Unknown fields are rejected with `422`:

```json
{
  "symbol": "EURUSD",
  "side": "buy",
  "volume": 0.01,
  "sl": 1.1500,
  "tp": 1.1650,
  "deviation": 20,
  "magic": 0,
  "comment": "my bot"
}
```

| Field | Rule |
| --- | --- |
| `symbol` | required, symbol pattern |
| `side` | required, `buy` or `sell` |
| `volume` | required, greater than 0 |
| `sl`, `tp` | optional prices, not negative |
| `deviation` | max slippage in points, 0 to 100000, default 20 |
| `magic` | EA magic number, default 0 |
| `comment` | at most 31 characters, default `mt5-bridge` |

Successful response (`retcode` 10009 means executed):

```json
{
  "retcode": 10009,
  "deal": 56719498044,
  "order": 57052856011,
  "volume": 0.01,
  "price": 1.15748,
  "comment": "Request executed",
  "request": {"action": 1, "symbol": "EURUSD", "volume": 0.01, "type": 0}
}
```

Common retcodes: 10009 done, 10027 algo trading disabled in terminal,
10019 insufficient funds, 10018 market closed, 10030 unsupported filling.

### POST /positions/{ticket}/close

Close an open position by ticket (full volume, opposite-side deal with
`position` set). Optional query param `deviation` (default 20).
Returns the same trade-result shape as /orders/market; 404 if the ticket
is not an open position.

### POST /orders/send

Raw `order_send()` passthrough for everything else: pending orders,
SL/TP modify, partial close, delete. Body is the documented MT5 trade
request dict with numeric enum values:

```json
{
  "action": 5,
  "symbol": "EURUSD",
  "volume": 0.1,
  "type": 2,
  "price": 1.1500,
  "type_time": 0,
  "type_filling": 2
}
```

Only these fields are accepted, each coerced to the type shown. Anything
else returns `400`:

| Integer | Float | String |
| --- | --- | --- |
| `action`, `magic`, `order`, `deviation`, `type`, `type_filling`, `type_time`, `expiration`, `position`, `position_by` | `volume`, `price`, `stoplimit`, `sl`, `tp` | `symbol`, `comment` |

`action` is required. Key enums: action 1=DEAL 5=PENDING 6=SLTP 7=MODIFY
8=REMOVE; type 0=BUY 1=SELL 2=BUY_LIMIT 3=SELL_LIMIT 4=BUY_STOP
5=SELL_STOP.

### POST /orders/check

Raw `order_check()` passthrough: validates a trade request and returns
expected margin/balance without executing. Same body rules as
/orders/send.

---

## Portfolio and history

### GET /positions?symbol=EURUSD

Open positions, optionally filtered by symbol.

```json
{"positions": [{"ticket": 57052791711, "symbol": "EURUSD", "type": 0,
  "volume": 0.01, "price_open": 1.1575, "price_current": 1.15745,
  "sl": 0.0, "tp": 0.0, "profit": -0.05, "magic": 0, "comment": "..."}]}
```

`type`: 0 = buy, 1 = sell.

### GET /orders?symbol=EURUSD

Pending (working) orders, same filter.

### GET /history/orders?time_from=...&time_to=...

### GET /history/deals?time_from=...&time_to=...

Historical orders and deals in the time window. Both time parameters
required.

---

## WebSockets

Three streams, all requiring the same key as REST:

```
ws://<host>:8001/ws/ticks?symbols=EURUSD,GBPUSD&mode=all&interval_ms=100
ws://<host>:8001/ws/account?interval_ms=500&only_changes=1
ws://<host>:8001/ws/positions?interval_ms=700
```

`/ws/ticks` parameters:

- `symbols` (required): comma-separated, at most 32 per stream
- `mode`: `all` (default) = lossless, every tick is delivered via
  incremental `copy_ticks_from` draining; `latest` = sampled, at most one
  tick per poll per symbol (cheaper, fine for dashboards)
- `interval_ms`: poll interval, minimum 50, default 100, capped at 60000
- `api_key`: the key, unless sent as an `X-API-Key` header

Each message is one tick:

```json
{"symbol": "EURUSD", "time": 1781227246, "bid": 1.15754, "ask": 1.15757,
 "last": 0.0, "volume": 0, "time_msc": 1781227246010, "flags": 6,
 "volume_real": 0.0}
```

`/ws/account` emits account snapshots (`only_changes=1` suppresses
repeats), `/ws/positions` emits `{"positions": [...]}` whenever the list
changes. Both take `interval_ms`, minimum 100, default 500.

Close codes: 4400 missing or malformed symbol list, 4401 bad API key,
rejected origin, or a lockout in force.

**Origin.** A browser WebSocket is accepted only when its `Origin` matches
the host it connects to, or one of `BRIDGE_ALLOWED_ORIGINS`. Non-browser
clients send no `Origin` and are unaffected.

---

## Portal

`GET /portal` serves the browser console: API key management, live
account telemetry (switchable between the WebSocket stream and a gRPC
relay), a lossless tick viewer, and an order ticket with a live positions
table. Set `BRIDGE_PORTAL=0` to stop serving it.

The page loads no third-party script, stylesheet or font; its CSS and JS
come from `/portal/static/`.

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `GET /portal` | none (static page) | the console itself |
| `GET /portal/session` | any key | what the presented key may do |
| `GET /portal/keys` | admin key | list keys, masked |
| `POST /portal/keys` | admin key | create; full key returned once |
| `DELETE /portal/keys/{id}` | admin key | revoke one key by its id |
| `WS /portal/grpc/account` | any key | relays gRPC StreamAccount |
| `WS /portal/grpc/ticks` | any key | relays gRPC StreamTicks |

`POST /portal/keys` body: `{"label": "my-bot", "admin": false}`. The `id`
returned is an opaque handle, not a slice of the key; revocation matches
it exactly, so there is no way to revoke keys in bulk by prefix.

```json
{"key": "mtb_9f3c...", "id": "a1b2c3d4e5f6", "note": "Shown once; store it now."}
```

`GET /portal/session` returns what the console needs to decide what to
show:

```json
{"admin": true, "label": "bootstrap-admin", "auth_required": true, "read_only": false}
```

## gRPC

Target `<host>:8002`. Plaintext by default; set `BRIDGE_GRPC_TLS_CERT`
and `BRIDGE_GRPC_TLS_KEY` to serve TLS directly, or terminate TLS in a
proxy. Binds `127.0.0.1` unless `BRIDGE_GRPC_HOST` (or `BRIDGE_HOST`)
says otherwise. Full contract: `bridge/proto/mt5bridge.proto`. Python
stubs are pre-generated (`bridge/mt5bridge_pb2*.py`); for other languages
run protoc on the proto file.

| RPC | Type | Purpose |
| --- | --- | --- |
| Health | unary | liveness, terminal connection, session counter |
| GetAccount / GetTick / GetRates | unary | snapshots |
| GetPositions / GetPendingOrders | unary | portfolio |
| MarketOrder | unary | market order with filling fallback |
| SendOrder / CheckOrder | unary | raw trade request passthrough |
| ClosePosition | unary | close by ticket |
| StreamTicks | server stream | lossless (`mode: "all"`) or sampled ticks |
| StreamAccount | server stream | balance/equity/margin snapshots |
| StreamPositions | server stream | position list, emitted on change |

Python example (`examples/grpc_client.py` is the full version):

```python
import grpc
import mt5bridge_pb2 as pb
import mt5bridge_pb2_grpc as pb_grpc

channel = grpc.insecure_channel("localhost:8002")
stub = pb_grpc.MT5BridgeStub(channel)
metadata = [("x-api-key", "mtb_...")]

print(stub.GetAccount(pb.Empty(), metadata=metadata))

for tick in stub.StreamTicks(
    pb.StreamTicksRequest(symbols=["EURUSD"], mode="all"), metadata=metadata
):
    print(tick.symbol, tick.bid, tick.ask)

result = stub.MarketOrder(
    pb.MarketOrderRequest(symbol="EURUSD", side="buy", volume=0.01),
    metadata=metadata,
)
print(result.retcode, result.deal)
```

Error mapping: UNAVAILABLE = not connected to terminal, NOT_FOUND =
unknown ticket/symbol, INVALID_ARGUMENT = bad input, UNAUTHENTICATED =
bad key, PERMISSION_DENIED = read-only mode, INTERNAL = terminal rejected
the call (message carries the MT5 error code). Server streams report the
same statuses; a stream that fails ends with the status set rather than
raising UNKNOWN.

## Connection lifetime: keepalive, heartbeats, reconnect

What the bridge manages for you:

- WebSocket: the server sends protocol-level pings every 20s and drops
  peers that do not answer within 20s. Browsers and the Python
  `websockets` library answer pongs automatically (and `websockets`
  pings the server every 20s itself) - no application heartbeat needed.
- gRPC: the server sends HTTP/2 keepalive pings every 30s, allows client
  pings without active calls, and detects dead peers within ~40s.
- Tick backlog guard: if the terminal's tick history is more than
  `BRIDGE_TICK_MAX_BACKFILL_MS` (default 10s) behind the live tick when a
  lossless stream starts or resumes (for example right after a container
  boot), the stream skips the stale backlog instead of replaying hours of
  history.

What your client must manage:

- Reconnect. Neither WebSocket sessions nor gRPC server-streams resume
  after a drop. Wrap consumption in a retry loop:

```python
# WebSocket
import websockets.sync.client, time
while True:
    try:
        with websockets.sync.client.connect(url) as ws:   # pings automatic
            for message in ws:
                handle(message)
    except Exception:
        time.sleep(3)   # then reconnect

# gRPC: channels reconnect automatically, but the *stream call* must be
# re-issued after an UNAVAILABLE error
channel = grpc.insecure_channel("localhost:8002", options=[
    ("grpc.keepalive_time_ms", 30_000),
    ("grpc.keepalive_permit_without_calls", 1),
])
stub = pb_grpc.MT5BridgeStub(channel)
while True:
    try:
        for tick in stub.StreamTicks(request, metadata=metadata):
            handle(tick)
    except grpc.RpcError as exc:
        if exc.code() != grpc.StatusCode.UNAVAILABLE:
            raise
        time.sleep(3)
```

- Gap recovery after reconnect: fetch missed candles/ticks via
  `/rates/{symbol}` or `/ticks/{symbol}` (or GetRates) using your last
  seen `time_msc`, then resume the stream.

The portal does this automatically: all three of its streams reconnect
3s after an unexpected close.

## Recovering after a terminal restart

The terminal can restart underneath a running bridge: it crashes, the
broker logs it out, or the container watchdog revives it. The bridge
supervises that connection rather than assuming the first attach lasts
forever.

What happens, in order:

1. The terminal goes. Open WebSocket streams close with `1011` and the
   reason `Not connected to a MetaTrader 5 terminal`; gRPC streams end
   with `UNAVAILABLE`. Requests return `503`. The bridge never serves
   stale data in place of a live answer.
2. `GET /health` flips to `connected: false` within `MT5_WATCH_SECONDS`
   (default 5).
3. The bridge re-attaches by itself, retrying every
   `MT5_INIT_RETRY_SECONDS`. `POST /initialize` is only needed when
   `MT5_INIT_RETRIES` is a finite number and the retries ran out.
4. On success `session` increments and `connected_since` is stamped.

Your open positions are not affected by any of this. They live on the
broker's server, not in the terminal.

**How a client detects a gap.** Record `session` alongside your data.
After a reconnect, read `/health` again:

```python
before = requests.get(f"{base}/health", headers=headers).json()["session"]

# ... stream, then the stream drops and you reconnect ...

after = requests.get(f"{base}/health", headers=headers).json()["session"]
if after != before:
    # The terminal restarted. Anything between the last tick you saw and
    # now is missing, so backfill before trusting the live stream again.
    backfill(since=last_seen_time_msc)
```

This matters because a resumed lossless tick stream does not replay an
arbitrarily long backlog. If the terminal's tick history is more than
`BRIDGE_TICK_MAX_BACKFILL_MS` (default 10s) behind the live tick when the
stream starts, the stale portion is skipped so a fresh client is not
flooded with hours of history. The skip is logged, but the only signal
your client gets is the changed `session`, so check it.

## Configuration reference

| Variable | Default | Meaning |
| --- | --- | --- |
| `BRIDGE_HOST` / `BRIDGE_PORT` | 127.0.0.1 / 8001 | REST and WebSocket bind address |
| `BRIDGE_GRPC` / `BRIDGE_GRPC_HOST` / `BRIDGE_GRPC_PORT` | 1 / BRIDGE_HOST / 8002 | gRPC toggle and bind address |
| `BRIDGE_GRPC_TLS_CERT` / `BRIDGE_GRPC_TLS_KEY` | unset | PEM pair for direct gRPC TLS |
| `BRIDGE_GRPC_MAX_CONCURRENT` | 64 | concurrent gRPC calls |
| `BRIDGE_API_KEY` | unset | master admin key, minimum 24 characters |
| `BRIDGE_KEYS_FILE` | api_keys.json | key store (digests only) |
| `BRIDGE_ALLOW_OPEN` | unset | 1 disables authentication; loopback only |
| `BRIDGE_READ_ONLY` | unset | 1 rejects every trading endpoint |
| `BRIDGE_PORTAL` | 1 | 0 stops serving the browser console |
| `BRIDGE_DOCS` | unset | 1 enables /docs, /redoc, /openapi.json |
| `BRIDGE_ALLOWED_ORIGINS` | unset | comma-separated browser origins; same origin by default |
| `BRIDGE_TRUSTED_PROXIES` | unset | proxy addresses allowed to set X-Forwarded-For |
| `BRIDGE_RATE_LIMIT` | 600 | requests per minute per client; 0 disables |
| `BRIDGE_AUTH_FAIL_LIMIT` / `BRIDGE_AUTH_FAIL_WINDOW` | 10 / 300 | bad keys before lockout, and its length in seconds |
| `BRIDGE_MAX_BODY_BYTES` | 262144 | largest accepted request body |
| `BRIDGE_TICK_MAX_BACKFILL_MS` | 10000 | stale tick backlog skipped on stream start |
| `MT5_PATH` | unset | terminal64.exe to launch; attach if unset |
| `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` | unset | broker auto-login |
| `MT5_PORTABLE` | 0 | terminal runs in /portable mode |
| `MT5_TIMEOUT_MS` | 60000 | IPC timeout passed to initialize() |
| `MT5_INIT_RETRIES` / `MT5_INIT_RETRY_SECONDS` | 0 / 5 | consecutive attach failures before giving up, and the delay between them; 0 retries forever |
| `MT5_WATCH_SECONDS` | 5 | how often the supervisor checks the terminal is still there |
| `VNC_PASSWORD` | unset | noVNC password, minimum 8 characters (docker) |
| `SCREEN_RESOLUTION` | 1280x800x24 | Xvfb resolution (docker) |
| `BIND_ADDR` | 127.0.0.1 | host interface compose publishes ports on |

## Client examples

Python (stdlib only) and WebSocket clients live in `examples/`:

- `examples/fetch_rates.py` - REST: health, account, candles
- `examples/stream_ticks.py` - WebSocket tick stream
- `examples/grpc_client.py` - gRPC unary calls, streams and a trade
- `examples/measure_ticks.py` - measure broker tick rates (native, uses
  the MetaTrader5 package directly)

Go example:

```go
req, _ := http.NewRequest("GET", "http://localhost:8001/rates/EURUSD?timeframe=M5&count=100", nil)
req.Header.Set("X-API-Key", os.Getenv("BRIDGE_API_KEY"))
resp, _ := http.DefaultClient.Do(req)
// decode JSON into a struct with time/open/high/low/close fields
```

Any language that speaks HTTP and JSON works; generate typed clients from
`/openapi.json` (enable it with `BRIDGE_DOCS=1`) if preferred.
