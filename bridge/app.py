"""HTTP and WebSocket bridge exposing the official MetaTrader5 Python API.

Runs in two places with the same code:
- Natively on Windows, next to an installed MetaTrader 5 terminal.
- Inside Wine in the Linux Docker image (see docker/), where start.sh
  launches the terminal first and this server attaches to it.

A gRPC server (grpc_server.py) runs in the same process; see
bridge/proto/mt5bridge.proto.

This process can move real money, so it authenticates every call except
the liveness probe, refuses to start with a weak master key, rate limits
callers, and binds to the loopback interface unless told otherwise.

Configuration is environment driven:
  BRIDGE_HOST / BRIDGE_PORT   bind address (default 127.0.0.1:8001)
  BRIDGE_GRPC / BRIDGE_GRPC_PORT   gRPC server toggle and port (default on, 8002)
  BRIDGE_API_KEY              optional master admin key, minimum 24 characters
  BRIDGE_ALLOW_OPEN           set to 1 to disable authentication (local only)
  BRIDGE_READ_ONLY            set to 1 to reject every trading endpoint
  BRIDGE_PORTAL               set to 0 to stop serving the browser console
  BRIDGE_DOCS                 set to 1 to enable /docs, /redoc, /openapi.json
  BRIDGE_ALLOWED_ORIGINS      browser origins allowed to open WebSockets
  MT5_PATH                    path to terminal64.exe (optional, attach otherwise)
  MT5_LOGIN / MT5_PASSWORD / MT5_SERVER   broker credentials for auto-login
  MT5_PORTABLE                set to 1 when the terminal runs in /portable mode
  MT5_INIT_RETRIES / MT5_INIT_RETRY_SECONDS   startup attach retry policy
"""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    HTTPException,
    Path,
    Query,
    Request,
    Security,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security.api_key import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import core
import keys
import security

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("mt5bridge")


_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

_UNAUTHORIZED = "Missing or invalid X-API-Key"

# Path parameters reach the terminal directly, so they are constrained to
# the shapes MetaTrader itself uses, rejected by the router before any
# handler runs
SYMBOL_PATH = Path(min_length=1, max_length=32, pattern=core.SYMBOL_PATTERN)
TICKET_PATH = Path(ge=1, le=2**63 - 1)


def _peer(request: Request):
    return security.client_ip(
        (request.client.host, request.client.port) if request.client else None,
        request.headers,
    )


def require_api_key(request: Request, api_key: str = Security(_api_key_header)):
    """Reject anonymous or unknown callers, and slow down guessers."""
    ip = _peer(request)
    if security.is_locked_out(ip):
        raise HTTPException(status_code=429, detail="Too many failed attempts")
    if keys.auth_disabled():
        return
    if keys.verify(api_key) is None:
        security.record_auth_failure(ip)
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED)
    security.record_auth_success(ip)


def require_admin(request: Request, api_key: str = Security(_api_key_header)):
    """Key management is a privileged operation, not a public one."""
    ip = _peer(request)
    if security.is_locked_out(ip):
        raise HTTPException(status_code=429, detail="Too many failed attempts")
    if keys.auth_disabled():
        return
    if not keys.is_admin(api_key):
        security.record_auth_failure(ip)
        raise HTTPException(status_code=401, detail="Admin X-API-Key required")
    security.record_auth_success(ip)


def require_trading(_=Depends(require_api_key)):
    """Trading endpoints additionally honour the read-only kill switch."""
    if security.read_only():
        raise HTTPException(
            status_code=403, detail="Bridge is running read-only (BRIDGE_READ_ONLY)"
        )


async def ws_reject(websocket: WebSocket, code: int):
    """Close a WebSocket with a code the client can actually read.

    Closing before accept() makes the ASGI server reject the handshake
    with a bare HTTP 403 and the close code never reaches the client, so
    a caller cannot tell a bad key from a bad symbol list. Completing the
    handshake first costs one round trip and no data is ever sent.
    """
    await websocket.accept()
    await websocket.close(code=code)


