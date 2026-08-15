"""Exercise the gRPC API: health, account, rates, streams, and a trade.

Usage: python examples/grpc_client.py [--trade]
Environment: BRIDGE_GRPC_URL (default localhost:8002), BRIDGE_API_KEY
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bridge"))

import grpc

import mt5bridge_pb2 as pb
import mt5bridge_pb2_grpc as pb_grpc

TARGET = os.getenv("BRIDGE_GRPC_URL", "localhost:8002")
API_KEY = os.getenv("BRIDGE_API_KEY", "")
METADATA = [("x-api-key", API_KEY)] if API_KEY else []


def main():
    do_trade = "--trade" in sys.argv
    channel = grpc.insecure_channel(TARGET)
    stub = pb_grpc.MT5BridgeStub(channel)

    health = stub.Health(pb.Empty(), metadata=METADATA)
    print("health connected:", health.connected)
    if not health.connected:
        print("bridge is up but not connected to a terminal yet")
        return

    account = stub.GetAccount(pb.Empty(), metadata=METADATA)
    print(f"account {account.login} on {account.server}: balance={account.balance} equity={account.equity}")

    rates = stub.GetRates(
        pb.RatesRequest(symbol="EURUSD", timeframe="M5", count=3), metadata=METADATA
    )
    for rate in rates.rates:
        print("rate", rate.time, rate.open, rate.high, rate.low, rate.close)

    print("streaming 3 account snapshots (gRPC StreamAccount)")
    stream = stub.StreamAccount(pb.StreamAccountRequest(interval_ms=500), metadata=METADATA)
    for i, snapshot in enumerate(stream):
        print(f"  snapshot {i + 1}: balance={snapshot.balance} equity={snapshot.equity} profit={snapshot.profit}")
        if i >= 2:
            stream.cancel()
            break

    print("streaming up to 5 ticks within 30s (gRPC StreamTicks)")
    stream = stub.StreamTicks(
        pb.StreamTicksRequest(symbols=["EURUSD"], mode="all", interval_ms=100),
        metadata=METADATA,
        timeout=30,
    )
    try:
        for i, tick in enumerate(stream):
            print(f"  tick {tick.symbol} {tick.time_msc} bid={tick.bid} ask={tick.ask}")
            if i >= 4:
                stream.cancel()
                break
    except grpc.RpcError as exc:
        if exc.code() not in (grpc.StatusCode.CANCELLED, grpc.StatusCode.DEADLINE_EXCEEDED):
            raise
        print("  (stream ended:", exc.code().name, "- quiet market is fine)")

    if do_trade:
        print("placing 0.01 EURUSD market buy (gRPC MarketOrder)")
        result = stub.MarketOrder(
            pb.MarketOrderRequest(symbol="EURUSD", side="buy", volume=0.01, comment="grpc verified"),
            metadata=METADATA,
        )
        print(f"  retcode={result.retcode} deal={result.deal} order={result.order} price={result.price}")
        if result.retcode == 10009:
            positions = stub.GetPositions(pb.PositionsRequest(), metadata=METADATA)
            for position in positions.positions:
                if position.comment == "grpc verified":
                    closed = stub.ClosePosition(
                        pb.CloseRequest(ticket=position.ticket), metadata=METADATA
                    )
                    print(f"  closed ticket {position.ticket}: retcode={closed.retcode}")

    print("grpc client done")


if __name__ == "__main__":
    main()
