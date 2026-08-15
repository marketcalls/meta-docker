"""gRPC server over the MT5 core, running alongside the FastAPI app.

Contract: bridge/proto/mt5bridge.proto. Streams are server-side: ticks
(lossless or sampled), account snapshots (balance/equity/margin), and
position-list changes.

Auth: every call must carry x-api-key metadata holding a key the bridge
knows, checked in constant time by the same store the REST API uses.
Trading RPCs also honour BRIDGE_READ_ONLY.

Transport: plaintext on loopback by default. Set BRIDGE_GRPC_TLS_CERT and
BRIDGE_GRPC_TLS_KEY to serve TLS directly, or terminate TLS in a proxy.

Environment
-----------
  BRIDGE_GRPC_HOST / BRIDGE_GRPC_PORT   bind address (default 127.0.0.1:8002)
  BRIDGE_GRPC_TLS_CERT / BRIDGE_GRPC_TLS_KEY   PEM files for direct TLS
  BRIDGE_GRPC_MAX_CONCURRENT            concurrent RPCs per server (default 64)
"""

import asyncio
import logging
import os

import grpc

import core
import keys
import mt5bridge_pb2 as pb
import mt5bridge_pb2_grpc as pb_grpc
import security

logger = logging.getLogger("mt5bridge.grpc")

# gRPC has no equivalent of the HTTP middleware, so the same caps are
# applied here: bodies stay small, streams are bounded
MAX_MESSAGE_BYTES = 4 * 1024 * 1024


async def _deny_trading(context):
    await context.abort(
        grpc.StatusCode.PERMISSION_DENIED, "Bridge is running read-only (BRIDGE_READ_ONLY)"
    )


def _fail_stream(context, exc):
    """End a server stream with a meaningful status.

    A server-streaming handler cannot abort the way a unary one does: an
    exception escaping the generator reaches the client as UNKNOWN, which
    tells the caller nothing. Setting the code and returning gives them
    the same status vocabulary the unary RPCs use.
    """
    if isinstance(exc, core.NotConnectedError):
        code, message = grpc.StatusCode.UNAVAILABLE, exc.message
    elif isinstance(exc, core.NotFoundError):
        code, message = grpc.StatusCode.NOT_FOUND, exc.message
    elif isinstance(exc, core.BridgeError):
        code = grpc.StatusCode.INTERNAL
        message = f"{exc.message} (mt5 error {exc.code}: {exc.description})"
    else:
        code, message = grpc.StatusCode.INVALID_ARGUMENT, str(exc)
    context.set_code(code)
    context.set_details(message[:200])


async def _guard(context, fn):
    """Run a sync core function in a thread, mapping errors to gRPC codes."""
    try:
        return await asyncio.to_thread(fn)
    except core.NotConnectedError as exc:
        await context.abort(grpc.StatusCode.UNAVAILABLE, exc.message)
    except core.NotFoundError as exc:
        await context.abort(grpc.StatusCode.NOT_FOUND, exc.message)
    except ValueError as exc:
        await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
    except core.BridgeError as exc:
        await context.abort(
            grpc.StatusCode.INTERNAL,
            f"{exc.message} (mt5 error {exc.code}: {exc.description})",
        )


def _account_msg(data):
    return pb.Account(
        login=int(data.get("login", 0)),
        server=data.get("server", ""),
        currency=data.get("currency", ""),
        balance=data.get("balance", 0.0),
        equity=data.get("equity", 0.0),
        profit=data.get("profit", 0.0),
        margin=data.get("margin", 0.0),
        margin_free=data.get("margin_free", 0.0),
        margin_level=data.get("margin_level", 0.0),
        leverage=int(data.get("leverage", 0)),
        trade_allowed=bool(data.get("trade_allowed", False)),
        name=data.get("name", ""),
    )


def _tick_msg(data):
    return pb.Tick(
        symbol=data.get("symbol", ""),
        time=int(data.get("time", 0)),
        bid=data.get("bid", 0.0),
        ask=data.get("ask", 0.0),
        last=data.get("last", 0.0),
        volume=int(data.get("volume", 0)),
        time_msc=int(data.get("time_msc", 0)),
        flags=int(data.get("flags", 0)),
        volume_real=data.get("volume_real", 0.0),
    )


def _rate_msg(data):
    return pb.Rate(
        time=int(data.get("time", 0)),
        open=data.get("open", 0.0),
        high=data.get("high", 0.0),
        low=data.get("low", 0.0),
        close=data.get("close", 0.0),
        tick_volume=int(data.get("tick_volume", 0)),
        spread=int(data.get("spread", 0)),
        real_volume=int(data.get("real_volume", 0)),
    )