def ws_authorized(websocket: WebSocket):
    """Authenticate a WebSocket and refuse hostile browser origins.

    A page on another site can open a WebSocket to any host the visitor
    can reach, so the Origin check is what keeps a bridge on someone's
    laptop from being driven by a web page they happened to visit.
    """
    ip = security.client_ip(
        (websocket.client.host, websocket.client.port) if websocket.client else None,
        websocket.headers,
    )
    if security.is_locked_out(ip):
        return False
    if not security.origin_allowed(
        websocket.headers.get("origin"), websocket.headers.get("host")
    ):
        logger.warning("Rejected WebSocket from origin %s", websocket.headers.get("origin"))
        return False
    if keys.auth_disabled():
        return True
    provided = websocket.headers.get("x-api-key") or websocket.query_params.get("api_key")
    if keys.verify(provided) is None:
        security.record_auth_failure(ip)
        return False
    security.record_auth_success(ip)
    return True


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Connect in the background so the server binds and serves /health
    # immediately, even while the terminal is still starting up
    connect_task = asyncio.create_task(core.connect_loop())
    grpc_server = None
    if os.getenv("BRIDGE_GRPC", "1").strip().lower() in {"1", "true", "yes"}:
        from grpc_server import create_grpc_server

        grpc_server = await create_grpc_server()
    yield
    if grpc_server is not None:
        await grpc_server.stop(2)
    connect_task.cancel()
    core.shutdown()


# Documentation UIs are disabled unless BRIDGE_DOCS is set; the reference
# lives in docs/API.md
_docs_enabled = os.getenv("BRIDGE_DOCS", "").strip().lower() in {"1", "true", "yes"}
_portal_enabled = os.getenv("BRIDGE_PORTAL", "1").strip().lower() in {"1", "true", "yes"}

app = FastAPI(
    title="MT5 Bridge",
    description="Language-agnostic HTTP and WebSocket API over the MetaTrader 5 terminal",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)
api = APIRouter(dependencies=[Security(require_api_key)])
trading = APIRouter(dependencies=[Security(require_trading)])


@app.middleware("http")
async def guard(request: Request, call_next):
    """Per client rate limit, request body cap, and response hardening."""
    ip = _peer(request)
    if not security.allow_request(ip):
        return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > security.max_body_bytes():
        return JSONResponse(status_code=413, content={"detail": "Request body too large"})
    if not security.origin_allowed(request.headers.get("origin"), request.headers.get("host")):
        return JSONResponse(status_code=403, content={"detail": "Origin not allowed"})
    response = await call_next(request)
    for header, value in security.SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


@app.exception_handler(core.NotConnectedError)
async def _not_connected_handler(request: Request, exc: core.NotConnectedError):
    return JSONResponse(status_code=503, content={"detail": exc.message})


@app.exception_handler(core.NotFoundError)
async def _not_found_handler(request: Request, exc: core.NotFoundError):
    return JSONResponse(status_code=404, content={"detail": exc.message})


@app.exception_handler(core.BridgeError)
async def _bridge_error_handler(request: Request, exc: core.BridgeError):
    return JSONResponse(
        status_code=502,
        content={
            "detail": {
                "message": exc.message,
                "code": exc.code,
                "description": exc.description,
            }
        },
    )


@app.exception_handler(ValueError)
async def _value_error_handler(request: Request, exc: ValueError):
    # Only the bridge's own validation messages reach the caller, capped
    # so an unexpected error cannot turn into an information leak
    return JSONResponse(status_code=400, content={"detail": str(exc)[:200]})


@app.get("/health")
def health():
    """Liveness probe. Deliberately the only unauthenticated endpoint."""
    return {"status": "ok", "connected": core.is_connected()}


@api.post("/initialize")
def initialize():
    if not core.try_initialize():
        raise core.mt5_error("initialize failed")
    return {"connected": True}


@api.get("/terminal")
def terminal_info():
    return core.get_terminal()


@api.get("/account")
def account_info():
    return core.get_account()


@api.get("/symbols")
def list_symbols(
    search: str = Query(default="*", max_length=64),
    limit: int = Query(default=500, ge=1, le=10_000),
):
    names = core.get_symbols(search)
    return {"total": len(names), "symbols": names[:limit]}


@api.get("/symbols/{symbol}")
def symbol_info(symbol: str = SYMBOL_PATH):
    return core.get_symbol_info(symbol)


@api.post("/symbols/{symbol}/select")
def symbol_select(symbol: str = SYMBOL_PATH, enable: bool = True):
    core.select_symbol(symbol, enable)
    return {"symbol": symbol, "selected": enable}


