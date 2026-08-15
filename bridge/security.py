"""Transport level protections shared by the HTTP and gRPC servers.

Everything here is dependency free and in process, sized for one bridge
in front of one terminal rather than for a fleet. Put a real reverse
proxy (TLS, WAF, global rate limits) in front when exposing the bridge
beyond a private network.

Environment
-----------
  BRIDGE_RATE_LIMIT          requests per minute per client (default 600, 0 disables)
  BRIDGE_AUTH_FAIL_LIMIT     failed auth attempts per window before lockout (default 10)
  BRIDGE_AUTH_FAIL_WINDOW    lockout window in seconds (default 300)
  BRIDGE_MAX_BODY_BYTES      largest accepted request body (default 262144)
  BRIDGE_ALLOWED_ORIGINS     comma separated browser origins; default same origin only
  BRIDGE_TRUSTED_PROXIES     comma separated proxy IPs allowed to set X-Forwarded-For
  BRIDGE_READ_ONLY           set to 1 to reject every order and trade endpoint
"""

import ipaddress
import logging
import os
import threading
import time
from collections import defaultdict, deque

logger = logging.getLogger("mt5bridge.security")

_lock = threading.Lock()
_requests = defaultdict(deque)
_auth_failures = defaultdict(deque)
_blocked_until = {}
# Bound the tables so a spray of forged source addresses cannot grow them
# without limit
MAX_TRACKED_CLIENTS = 4096


def _flag(name, default=""):
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes"}


def read_only():
    return _flag("BRIDGE_READ_ONLY")


def rate_limit():
    return int(os.getenv("BRIDGE_RATE_LIMIT", "600"))


def max_body_bytes():
    return int(os.getenv("BRIDGE_MAX_BODY_BYTES", str(256 * 1024)))


def _auth_fail_limit():
    return int(os.getenv("BRIDGE_AUTH_FAIL_LIMIT", "10"))


def _auth_fail_window():
    return int(os.getenv("BRIDGE_AUTH_FAIL_WINDOW", "300"))


def trusted_proxies():
    raw = os.getenv("BRIDGE_TRUSTED_PROXIES", "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def allowed_origins():
    """Browser origins allowed to open WebSockets or cross origin calls.

    Empty means same origin only, which is what a locally hosted portal
    needs and what stops a hostile page from opening a socket to a bridge
    running on the visitor's own machine.
    """
    raw = os.getenv("BRIDGE_ALLOWED_ORIGINS", "")
    return [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]


def client_ip(scope_client, headers):
    """Best effort client address, trusting X-Forwarded-For only from a
    configured proxy so a caller cannot forge its way past the limiter."""
    direct = scope_client[0] if scope_client else "unknown"
    proxies = trusted_proxies()
    if proxies and direct in proxies:
        forwarded = headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            try:
                ipaddress.ip_address(first)
                return first
            except ValueError:
                pass
    return direct


def _prune(table, key, window, now):
    bucket = table[key]
    cutoff = now - window
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if not bucket:
        table.pop(key, None)
    return bucket


def _evict_if_crowded(table):
    if len(table) > MAX_TRACKED_CLIENTS:
        table.clear()


def allow_request(ip):
    """False when this client has exceeded its per minute request budget."""
    limit = rate_limit()
    if limit <= 0:
        return True
    now = time.monotonic()
    with _lock:
        _evict_if_crowded(_requests)
        bucket = _prune(_requests, ip, 60.0, now)
        if len(bucket) >= limit:
            return False
        _requests[ip].append(now)
    return True


def is_locked_out(ip):
    """True while a client is serving a lockout for repeated bad keys."""
    now = time.monotonic()
    with _lock:
        until = _blocked_until.get(ip)
        if until is None:
            return False
        if until <= now:
            _blocked_until.pop(ip, None)
            return False
    return True


def record_auth_failure(ip):
    """Count a rejected key and lock the client out once it repeats.

    Without this an attacker could grind guesses against an endpoint that
    can place live orders.
    """
    limit = _auth_fail_limit()
    if limit <= 0:
        return
    window = _auth_fail_window()
    now = time.monotonic()
    with _lock:
        _evict_if_crowded(_auth_failures)
        bucket = _prune(_auth_failures, ip, float(window), now)
        bucket.append(now)
        _auth_failures[ip] = bucket
        if len(bucket) >= limit:
            _blocked_until[ip] = now + window
            _auth_failures.pop(ip, None)
            logger.warning("Locking out %s for %ss after %d failed key attempts", ip, window, limit)


def record_auth_success(ip):
    with _lock:
        _auth_failures.pop(ip, None)


def origin_allowed(origin, host_header):
    """True if a browser origin may talk to this bridge.

    A missing Origin means a non browser client (curl, a bot, a gRPC
    gateway), which is allowed because it is not subject to the ambient
    authority problem Origin exists to solve.
    """
    if not origin:
        return True
    origin = origin.rstrip("/")
    configured = allowed_origins()
    if configured:
        return origin in configured
    if not host_header:
        return False
    host = host_header.split(",")[0].strip()
    return origin in {f"http://{host}", f"https://{host}"}


SECURITY_HEADERS = {
    # No third party assets are loaded, so the policy can be strict; the
    # portal ships its own CSS and JS as separate files for this reason
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Cache-Control": "no-store",
}
