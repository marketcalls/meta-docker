"""Framework-neutral core over the official MetaTrader5 package.

Shared by the REST/WebSocket server (app.py) and the gRPC server
(grpc_server.py). All terminal access goes through call() so the IPC
channel is never used concurrently.
"""

import asyncio
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5

logger = logging.getLogger("mt5bridge")

_mt5_lock = threading.Lock()

# Terminal session state. The counter increments every time the bridge
# establishes a connection to a terminal that it was not connected to a
# moment ago, so a client that reconnects can tell "same terminal, I was
# just idle" from "new terminal session, I have a hole in my data".
_session_lock = threading.Lock()
_session = {"id": 0, "connected": False, "since": None}

# Symbols are the only free-form strings that reach the terminal, so they
# are held to the shape brokers actually use: starts alphanumeric, then
# letters, digits and the few separators seen in suffixed names such as
# EURUSD.a or XAUUSD#. No slashes and no leading dot, which keeps
# traversal-shaped values out of the terminal and out of the logs.
SYMBOL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._#&-]{0,31}$"
SYMBOL_RE = re.compile(SYMBOL_PATTERN)
MAX_STREAM_SYMBOLS = 32


def call(thunk):
    """Run a zero-argument closure of mt5 calls under the IPC lock.

    The MetaTrader5 C extension validates how its functions are invoked;
    calling them through argument-unpacking wrappers (fn(*args)) makes the
    trade functions fail with (-2, 'Unnamed arguments not allowed'). Every
    mt5.* invocation must therefore be a plain call written out inside the
    closure: call(lambda: mt5.order_send(request)).
    """
    with _mt5_lock:
        return thunk()


TIMEFRAMES = {
    "M1": mt5.TIMEFRAME_M1,
    "M2": mt5.TIMEFRAME_M2,
    "M3": mt5.TIMEFRAME_M3,
    "M4": mt5.TIMEFRAME_M4,
    "M5": mt5.TIMEFRAME_M5,
    "M6": mt5.TIMEFRAME_M6,
    "M10": mt5.TIMEFRAME_M10,
    "M12": mt5.TIMEFRAME_M12,
    "M15": mt5.TIMEFRAME_M15,
    "M20": mt5.TIMEFRAME_M20,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H2": mt5.TIMEFRAME_H2,
    "H3": mt5.TIMEFRAME_H3,
    "H4": mt5.TIMEFRAME_H4,
    "H6": mt5.TIMEFRAME_H6,
    "H8": mt5.TIMEFRAME_H8,
    "H12": mt5.TIMEFRAME_H12,
    "D1": mt5.TIMEFRAME_D1,
    "W1": mt5.TIMEFRAME_W1,
    "MN1": mt5.TIMEFRAME_MN1,
}

TICK_FLAGS = {
    "all": mt5.COPY_TICKS_ALL,
    "info": mt5.COPY_TICKS_INFO,
    "trade": mt5.COPY_TICKS_TRADE,
}

ORDER_SIDES = {
    "buy": mt5.ORDER_TYPE_BUY,
    "sell": mt5.ORDER_TYPE_SELL,
}

# The raw order_send passthrough forwards a caller supplied dict straight
# into the terminal, so only the documented trade request fields are let
# through, each with the type MetaTrader expects. Anything else is a typo
# at best and an attempt to reach into the extension at worst.
TRADE_REQUEST_FIELDS = {
    "action": int,
    "magic": int,
    "order": int,
    "symbol": str,
    "volume": float,
    "price": float,
    "stoplimit": float,
    "sl": float,
    "tp": float,
    "deviation": int,
    "type": int,
    "type_filling": int,
    "type_time": int,
    "expiration": int,
    "comment": str,
    "position": int,
    "position_by": int,
}
MAX_COMMENT_LENGTH = 31

# Brokers disagree on supported filling modes; try them in order until one
# is accepted.
FILLING_FALLBACK = (
    mt5.ORDER_FILLING_IOC,
    mt5.ORDER_FILLING_FOK,
    mt5.ORDER_FILLING_RETURN,
)