@api.get("/symbols/{symbol}/tick")
def symbol_tick(symbol: str = SYMBOL_PATH):
    return core.get_tick(symbol)


@api.get("/rates/{symbol}")
def rates(
    symbol: str = SYMBOL_PATH,
    timeframe: str = Query(default="M1", max_length=4),
    count: int = Query(default=100, ge=1, le=100_000),
    start_pos: int = Query(default=0, ge=0, le=10_000_000),
    time_from: str | None = Query(default=None, max_length=40),
    time_to: str | None = Query(default=None, max_length=40),
):
    """Bars for a symbol.

    Modes: time_from and time_to -> copy_rates_range; time_from and count ->
    copy_rates_from; otherwise start_pos and count -> copy_rates_from_pos.
    Times are unix seconds or ISO-8601.
    """
    records = core.get_rates(symbol, timeframe, count, start_pos, time_from, time_to)
    return {
        "symbol": symbol,
        "timeframe": timeframe.upper(),
        "count": len(records),
        "rates": records,
    }


@api.get("/ticks/{symbol}")
def ticks(
    symbol: str = SYMBOL_PATH,
    time_from: str = Query(..., max_length=40),
    time_to: str | None = Query(default=None, max_length=40),
    count: int = Query(default=1000, ge=1, le=1_000_000),
    flags: str = Query(default="all", max_length=8),
):
    records = core.get_ticks_history(symbol, time_from, time_to, count, flags)
    return {"symbol": symbol, "count": len(records), "ticks": records}


@api.get("/positions")
def positions(symbol: str | None = Query(default=None, max_length=32)):
    return {"positions": core.get_positions(symbol)}


@api.get("/orders")
def pending_orders(symbol: str | None = Query(default=None, max_length=32)):
    return {"orders": core.get_pending_orders(symbol)}


@api.get("/history/orders")
def history_orders(
    time_from: str = Query(..., max_length=40), time_to: str = Query(..., max_length=40)
):
    return {"orders": core.get_history_orders(time_from, time_to)}


@api.get("/history/deals")
def history_deals(
    time_from: str = Query(..., max_length=40), time_to: str = Query(..., max_length=40)
):
    return {"deals": core.get_history_deals(time_from, time_to)}


class MarketOrder(BaseModel):
    model_config = {"extra": "forbid"}

    symbol: str = Field(max_length=32)
    side: str = Field(max_length=4)
    volume: float = Field(gt=0, le=1_000_000)
    sl: float | None = Field(default=None, ge=0)
    tp: float | None = Field(default=None, ge=0)
    deviation: int = Field(default=20, ge=0, le=100_000)
    magic: int = Field(default=0, ge=0, le=2**31 - 1)
    comment: str = Field(default="mt5-bridge", max_length=31)


@trading.post("/orders/market")
def market_order(order: MarketOrder):
    return core.place_market_order(
        order.symbol,
        order.side,
        order.volume,
        sl=order.sl,
        tp=order.tp,
        deviation=order.deviation,
        magic=order.magic,
        comment=order.comment,
    )


@trading.post("/orders/send")
def order_send(request: dict):
    """Raw passthrough to mt5.order_send for pending orders, modifies, etc.

    The body is the MT5 trade request dict with numeric enum values, exactly
    as documented for the official package. Field names and value types are
    validated before the terminal sees them.
    """
    return core.send_raw_order(request)


@trading.post("/orders/check")
def order_check(request: dict):
    return core.check_raw_order(request)


@trading.post("/positions/{ticket}/close")
def close_position(
    ticket: int = TICKET_PATH, deviation: int = Query(default=20, ge=0, le=100_000)
):
    return core.close_position(ticket, deviation)


@app.websocket("/ws/ticks")
async def ws_ticks(websocket: WebSocket):
    if not ws_authorized(websocket):
        await ws_reject(websocket, 4401)
        return
    try:
        symbols = core.parse_symbol_list(websocket.query_params.get("symbols", ""))
    except ValueError:
        await ws_reject(websocket, 4400)
        return
    mode = websocket.query_params.get("mode", "all").lower()
    interval = core.parse_interval(websocket.query_params.get("interval_ms"), 100, 50)
    await websocket.accept()
    try:
        async for tick in core.iter_ticks(symbols, interval, mode):
            await websocket.send_json(tick)
    except WebSocketDisconnect:
        pass
    except core.BridgeError as exc:
        await websocket.close(code=1011, reason=exc.message[:120])


