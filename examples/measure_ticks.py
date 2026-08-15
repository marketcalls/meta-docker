import time
from datetime import datetime, timezone

import MetaTrader5 as mt5

SYMBOLS = ["EURUSD", "GBPUSD", "XAUUSD"]

if not mt5.initialize():
    raise SystemExit(f"initialize failed: {mt5.last_error()}")

print("terminal:", mt5.terminal_info().name, "| server:", mt5.account_info().server)
print()
print("Historical rate (last 60 seconds of broker time):")
anchors = {}
for symbol in SYMBOLS:
    mt5.symbol_select(symbol, True)
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        print(f"  {symbol}: no tick available")
        continue
    anchors[symbol] = tick.time_msc
    end = datetime.fromtimestamp(tick.time_msc / 1000.0, tz=timezone.utc)
    start = datetime.fromtimestamp((tick.time_msc - 60_000) / 1000.0, tz=timezone.utc)
    ticks = mt5.copy_ticks_range(symbol, start, end, mt5.COPY_TICKS_ALL)
    count = 0 if ticks is None else len(ticks)
    print(f"  {symbol}: {count} ticks in 60s = {count / 60:.1f}/sec")

print()
print("Live rate (sampling for 10 seconds via copy_ticks_from):")
last_msc = dict(anchors)
counts = {s: 0 for s in last_msc}
calls = 0
t0 = time.monotonic()
while time.monotonic() - t0 < 10:
    for symbol in list(last_msc):
        since = datetime.fromtimestamp(last_msc[symbol] / 1000.0, tz=timezone.utc)
        ticks = mt5.copy_ticks_from(symbol, since, 2000, mt5.COPY_TICKS_ALL)
        calls += 1
        if ticks is None or len(ticks) == 0:
            continue
        for row in ticks:
            msc = int(row["time_msc"])
            if msc > last_msc[symbol]:
                last_msc[symbol] = msc
                counts[symbol] += 1
    time.sleep(0.1)
elapsed = time.monotonic() - t0
for symbol, count in counts.items():
    print(f"  {symbol}: {count} ticks in {elapsed:.1f}s = {count / elapsed:.1f}/sec")
print(f"  IPC calls made: {calls} ({calls / elapsed:.0f}/sec) with no strain")

mt5.shutdown()
