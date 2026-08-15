<div align="center">

# meta-docker

### Headless MetaTrader 5 with a REST, WebSocket and gRPC API

**Run MT5 on a server. Trade it from any language.**

[![Python](https://img.shields.io/badge/PYTHON-3.10+-3776AB?style=for-the-badge)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/DOCKER-READY-2496ED?style=for-the-badge)](https://www.docker.com/)
[![Platforms](https://img.shields.io/badge/RUNS%20ON-WIN%20%7C%20LINUX%20%7C%20MACOS-4caf50?style=for-the-badge)](#install)
[![Version](https://img.shields.io/badge/VERSION-1.0.0-brightgreen?style=for-the-badge)](https://github.com/marketcalls/meta-docker/releases)
[![License](https://img.shields.io/badge/LICENSE-MIT-387ed1?style=for-the-badge)](LICENSE)

[![REST](https://img.shields.io/badge/REST-8001-387ed1?style=flat-square)](docs/API.md)
[![WebSocket](https://img.shields.io/badge/WEBSOCKET-live%20ticks-387ed1?style=flat-square)](docs/API.md#websockets)
[![gRPC](https://img.shields.io/badge/GRPC-8002-387ed1?style=flat-square)](docs/API.md#grpc)
[![Security](https://img.shields.io/badge/SECURITY-hardened-4caf50?style=flat-square)](SECURITY.md)

**Developer: [Marketcalls](https://www.marketcalls.in) (Rajandran R)**

</div>

---

## The problem this solves

MetaTrader 5 is a Windows desktop program. The official Python API only
talks to a terminal running on the same machine, over a private Windows
IPC channel. That leaves most traders stuck in the same corner:

| What you want | What MT5 gives you |
| --- | --- |
| Run your bot on a cheap Linux VPS | MT5 needs Windows |
| Write your strategy in Go, Node, Rust, C# | Official API is Python-only, Windows-only |
| Backtest on a Mac | No native Apple Silicon build |
| One data feed, several strategies | One script owns the terminal |
| Live prices in a dashboard | Polling `copy_ticks_from` yourself |

**meta-docker puts an HTTP API in front of the terminal.** The terminal
runs headless in a container, and everything else you write talks to it
over plain HTTP, WebSocket or gRPC, from any machine and any language.

```
Your strategy (Python / Go / Node / Rust / anything)
        |  HTTP, WebSocket, gRPC
        v
   meta-docker bridge  --IPC-->  MetaTrader 5 terminal  -->  Your broker
```

## Who this is for

- **The algo trader on a Mac.** MT5 has no Apple Silicon build. Run the
  container and keep your notebooks where they are.
- **The trader moving off a Windows VPS.** Run the same setup on a Linux
  box for a fraction of the cost, or on the Windows machine you already
  have, natively and with no Docker at all.
- **The developer who does not want to write Python.** Candles, ticks,
  positions and orders are JSON over HTTP. Any language that can call an
  API can trade.
- **Anyone building a dashboard.** Subscribe to `/ws/ticks` and get every
  tick pushed to you, lossless, instead of polling.
- **Anyone running several strategies.** They all talk to one bridge,
  which serialises access to the terminal so they do not trip over each
  other.

Not for you if you want a broker-neutral platform. This drives your MT5
terminal and your broker account. It adds an API, it does not replace
MetaTrader.

## What you get

- **Market data.** Symbols, quotes, candles on 21 timeframes and tick
  history, all as JSON.
- **Trading.** Market orders with automatic filling-mode fallback,
  pending orders, SL/TP modify, partial and full close, plus a raw
  `order_send` passthrough for anything exotic.
- **Live streams.** Ticks, account telemetry and positions over WebSocket
  or gRPC. Tick streaming is lossless, not sampled.
- **Portfolio.** Open positions, working orders, order and deal history.
- **Browser console.** Account, live prices, positions and an order
  ticket at `http://localhost:8001/portal`.
- **Safe defaults.** Authentication on by default, keys hashed at rest,
  loopback binding, rate limits and a read-only switch. See
  [SECURITY.md](SECURITY.md).
- **One container.** Wine, the terminal, a virtual display and the
  bridge, with a browser VNC desktop for the one-time broker login.

## Architecture

```
Windows (native, fastest)
  terminal64.exe <-- IPC --> bridge/app.py <-- HTTP/WS/gRPC --> your bots

Linux / macOS (Docker)
  container (debian + Wine, the official MetaQuotes Linux method)
    terminal64.exe          MT5 terminal, /portable mode
    Windows Python in Wine  official MetaTrader5 package + the bridge
    Xvfb                    virtual display, makes it headless
    x11vnc + noVNC :6080    browser desktop for the one-time broker login
  bridge on :8001 and :8002 <-- HTTP/WS/gRPC --> your bots (run anywhere)
```

The MetaTrader5 IPC protocol is proprietary, so the official package has
to run on Windows Python. On Linux that means inside Wine, which is the
same approach as the official MetaQuotes Linux installation script.

## Install

### Linux (Docker)

Runs natively on x86_64, no emulation.

```bash
git clone https://github.com/marketcalls/meta-docker
cd meta-docker/docker
cp .env.example .env      # fill in broker login, or log in by hand later
docker compose up --build -d
```

First boot installs MT5 into a persistent volume, which takes a few
minutes. Then either:

- **Auto-login.** Put `MT5_LOGIN`, `MT5_PASSWORD` and `MT5_SERVER` in
  `docker/.env` before starting. `MT5_SERVER` must be the exact server
  name, for example `MetaQuotes-Demo`. A name the terminal cannot resolve
  is silently ignored.
- **Manual login.** Open <http://localhost:6080/vnc.html>, then
  File, Open an Account. The login persists in the volume.

Under Wine the Python IPC connection only succeeds once the terminal is
logged in, so `/health` reports `connected: false` until that is done.

Grab the API key the bridge generated on first boot:

```bash
docker compose logs mt5 | grep -A2 "X-API-Key"
curl -H "X-API-Key: mtb_..." http://localhost:8001/health
```

### Windows (native, lowest latency)

MT5 installed normally, bridge runs next to it. No Docker, no Wine.

```powershell
uv sync
$env:MT5_PATH = "C:\Program Files\MetaTrader 5\terminal64.exe"
# Optional auto-login; otherwise the bridge attaches to a logged-in terminal
# $env:MT5_LOGIN = "12345678"; $env:MT5_PASSWORD = "..."; $env:MT5_SERVER = "YourBroker-Demo"
uv run python bridge/app.py
```

The bridge prints an API key on first run. Open
<http://localhost:8001/portal> and paste it in.

### macOS (Apple Silicon)

The same container under QEMU emulation:

```bash
brew install colima docker qemu
colima start --arch x86_64 --vm-type=qemu --cpu 4 --memory 8
docker context use colima
cd docker && docker compose up --build -d
```

Emulation adds latency. Fine for development, research and backtesting.
Use a real Windows or Linux host for live trading.

## First calls

```bash
export KEY=mtb_your_key_here

# Is the terminal up and logged in?
curl -H "X-API-Key: $KEY" http://localhost:8001/health

# Account
curl -H "X-API-Key: $KEY" http://localhost:8001/account

# Last 100 five-minute candles
curl -H "X-API-Key: $KEY" \
  "http://localhost:8001/rates/EURUSD?timeframe=M5&count=100"

# Buy 0.01 lots at market
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"symbol":"EURUSD","side":"buy","volume":0.01}' \
  http://localhost:8001/orders/market
```

Live ticks, pushed rather than polled:

```python
from websockets.sync.client import connect

url = "ws://localhost:8001/ws/ticks?symbols=EURUSD,XAUUSD&api_key=mtb_..."
with connect(url) as ws:
    for message in ws:
        print(message)
```

Ready-made scripts are in [`examples/`](examples): `fetch_rates.py`
(REST), `stream_ticks.py` (WebSocket), `grpc_client.py` (gRPC) and
`measure_ticks.py` (how fast your broker actually ticks).

## The console

`http://localhost:8001/portal` is a single page served by the bridge:
account balance and equity updating live, a lossless price stream you can
switch between WebSocket and gRPC, your open positions with a close
button, an order ticket, and API key management.

It loads no third-party script, stylesheet or font, and it never renders
server data as markup. Paste an admin key into the Key box to sign in.

## API at a glance

Full reference with request and response examples:
**[docs/API.md](docs/API.md)**.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | liveness, the only endpoint without auth |
| `GET` | `/account` | balance, equity, margin, leverage |
| `GET` | `/terminal` | terminal build and state |
| `GET` | `/symbols?search=*EUR*` | symbol names |
| `GET` | `/symbols/{symbol}` | contract specification |
| `GET` | `/symbols/{symbol}/tick` | latest tick |
| `GET` | `/rates/{symbol}?timeframe=M5&count=100` | candles |
| `GET` | `/ticks/{symbol}?time_from=...` | tick history |
| `GET` | `/positions`, `/orders` | open positions, working orders |
| `GET` | `/history/orders`, `/history/deals` | trade history |
| `POST` | `/orders/market` | market order, filling fallback |
| `POST` | `/orders/send`, `/orders/check` | raw trade request |
| `POST` | `/positions/{ticket}/close` | close a position |
| `WS` | `/ws/ticks?symbols=EURUSD,GBPUSD` | live tick stream |
| `WS` | `/ws/account`, `/ws/positions` | live account and positions |

Times are unix seconds or ISO-8601. gRPC on port 8002 exposes the same
operations plus server-streaming RPCs. The contract is in
[`bridge/proto/mt5bridge.proto`](bridge/proto/mt5bridge.proto).

Interactive Swagger docs are off by default. Set `BRIDGE_DOCS=1` to turn
them on while developing.

## Security

This API can place trades on your account. It ships locked down:
authentication is on by default, keys are stored hashed, the server binds
to `127.0.0.1`, and Docker publishes its ports on `127.0.0.1` too.

**Never expose port 8001 directly to the internet.** The bridge speaks
plain HTTP, so the key travels in a readable header. Put it behind a
TLS-terminating reverse proxy, or keep it on a private network and reach
it over WireGuard, Tailscale or an SSH tunnel.

Start with `BRIDGE_READ_ONLY=1` and a demo account. Read
**[SECURITY.md](SECURITY.md)** before you expose anything.

## Configuration

Everything is environment driven. The full table is in
[docs/API.md](docs/API.md#configuration-reference). The ones that matter
most:

| Variable | Default | Meaning |
| --- | --- | --- |
| `BRIDGE_API_KEY` | unset | master admin key, minimum 24 characters; one is generated if you leave it unset |
| `BRIDGE_READ_ONLY` | unset | `1` serves data and rejects every order |
| `BRIDGE_HOST` / `BRIDGE_PORT` | 127.0.0.1 / 8001 | REST and WebSocket bind address |
| `BRIDGE_GRPC_PORT` | 8002 | gRPC port |
| `BRIDGE_ALLOWED_ORIGINS` | unset | browser origins allowed to open WebSockets |
| `BRIDGE_RATE_LIMIT` | 600 | requests per minute per client |
| `BIND_ADDR` | 127.0.0.1 | host interface Docker publishes on |
| `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` | unset | broker auto-login |
| `VNC_PASSWORD` | unset | noVNC password; without it the desktop stays on loopback |

## Notes

- The terminal serialises IPC requests, so the bridge holds a lock around
  every call. One bridge maps to one terminal and one account. Run
  another container for another account.
- Logs live in the `mt5_logs` volume: `terminal.log` and `bridge.log`.
- Algo trading must be enabled in the terminal or orders come back with
  retcode 10027.
- Trading involves risk. Test on a demo account. This software is
  provided as-is and is not investment advice.

## License

MIT. See [LICENSE](LICENSE).
