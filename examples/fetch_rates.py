"""Fetch account info and candles from the bridge using only the stdlib.

Usage: python examples/fetch_rates.py
Environment: BRIDGE_URL (default http://localhost:8001), BRIDGE_API_KEY
"""

import json
import os
import urllib.request

BASE_URL = os.getenv("BRIDGE_URL", "http://localhost:8001")
API_KEY = os.getenv("BRIDGE_API_KEY", "")


def get(path):
    request = urllib.request.Request(BASE_URL + path)
    if API_KEY:
        request.add_header("X-API-Key", API_KEY)
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def main():
    health = get("/health")
    print("health:", health)
    if not health["connected"]:
        print("Bridge is up but not connected to a terminal yet")
        return

    account = get("/account")
    print("account:", account["login"], account["server"], "balance:", account["balance"])

    data = get("/rates/EURUSD?timeframe=M5&count=10")
    print(f"last {data['count']} EURUSD M5 bars:")
    for bar in data["rates"]:
        print(bar["time"], bar["open"], bar["high"], bar["low"], bar["close"])


if __name__ == "__main__":
    main()