@app.websocket("/ws/account")
async def ws_account(websocket: WebSocket):
    """Streams account snapshots (balance, equity, margin, profit)."""
    if not ws_authorized(websocket):
        await ws_reject(websocket, 4401)
        return
    interval = core.parse_interval(websocket.query_params.get("interval_ms"), 500, 100)
    only_changes = websocket.query_params.get("only_changes", "").lower() in {"1", "true", "yes"}
    await websocket.accept()
    try:
        async for snapshot in core.iter_account(interval, only_changes):
            await websocket.send_json(snapshot)
    except WebSocketDisconnect:
        pass
    except core.BridgeError as exc:
        await websocket.close(code=1011, reason=exc.message[:120])


@app.websocket("/ws/positions")
async def ws_positions(websocket: WebSocket):
    """Streams the open-positions list whenever it changes."""
    if not ws_authorized(websocket):
        await ws_reject(websocket, 4401)
        return
    interval = core.parse_interval(websocket.query_params.get("interval_ms"), 500, 100)
    await websocket.accept()
    try:
        async for rows in core.iter_positions(interval):
            await websocket.send_json({"positions": rows})
    except WebSocketDisconnect:
        pass
    except core.BridgeError as exc:
        await websocket.close(code=1011, reason=exc.message[:120])


# ---------------------------------------------------------------------------
# Portal: the browser console served by the bridge itself.
# Serving it is optional (BRIDGE_PORTAL=0) and every endpoint behind it
# requires a key, an admin key for anything that touches key material.

_PORTAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portal")

if _portal_enabled:
    app.mount("/portal/static", StaticFiles(directory=_PORTAL_DIR), name="portal-static")

    @app.get("/portal")
    def portal_page():
        return FileResponse(os.path.join(_PORTAL_DIR, "index.html"))


class KeyRequest(BaseModel):
    model_config = {"extra": "forbid"}

    label: str = Field(default="unnamed", max_length=64)
    admin: bool = False


portal = APIRouter(prefix="/portal", dependencies=[Security(require_admin)])


@portal.get("/keys")
def portal_list_keys():
    return {"required": keys.key_required(), "keys": keys.list_keys()}


@portal.post("/keys")
def portal_create_key(body: KeyRequest):
    key, entry = keys.create_key(body.label.strip() or "unnamed", admin=body.admin)
    logger.info("Issued %s key %s (%s)", "admin" if body.admin else "standard", entry["id"], entry["label"])
    return {"key": key, "id": entry["id"], "note": "Shown once; store it now."}


@portal.delete("/keys/{key_id}")
def portal_revoke_key(key_id: str = Path(max_length=32, pattern=r"^[a-f0-9]{4,32}$")):
    if not keys.revoke_key(key_id):
        raise HTTPException(status_code=404, detail="Unknown key id")
    logger.info("Revoked key %s", key_id)
    return {"revoked": key_id}


@app.get("/portal/session")
def portal_session(_=Security(require_api_key), api_key: str = Security(_api_key_header)):
    """Tell the console what the presented key is allowed to do."""
    scope = keys.verify(api_key) or {"admin": keys.auth_disabled(), "label": "open"}
    return {
        "admin": scope["admin"],
        "label": scope["label"],
        "auth_required": keys.key_required(),
        "read_only": security.read_only(),
    }


async def _relay_grpc(websocket: WebSocket, build_stream, to_json):
    """Shared plumbing for the portal's gRPC relay sockets."""
    import grpc

    provided = websocket.headers.get("x-api-key") or websocket.query_params.get("api_key") or ""
    metadata = [("x-api-key", provided)] if provided else []
    target = f"127.0.0.1:{int(os.getenv('BRIDGE_GRPC_PORT', '8002'))}"
    try:
        async with grpc.aio.insecure_channel(target) as channel:
            async for message in build_stream(channel, metadata):
                await websocket.send_json(to_json(message))
    except WebSocketDisconnect:
        pass
    except grpc.aio.AioRpcError as exc:
        await websocket.close(code=1011, reason=str(exc.details())[:120])