def _position_msg(data):
    return pb.Position(
        ticket=int(data.get("ticket", 0)),
        symbol=data.get("symbol", ""),
        type=int(data.get("type", 0)),
        volume=data.get("volume", 0.0),
        price_open=data.get("price_open", 0.0),
        price_current=data.get("price_current", 0.0),
        sl=data.get("sl", 0.0),
        tp=data.get("tp", 0.0),
        profit=data.get("profit", 0.0),
        swap=data.get("swap", 0.0),
        magic=int(data.get("magic", 0)),
        comment=data.get("comment", ""),
        time_msc=int(data.get("time_msc", 0)),
    )


def _pending_order_msg(data):
    return pb.PendingOrder(
        ticket=int(data.get("ticket", 0)),
        symbol=data.get("symbol", ""),
        type=int(data.get("type", 0)),
        volume_current=data.get("volume_current", 0.0),
        price_open=data.get("price_open", 0.0),
        sl=data.get("sl", 0.0),
        tp=data.get("tp", 0.0),
        magic=int(data.get("magic", 0)),
        comment=data.get("comment", ""),
        time_setup_msc=int(data.get("time_setup_msc", 0)),
    )


def _trade_result_msg(data):
    return pb.TradeResult(
        retcode=int(data.get("retcode", 0)),
        deal=int(data.get("deal", 0)),
        order=int(data.get("order", 0)),
        volume=data.get("volume", 0.0),
        price=data.get("price", 0.0),
        comment=data.get("comment", ""),
        request_id=int(data.get("request_id", 0)),
    )


def _check_result_msg(data):
    return pb.CheckResult(
        retcode=int(data.get("retcode", 0)),
        balance=data.get("balance", 0.0),
        equity=data.get("equity", 0.0),
        profit=data.get("profit", 0.0),
        margin=data.get("margin", 0.0),
        margin_free=data.get("margin_free", 0.0),
        margin_level=data.get("margin_level", 0.0),
        comment=data.get("comment", ""),
    )


def _trade_request_dict(message):
    """Only explicitly set (non-default) proto3 fields are forwarded,
    matching the official dict API where omitted means unset."""
    return {field.name: value for field, value in message.ListFields()}


class BridgeService(pb_grpc.MT5BridgeServicer):
    async def Health(self, request, context):
        # Cached supervisor state, matching GET /health; `session` moves
        # only when the bridge attaches to a terminal it was not attached
        # to a moment ago, so a client can tell an idle gap from a restart
        state = core.session_snapshot()
        return pb.HealthReply(
            connected=state["connected"],
            session=state["id"],
            connected_since=state["since"] or 0.0,
        )

    async def GetAccount(self, request, context):
        data = await _guard(context, core.get_account)
        return _account_msg(data)

    async def GetTick(self, request, context):
        data = await _guard(context, lambda: core.get_tick(request.symbol))
        data["symbol"] = request.symbol
        return _tick_msg(data)

    async def GetRates(self, request, context):
        timeframe = request.timeframe or "M1"
        rows = await _guard(
            context,
            lambda: core.get_rates(
                request.symbol,
                timeframe,
                request.count or 100,
                request.start_pos,
                request.time_from or None,
                request.time_to or None,
            ),
        )
        return pb.RatesReply(
            symbol=request.symbol,
            timeframe=timeframe.upper(),
            rates=[_rate_msg(r) for r in rows],
        )

    async def GetPositions(self, request, context):
        rows = await _guard(context, lambda: core.get_positions(request.symbol or None))
        return pb.PositionsReply(positions=[_position_msg(r) for r in rows])

    async def GetPendingOrders(self, request, context):
        rows = await _guard(context, lambda: core.get_pending_orders(request.symbol or None))
        return pb.PendingOrdersReply(orders=[_pending_order_msg(r) for r in rows])

    async def MarketOrder(self, request, context):
        if security.read_only():
            await _deny_trading(context)
        data = await _guard(
            context,
            lambda: core.place_market_order(
                request.symbol,
                request.side,
                request.volume,
                sl=request.sl or None,
                tp=request.tp or None,
                deviation=request.deviation or 20,
                magic=request.magic,
                comment=request.comment or "mt5-bridge",
            ),
        )
        return _trade_result_msg(data)

    async def SendOrder(self, request, context):
        if security.read_only():
            await _deny_trading(context)
        trade_request = _trade_request_dict(request)
        data = await _guard(context, lambda: core.send_raw_order(trade_request))
        return _trade_result_msg(data)

    async def CheckOrder(self, request, context):
        if security.read_only():
            await _deny_trading(context)
        trade_request = _trade_request_dict(request)
        data = await _guard(context, lambda: core.check_raw_order(trade_request))
        return _check_result_msg(data)

    async def ClosePosition(self, request, context):
        if security.read_only():
            await _deny_trading(context)
        data = await _guard(
            context,
            lambda: core.close_position(request.ticket, request.deviation or 20),
        )
        return _trade_result_msg(data)

    async def StreamTicks(self, request, context):
        try:
            symbols = core.parse_symbol_list(",".join(request.symbols))
            mode = request.mode or "all"
            interval = core.parse_interval(request.interval_ms or None, 100, 50)
            async for tick in core.iter_ticks(symbols, interval, mode):
                yield _tick_msg(tick)
        except (ValueError, core.BridgeError) as exc:
            _fail_stream(context, exc)

    async def StreamAccount(self, request, context):
        try:
            interval = core.parse_interval(request.interval_ms or None, 500, 100)
            async for snapshot in core.iter_account(interval, request.only_changes):
                yield _account_msg(snapshot)
        except (ValueError, core.BridgeError) as exc:
            _fail_stream(context, exc)

    async def StreamPositions(self, request, context):
        try:
            interval = core.parse_interval(request.interval_ms or None, 500, 100)
            async for rows in core.iter_positions(interval):
                yield pb.PositionsReply(positions=[_position_msg(r) for r in rows])
        except (ValueError, core.BridgeError) as exc:
            _fail_stream(context, exc)