class BridgeError(Exception):
    """Terminal rejected or failed a call; carries mt5.last_error()."""

    def __init__(self, message, code=None, description=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.description = description


class NotConnectedError(BridgeError):
    """No IPC connection to a terminal yet."""


class NotFoundError(BridgeError):
    """Requested entity (symbol, position) does not exist."""


def mt5_error(message):
    code, description = call(lambda: mt5.last_error())
    return BridgeError(message, code, description)


def session_snapshot():
    """Current terminal session: id, connected flag and start time."""
    with _session_lock:
        return dict(_session)


def _mark_connected():
    """Record a live connection, starting a new session if it is new.

    The id only moves on a transition from down to up, so repeated calls
    from ordinary requests do not make clients think they reconnected.
    """
    with _session_lock:
        if not _session["connected"]:
            _session["id"] += 1
            _session["since"] = time.time()
            _session["connected"] = True
            return _session["id"], True
        return _session["id"], False


def _mark_disconnected():
    with _session_lock:
        was_connected = _session["connected"]
        _session["connected"] = False
        return was_connected


def is_connected():
    """Live IPC check, which also refreshes the cached session state."""
    alive = call(lambda: mt5.terminal_info()) is not None
    if alive:
        _mark_connected()
    else:
        _mark_disconnected()
    return alive


def ensure_connected():
    if not is_connected():
        raise NotConnectedError("Not connected to a MetaTrader 5 terminal")


def build_init_kwargs():
    kwargs = {"timeout": int(os.getenv("MT5_TIMEOUT_MS", "60000"))}
    login = os.getenv("MT5_LOGIN")
    if login:
        kwargs["login"] = int(login)
        kwargs["password"] = os.getenv("MT5_PASSWORD", "")
        kwargs["server"] = os.getenv("MT5_SERVER", "")
    if os.getenv("MT5_PORTABLE", "").strip().lower() in {"1", "true", "yes"}:
        kwargs["portable"] = True
    return kwargs


def try_initialize():
    kwargs = build_init_kwargs()
    path = os.getenv("MT5_PATH")
    with _mt5_lock:
        if path:
            connected = mt5.initialize(path, **kwargs)
        else:
            connected = mt5.initialize(**kwargs)
    if connected:
        _mark_connected()
    return connected


def shutdown():
    call(lambda: mt5.shutdown())


async def connect_loop():
    """Attach to the terminal and keep it attached.

    This supervises rather than returning after the first success. The
    container watchdog restarts terminal64.exe on its own, and a bridge
    that stopped watching would answer 503 to every request until someone
    called POST /initialize by hand. Re-attaching is the whole point of
    running this next to a process that can restart underneath it.

    MT5_INIT_RETRIES bounds the consecutive failures in one attach cycle,
    not the lifetime of the bridge: a successful attach resets the count,
    so a terminal that dies again later gets the full retry budget again.
    """
    attempts = int(os.getenv("MT5_INIT_RETRIES", "0"))  # 0 = retry forever
    delay = float(os.getenv("MT5_INIT_RETRY_SECONDS", "5"))
    watch = max(float(os.getenv("MT5_WATCH_SECONDS", "5")), 1.0)
    failures = 0
    while True:
        try:
            if await asyncio.to_thread(is_connected):
                failures = 0
                await asyncio.sleep(watch)
                continue

            if _mark_disconnected():
                logger.warning("Terminal connection lost, re-attaching")

            failures += 1
            if await asyncio.to_thread(try_initialize):
                info = call(lambda: mt5.terminal_info())
                logger.info(
                    "Connected to terminal build %s at %s (session %d)",
                    getattr(info, "build", "unknown"),
                    getattr(info, "path", "unknown"),
                    session_snapshot()["id"],
                )
                failures = 0
                continue

            label = f"{failures}/{attempts}" if attempts > 0 else str(failures)
            logger.warning(
                "initialize attempt %s failed: %s", label, call(lambda: mt5.last_error())
            )
            if 0 < attempts <= failures:
                logger.error(
                    "Giving up after %d attempts; POST /initialize to retry", attempts
                )
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            # The supervisor is what keeps /health honest, so it must not
            # die on an unexpected error from the terminal or the wire
            logger.exception("Connection supervisor hit an unexpected error")
        await asyncio.sleep(delay)


def named_tuple_dict(value):
    return value._asdict() if hasattr(value, "_asdict") else value


def named_tuple_list(values):
    if values is None:
        return []
    return [v._asdict() for v in values]


def array_to_records(array):
    if array is None:
        return []
    names = array.dtype.names or ()
    return [dict(zip(names, row.tolist())) for row in array]


def trade_result_dict(result):
    data = result._asdict()
    request = data.get("request")
    if hasattr(request, "_asdict"):
        data["request"] = request._asdict()
    return data


def validate_symbol(symbol):
    """Return the symbol if it looks like a MetaTrader symbol, else raise."""
    if not isinstance(symbol, str) or not SYMBOL_RE.match(symbol):
        raise ValueError(f"Invalid symbol {str(symbol)[:32]!r}")
    return symbol


def parse_symbol_list(raw):
    """Split a comma or semicolon separated symbol list from a query string."""
    symbols = [s.strip() for s in (raw or "").replace(";", ",").split(",") if s.strip()]
    if not symbols:
        raise ValueError("no symbols given")
    if len(symbols) > MAX_STREAM_SYMBOLS:
        raise ValueError(f"at most {MAX_STREAM_SYMBOLS} symbols per stream")
    return [validate_symbol(s) for s in symbols]


def parse_interval(raw, default_ms, floor_ms):
    """Poll interval in seconds, never below the floor that protects the
    terminal's single IPC channel from being hammered."""
    try:
        value = int(raw) if raw not in (None, "") else default_ms
    except (TypeError, ValueError):
        value = default_ms
    return max(min(value, 60_000), floor_ms) / 1000.0


def validate_trade_request(request):
    """Coerce a raw trade request to the documented fields and types."""
    if not isinstance(request, dict):
        raise ValueError("trade request must be an object")
    if len(request) > len(TRADE_REQUEST_FIELDS):
        raise ValueError("trade request has too many fields")
    cleaned = {}
    for name, value in request.items():
        expected = TRADE_REQUEST_FIELDS.get(name)
        if expected is None:
            raise ValueError(f"Unknown trade request field {str(name)[:32]!r}")
        if isinstance(value, bool):
            raise ValueError(f"Field {name} must be {expected.__name__}, not a boolean")
        if expected is str:
            if not isinstance(value, str):
                raise ValueError(f"Field {name} must be a string")
            if name == "symbol":
                cleaned[name] = validate_symbol(value)
            else:
                cleaned[name] = value[:MAX_COMMENT_LENGTH]
            continue
        if not isinstance(value, (int, float)):
            raise ValueError(f"Field {name} must be a number")
        cleaned[name] = expected(value)
    if "action" not in cleaned:
        raise ValueError("trade request needs an action")
    return cleaned


def parse_time(value):
    """Accept unix epoch seconds or an ISO-8601 string, return aware UTC."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"Invalid time {value!r}; use unix seconds or ISO-8601")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# Data access


def get_terminal():
    ensure_connected()
    return named_tuple_dict(call(lambda: mt5.terminal_info()))


def get_account():
    ensure_connected()
    info = call(lambda: mt5.account_info())
    if info is None:
        raise mt5_error("account_info failed; is the terminal logged in?")
    return info._asdict()


def get_symbols(search="*"):
    ensure_connected()
    if not isinstance(search, str) or len(search) > 64:
        raise ValueError("search pattern must be a string of at most 64 characters")
    symbols = call(lambda: mt5.symbols_get(search))
    if symbols is None:
        raise mt5_error("symbols_get failed")
    return [s.name for s in symbols]


def get_symbol_info(symbol):
    ensure_connected()
    symbol = validate_symbol(symbol)
    info = call(lambda: mt5.symbol_info(symbol))
    if info is None:
        raise NotFoundError(f"Unknown symbol {symbol}")
    return info._asdict()


def select_symbol(symbol, enable=True):
    ensure_connected()
    symbol = validate_symbol(symbol)
    if not call(lambda: mt5.symbol_select(symbol, enable)):
        raise mt5_error(f"symbol_select failed for {symbol}")


def get_tick(symbol):
    ensure_connected()
    symbol = validate_symbol(symbol)
    tick = call(lambda: mt5.symbol_info_tick(symbol))
    if tick is None:
        raise mt5_error(f"symbol_info_tick failed for {symbol}")
    return tick._asdict()


def get_rates(symbol, timeframe="M1", count=100, start_pos=0, time_from=None, time_to=None):
    ensure_connected()
    symbol = validate_symbol(symbol)
    tf = TIMEFRAMES.get(str(timeframe).upper())
    if tf is None:
        raise ValueError(f"Unknown timeframe {timeframe}; use one of {sorted(TIMEFRAMES)}")
    t_from = parse_time(time_from)
    t_to = parse_time(time_to)
    if t_from and t_to:
        array = call(lambda: mt5.copy_rates_range(symbol, tf, t_from, t_to))
    elif t_from:
        array = call(lambda: mt5.copy_rates_from(symbol, tf, t_from, count))
    else:
        array = call(lambda: mt5.copy_rates_from_pos(symbol, tf, start_pos, count))
    if array is None:
        raise mt5_error(f"rates fetch failed for {symbol} {timeframe}")
    return array_to_records(array)


def get_ticks_history(symbol, time_from, time_to=None, count=1000, flags="all"):
    ensure_connected()
    symbol = validate_symbol(symbol)
    flag = TICK_FLAGS.get(str(flags).lower())
    if flag is None:
        raise ValueError(f"Unknown flags {flags}; use one of {sorted(TICK_FLAGS)}")
    t_from = parse_time(time_from)
    t_to = parse_time(time_to)
    if t_to:
        array = call(lambda: mt5.copy_ticks_range(symbol, t_from, t_to, flag))
    else:
        array = call(lambda: mt5.copy_ticks_from(symbol, t_from, count, flag))
    if array is None:
        raise mt5_error(f"ticks fetch failed for {symbol}")
    return array_to_records(array)


def get_positions(symbol=None):
    ensure_connected()
    if symbol:
        symbol = validate_symbol(symbol)
        found = call(lambda: mt5.positions_get(symbol=symbol))
    else:
        found = call(lambda: mt5.positions_get())
    return named_tuple_list(found)


def get_pending_orders(symbol=None):
    ensure_connected()
    if symbol:
        symbol = validate_symbol(symbol)
        found = call(lambda: mt5.orders_get(symbol=symbol))
    else:
        found = call(lambda: mt5.orders_get())
    return named_tuple_list(found)


def get_history_orders(time_from, time_to):
    ensure_connected()
    t_from = parse_time(time_from)
    t_to = parse_time(time_to)
    found = call(lambda: mt5.history_orders_get(t_from, t_to))
    if found is None:
        raise mt5_error("history_orders_get failed")
    return named_tuple_list(found)


def get_history_deals(time_from, time_to):
    ensure_connected()
    t_from = parse_time(time_from)
    t_to = parse_time(time_to)
    found = call(lambda: mt5.history_deals_get(t_from, t_to))
    if found is None:
        raise mt5_error("history_deals_get failed")
    return named_tuple_list(found)


# ---------------------------------------------------------------------------
# Trading


def send_with_filling_fallback(request):
    """Try filling modes in order; an unsupported mode surfaces either as a
    TRADE_RETCODE_INVALID_FILL result or, under Wine, as a bare None."""
    last_result = None
    for filling in FILLING_FALLBACK:
        request["type_filling"] = filling
        result = call(lambda: mt5.order_send(request))
        if result is None:
            logger.warning(
                "order_send returned None with filling %s: %s",
                filling,
                call(lambda: mt5.last_error()),
            )
            continue
        last_result = result
        if result.retcode != mt5.TRADE_RETCODE_INVALID_FILL:
            return result
    if last_result is None:
        raise mt5_error("order_send returned no result for any filling mode")
    return last_result


def place_market_order(symbol, side, volume, sl=None, tp=None, deviation=20, magic=0, comment="mt5-bridge"):
    ensure_connected()
    symbol = validate_symbol(symbol)
    side = str(side or "").lower()
    if side not in ORDER_SIDES:
        raise ValueError("side must be buy or sell")
    comment = str(comment)[:MAX_COMMENT_LENGTH]
    select_symbol(symbol, True)
    tick = call(lambda: mt5.symbol_info_tick(symbol))
    if tick is None:
        raise mt5_error(f"no tick for {symbol}")
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": ORDER_SIDES[side],
        "price": tick.ask if side == "buy" else tick.bid,
        "deviation": deviation,
        "magic": magic,
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
    }
    if sl is not None:
        request["sl"] = sl
    if tp is not None:
        request["tp"] = tp
    return trade_result_dict(send_with_filling_fallback(request))


def close_position(ticket, deviation=20):
    ensure_connected()
    found = call(lambda: mt5.positions_get(ticket=ticket))
    if not found:
        raise NotFoundError(f"No open position with ticket {ticket}")
    position = found[0]
    closing_side = "sell" if position.type == mt5.POSITION_TYPE_BUY else "buy"
    tick = call(lambda: mt5.symbol_info_tick(position.symbol))
    if tick is None:
        raise mt5_error(f"no tick for {position.symbol}")
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": position.symbol,
        "volume": position.volume,
        "type": ORDER_SIDES[closing_side],
        "position": ticket,
        "price": tick.ask if closing_side == "buy" else tick.bid,
        "deviation": deviation,
        "magic": position.magic,
        "comment": "mt5-bridge close",
        "type_time": mt5.ORDER_TIME_GTC,
    }
    return trade_result_dict(send_with_filling_fallback(request))


def send_raw_order(request):
    ensure_connected()
    request = validate_trade_request(request)
    result = call(lambda: mt5.order_send(request))
    if result is None:
        raise mt5_error("order_send returned no result")
    return trade_result_dict(result)


def check_raw_order(request):
    ensure_connected()
    request = validate_trade_request(request)
    result = call(lambda: mt5.order_check(request))
    if result is None:
        raise mt5_error("order_check returned no result")
    return trade_result_dict(result)


# ---------------------------------------------------------------------------
# Streaming


async def iter_ticks(symbols, interval, mode="all"):
    """Async generator of tick dicts for the given symbols.

    mode "all": lossless; each poll drains every tick since the last one
    seen via copy_ticks_from. Fetches overlap on the anchor millisecond;
    a duplicate counter filters re-delivery of ticks sharing that
    millisecond.
    mode "latest": sampled; at most one tick per poll per symbol.
    """
    symbols = [validate_symbol(s) for s in symbols][:MAX_STREAM_SYMBOLS]
    for symbol in symbols:
        await asyncio.to_thread(select_symbol, symbol, True)

    if mode == "latest":
        last_seen = {}
        while True:
            for symbol in symbols:
                tick = await asyncio.to_thread(call, lambda: mt5.symbol_info_tick(symbol))
                if tick is None:
                    continue
                if last_seen.get(symbol) != tick.time_msc:
                    last_seen[symbol] = tick.time_msc
                    yield {"symbol": symbol, **tick._asdict()}
            await asyncio.sleep(interval)
        return

    # When the terminal has just booted, its last known tick can be hours
    # old; draining that backlog floods clients with stale history. If a
    # full batch is still this far behind the live tick, jump forward.
    max_backfill_ms = int(os.getenv("BRIDGE_TICK_MAX_BACKFILL_MS", "10000"))
    batch_size = 2000

    anchors = {}
    while True:
        for symbol in symbols:
            if symbol not in anchors:
                tick = await asyncio.to_thread(call, lambda: mt5.symbol_info_tick(symbol))
                if tick is not None:
                    anchors[symbol] = (tick.time_msc, 1_000_000)
                continue
            anchor_msc, sent_dups = anchors[symbol]
            since = datetime.fromtimestamp(anchor_msc / 1000.0, tz=timezone.utc)
            array = await asyncio.to_thread(
                call, lambda: mt5.copy_ticks_from(symbol, since, batch_size, mt5.COPY_TICKS_ALL)
            )
            if array is None or len(array) == 0:
                continue
            if len(array) >= batch_size:
                live = await asyncio.to_thread(call, lambda: mt5.symbol_info_tick(symbol))
                batch_last_msc = int(array[-1]["time_msc"])
                if live is not None and live.time_msc - batch_last_msc > max_backfill_ms:
                    logger.warning(
                        "Tick stream for %s is %.0fs behind live; skipping backlog",
                        symbol,
                        (live.time_msc - batch_last_msc) / 1000.0,
                    )
                    anchors[symbol] = (live.time_msc - max_backfill_ms, 0)
                    continue
            seen_dups = 0
            for record in array_to_records(array):
                msc = int(record["time_msc"])
                if msc < anchor_msc:
                    continue
                if msc == anchor_msc:
                    seen_dups += 1
                    if seen_dups <= sent_dups:
                        continue
                    sent_dups = seen_dups
                else:
                    anchor_msc = msc
                    sent_dups = 1
                    seen_dups = 1
                yield {"symbol": symbol, **record}
            anchors[symbol] = (anchor_msc, sent_dups)
        await asyncio.sleep(interval)


async def iter_account(interval, only_changes=False):
    """Async generator of account dicts (balance, equity, margin, ...)."""
    last = None
    while True:
        data = await asyncio.to_thread(get_account)
        snapshot = (
            data.get("balance"),
            data.get("equity"),
            data.get("profit"),
            data.get("margin"),
            data.get("margin_free"),
        )
        if not only_changes or snapshot != last:
            last = snapshot
            yield data
        await asyncio.sleep(interval)


async def iter_positions(interval):
    """Async generator of position list snapshots, emitted on change."""
    last = None
    while True:
        rows = await asyncio.to_thread(get_positions)
        key = repr(rows)
        if key != last:
            last = key
            yield rows
        await asyncio.sleep(interval)
