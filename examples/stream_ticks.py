"""Stream live ticks from the bridge WebSocket.

Usage: python examples/stream_ticks.py
Environment: BRIDGE_URL (default http://localhost:8001), BRIDGE_API_KEY
"""

import os

from websockets.sync.client import connect

BASE_URL = os.getenv("BRIDGE_URL", "http://localhost:8001")
API_KEY = os.getenv("BRIDGE_API_KEY", "")


def main():
    ws_url = BASE_URL.replace("http", "ws", 1) + "/ws/ticks?symbols=EURUSD,GBPUSD"
    if API_KEY:
        ws_url += f"&api_key={API_KEY}"
    with connect(ws_url) as ws:
        print("connected, streaming 50 ticks")
        for _ in range(50):
            print(ws.recv())


if __name__ == "__main__":
    main()