class ApiKeyInterceptor(grpc.aio.ServerInterceptor):
    """Reject calls without a known key, in a way that fits every RPC type.

    The rejection handler has to mirror the streaming shape of the RPC it
    replaces, otherwise a streaming client sees an internal error instead
    of a clean UNAUTHENTICATED.
    """

    @staticmethod
    def _deny_handler(handler):
        async def deny_unary(request, context):
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Missing or invalid x-api-key")

        async def deny_stream(request, context):
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Missing or invalid x-api-key")
            yield  # abort raises before this runs; present to make it a generator

        request_streaming = getattr(handler, "request_streaming", False)
        response_streaming = getattr(handler, "response_streaming", False)
        if response_streaming:
            factory = (
                grpc.stream_stream_rpc_method_handler
                if request_streaming
                else grpc.unary_stream_rpc_method_handler
            )
            return factory(deny_stream)
        factory = (
            grpc.stream_unary_rpc_method_handler
            if request_streaming
            else grpc.unary_unary_rpc_method_handler
        )
        return factory(deny_unary)

    async def intercept_service(self, continuation, handler_call_details):
        handler = await continuation(handler_call_details)
        metadata = dict(handler_call_details.invocation_metadata or ())
        peer = metadata.get("x-forwarded-for", "grpc")
        if security.is_locked_out(peer):
            return self._deny_handler(handler)
        if keys.auth_disabled():
            return handler
        if keys.verify(metadata.get("x-api-key")) is None:
            security.record_auth_failure(peer)
            return self._deny_handler(handler)
        security.record_auth_success(peer)
        return handler


def _credentials():
    """Server credentials when a certificate pair is configured."""
    cert_path = os.getenv("BRIDGE_GRPC_TLS_CERT", "").strip()
    key_path = os.getenv("BRIDGE_GRPC_TLS_KEY", "").strip()
    if not cert_path or not key_path:
        return None
    with open(cert_path, "rb") as handle:
        certificate = handle.read()
    with open(key_path, "rb") as handle:
        private_key = handle.read()
    return grpc.ssl_server_credentials([(private_key, certificate)])


async def create_grpc_server():
    # HTTP/2 keepalive: ping idle clients every 30s and allow clients to
    # ping without active calls, so long-lived streams survive NAT/proxy
    # idle timeouts and dead peers are detected within ~40s
    options = [
        ("grpc.keepalive_time_ms", 30_000),
        ("grpc.keepalive_timeout_ms", 10_000),
        ("grpc.keepalive_permit_without_calls", 1),
        ("grpc.http2.max_pings_without_data", 0),
        ("grpc.http2.min_ping_interval_without_data_ms", 10_000),
        ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
        ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
    ]
    server = grpc.aio.server(
        interceptors=[ApiKeyInterceptor()],
        options=options,
        maximum_concurrent_rpcs=int(os.getenv("BRIDGE_GRPC_MAX_CONCURRENT", "64")),
    )
    pb_grpc.add_MT5BridgeServicer_to_server(BridgeService(), server)
    # Loopback default, matching the REST server: reaching the gRPC port
    # from another host has to be an explicit choice
    host = os.getenv("BRIDGE_GRPC_HOST", os.getenv("BRIDGE_HOST", "127.0.0.1"))
    address = f"{host}:{int(os.getenv('BRIDGE_GRPC_PORT', '8002'))}"
    credentials = _credentials()
    if credentials is not None:
        server.add_secure_port(address, credentials)
        logger.info("gRPC server listening on %s (TLS)", address)
    else:
        server.add_insecure_port(address)
        logger.info("gRPC server listening on %s (plaintext)", address)
    await server.start()
    return server
