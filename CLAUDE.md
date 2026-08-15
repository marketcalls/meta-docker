# meta-docker

Headless MetaTrader 5 behind a REST, WebSocket and gRPC API. The terminal
runs under Wine in a container (or natively on Windows) and the bridge
exposes it over HTTP.

## Writing style

No icons or emoji anywhere: not in code, log messages, comments, commit
messages, README files, documentation or terminal output.

No em dashes or en dashes anywhere. Use a comma, a colon, parentheses or
a full stop instead. A plain hyphen in a compound word such as read-only
is fine.

## Layout

```
bridge/
  app.py          FastAPI app: REST, WebSocket, portal endpoints, middleware
  core.py         framework-neutral MT5 wrapper, shared by REST and gRPC
  grpc_server.py  gRPC service over the same core
  keys.py         API key store, hashed at rest, admin and standard scopes
  security.py     rate limits, lockouts, origin checks, response headers
  portal/         browser console: index.html, app.js, style.css
  proto/          gRPC contract; mt5bridge_pb2*.py are generated from it
docker/           Dockerfile, compose.yaml, start.sh, mt5cfg.ini, .env.example
docs/API.md       full API reference
examples/         client scripts for REST, WebSocket and gRPC
```

## Rules that are easy to get wrong

**Every mt5 call goes through `core.call()`.** The terminal serialises
IPC and the MetaTrader5 C extension rejects argument-unpacking wrappers,
so each call must be written out inside the closure:
`call(lambda: mt5.order_send(request))`. Never nest one `call()` inside
another; `_mt5_lock` is not reentrant.

**This process can move real money.** Authentication is on by default and
must stay that way. New endpoints go on the `api` router (any key) or the
`trading` router (any key, plus the `BRIDGE_READ_ONLY` kill switch), never
on `app` directly unless they are genuinely public like `/health`.

**Anything a caller sends that reaches the terminal must be validated.**
Symbols go through `core.validate_symbol`, raw trade requests through
`core.validate_trade_request`, which allows only the documented fields.

**The portal never assigns `innerHTML`.** Key labels, symbols and order
comments are attacker-influenced. Build elements and use `textContent`.
The page loads no third-party asset so the CSP can stay `default-src
'self'`.

**Bind loopback by default.** `BRIDGE_HOST` defaults to `127.0.0.1` and
compose publishes on `127.0.0.1`. Exposing the bridge is a deliberate act
by the operator, never a default.

**WebSocket rejections call `ws_reject`**, which accepts the handshake
before closing. Closing before `accept()` makes the ASGI server return a
bare HTTP 403 and the close code never reaches the client.

## Changing the API

`docs/API.md` is the contract. When an endpoint, parameter bound, status
code or environment variable changes, update the reference in the same
commit. The doc claims exact bounds and status codes, so a change that
does not land there becomes a lie.

## Regenerating the gRPC stubs

```bash
uv run python -m grpc_tools.protoc -I bridge/proto \
  --python_out=bridge --grpc_python_out=bridge bridge/proto/mt5bridge.proto
```

## Running it

```powershell
uv sync
$env:MT5_PATH = "C:\Program Files\MetaTrader 5\terminal64.exe"
uv run python bridge/app.py
```

The bridge prints a generated admin key on first run when no
`BRIDGE_API_KEY` is set.