@app.websocket("/portal/grpc/account")
async def ws_grpc_account(websocket: WebSocket):
    """Relays the gRPC StreamAccount stream to the browser, so the portal
    can demonstrate the real gRPC path end to end."""
    if not ws_authorized(websocket):
        await ws_reject(websocket, 4401)
        return
    await websocket.accept()
    import mt5bridge_pb2 as pb
    import mt5bridge_pb2_grpc as pb_grpc

    interval = int(core.parse_interval(websocket.query_params.get("interval_ms"), 500, 100) * 1000)

    def build(channel, metadata):
        stub = pb_grpc.MT5BridgeStub(channel)
        return stub.StreamAccount(pb.StreamAccountRequest(interval_ms=interval), metadata=metadata)

    await _relay_grpc(
        websocket,
        build,
        lambda snapshot: {
            "source": "grpc",
            "login": snapshot.login,
            "server": snapshot.server,
            "currency": snapshot.currency,
            "balance": snapshot.balance,
            "equity": snapshot.equity,
            "profit": snapshot.profit,
            "margin": snapshot.margin,
            "margin_free": snapshot.margin_free,
            "margin_level": snapshot.margin_level,
            "leverage": snapshot.leverage,
        },
    )


@app.websocket("/portal/grpc/ticks")
async def ws_grpc_ticks(websocket: WebSocket):
    """Relays the gRPC StreamTicks stream to the browser, so the portal's
    price panel can demonstrate the real gRPC path end to end."""
    if not ws_authorized(websocket):
        await ws_reject(websocket, 4401)
        return
    try:
        symbols = core.parse_symbol_list(websocket.query_params.get("symbols", ""))
    except ValueError:
        await ws_reject(websocket, 4400)
        return
    mode = websocket.query_params.get("mode", "all").lower()
    interval = int(core.parse_interval(websocket.query_params.get("interval_ms"), 100, 50) * 1000)
    await websocket.accept()
    import mt5bridge_pb2 as pb
    import mt5bridge_pb2_grpc as pb_grpc

    def build(channel, metadata):
        stub = pb_grpc.MT5BridgeStub(channel)
        return stub.StreamTicks(
            pb.StreamTicksRequest(symbols=symbols, mode=mode, interval_ms=interval),
            metadata=metadata,
        )

    await _relay_grpc(
        websocket,
        build,
        lambda tick: {
            "source": "grpc",
            "symbol": tick.symbol,
            "time": tick.time,
            "bid": tick.bid,
            "ask": tick.ask,
            "last": tick.last,
            "volume": tick.volume,
            "time_msc": tick.time_msc,
            "flags": tick.flags,
            "volume_real": tick.volume_real,
        },
    )


app.include_router(api)
app.include_router(trading)
if _portal_enabled:
    app.include_router(portal)


def startup_checks():
    """Refuse to serve in a configuration that would be unsafe."""
    try:
        keys.ensure_bootstrap()
    except keys.WeakKeyError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    host = os.getenv("BRIDGE_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "localhost", "::1"} and keys.auth_disabled():
        logger.error(
            "Refusing to bind %s with BRIDGE_ALLOW_OPEN set: that would expose an "
            "unauthenticated trading API. Unset BRIDGE_ALLOW_OPEN or bind 127.0.0.1.",
            host,
        )
        sys.exit(1)
    if security.read_only():
        logger.info("Read-only mode: order and position endpoints are disabled")


if __name__ == "__main__":
    import uvicorn

    startup_checks()
    uvicorn.run(
        app,
        # Loopback by default: exposing the bridge is an explicit decision,
        # made by setting BRIDGE_HOST, not something that happens silently
        host=os.getenv("BRIDGE_HOST", "127.0.0.1"),
        port=int(os.getenv("BRIDGE_PORT", "8001")),
        # Explicit pure-python implementations so the same code runs under
        # Wine, where compiled optional extras may be unavailable
        loop="asyncio",
        http="h11",
        ws="websockets",
        # Protocol-level heartbeat: the server pings every 20s and drops
        # peers that fail to answer within 20s, so half-open connections
        # (NAT timeouts, suspended laptops) are reaped instead of leaking
        ws_ping_interval=20.0,
        ws_ping_timeout=20.0,
        # Only a configured proxy may set X-Forwarded-For
        forwarded_allow_ips=os.getenv("BRIDGE_TRUSTED_PROXIES", "") or None,
        server_header=False,
        date_header=True,
        log_level="info",
    )
