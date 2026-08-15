"""API key store for the bridge.

Security model
--------------
- Keys are never stored in clear text. The file keeps a SHA-256 digest
  plus a short display prefix, so a stolen key file cannot be replayed
  against the API.
- Verification is constant time (secrets.compare_digest) so a remote
  caller cannot recover a key byte by byte from response timing.
- Two scopes: ``admin`` keys may manage other keys through the portal,
  ordinary keys may only read data and trade.
- Fail closed. If no key exists the bridge mints an admin key on startup
  and logs it once, instead of serving an open API. Keyless operation is
  possible only when BRIDGE_ALLOW_OPEN is set explicitly, which is meant
  for a laptop, never for a public host.

Environment
-----------
  BRIDGE_KEYS_FILE   key store path (default api_keys.json beside this file)
  BRIDGE_API_KEY     optional master admin key, minimum 24 characters
  BRIDGE_ALLOW_OPEN  set to 1 to run with no authentication at all
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
import threading
import time

logger = logging.getLogger("mt5bridge.keys")

STORE_VERSION = 2
KEY_PREFIX = "mtb_"
# 32 bytes of entropy; brute force is not a consideration at this size
KEY_BYTES = 32
MIN_MASTER_KEY_LENGTH = 24

_lock = threading.RLock()
_cache = {"stamp": None, "data": None}


class WeakKeyError(RuntimeError):
    """BRIDGE_API_KEY was set to a value too short to be safe."""


def _path():
    return os.getenv(
        "BRIDGE_KEYS_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_keys.json"),
    )


def allow_open():
    return os.getenv("BRIDGE_ALLOW_OPEN", "").strip().lower() in {"1", "true", "yes"}


def master_key():
    """The optional BRIDGE_API_KEY, validated for length.

    A short master key is worse than no key at all because it invites a
    guessing attack against an endpoint that can place trades.
    """
    value = os.getenv("BRIDGE_API_KEY", "").strip()
    if not value:
        return None
    if len(value) < MIN_MASTER_KEY_LENGTH:
        raise WeakKeyError(
            f"BRIDGE_API_KEY must be at least {MIN_MASTER_KEY_LENGTH} characters; "
            "generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    return value


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _empty():
    return {"version": STORE_VERSION, "keys": []}


def _stamp(path):
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def _load():
    """Read the key store, cached on the file's mtime and size.

    Without the cache every authenticated request would hit the disk,
    which is both slow and a cheap denial of service lever.
    """
    path = _path()
    stamp = _stamp(path)
    with _lock:
        if stamp is not None and _cache["stamp"] == stamp and _cache["data"] is not None:
            return _cache["data"]
        data = _empty()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                parsed = json.load(handle)
            if isinstance(parsed, dict) and isinstance(parsed.get("keys"), list):
                data = _migrate(parsed)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            logger.error("Key store at %s is unreadable (%s); refusing all keys", path, exc)
        _cache["stamp"] = stamp
        _cache["data"] = data
        return data


def _migrate(parsed):
    """Upgrade a version 1 store, which held keys in clear text."""
    if parsed.get("version") == STORE_VERSION:
        return {"version": STORE_VERSION, "keys": [e for e in parsed["keys"] if "hash" in e]}
    entries = []
    for entry in parsed["keys"]:
        raw = entry.get("key")
        if not raw:
            continue
        entries.append(
            {
                "id": secrets.token_hex(6),
                "label": entry.get("label", "unnamed"),
                "hash": _digest(raw),
                "prefix": raw[: len(KEY_PREFIX) + 4],
                "admin": True,
                "created": entry.get("created", int(time.time())),
            }
        )
    upgraded = {"version": STORE_VERSION, "keys": entries}
    logger.warning("Upgraded the key store to hashed storage; existing keys still work")
    _write(upgraded)
    return upgraded


def _write(data):
    path = _path()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    try:
        # Owner read/write only; the digests are not secret but the file
        # also records which keys exist and when they were issued
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    os.replace(temporary, path)
    with _lock:
        _cache["stamp"] = _stamp(path)
        _cache["data"] = data


def create_key(label, admin=False):
    """Mint a key and return it once; only the digest is persisted."""
    key = KEY_PREFIX + secrets.token_hex(KEY_BYTES)
    entry = {
        "id": secrets.token_hex(6),
        "label": (label or "unnamed")[:64],
        "hash": _digest(key),
        "prefix": key[: len(KEY_PREFIX) + 4],
        "admin": bool(admin),
        "created": int(time.time()),
    }
    with _lock:
        data = _load()
        data = {"version": STORE_VERSION, "keys": list(data["keys"]) + [entry]}
        _write(data)
    return key, entry


def list_keys():
    return [
        {
            "id": entry["id"],
            "label": entry["label"],
            "masked": entry["prefix"] + "..." + entry["hash"][-4:],
            "admin": entry.get("admin", False),
            "created": entry["created"],
        }
        for entry in _load()["keys"]
    ]


def revoke_key(key_id):
    """Revoke exactly one key by its opaque id.

    The id is a random handle, not a slice of the key, so revocation
    neither leaks key material nor can be widened into "revoke every key
    starting with mtb_", which would silently disable authentication.
    """
    if not key_id or not isinstance(key_id, str):
        return False
    with _lock:
        data = _load()
        remaining = [entry for entry in data["keys"] if entry["id"] != key_id]
        if len(remaining) == len(data["keys"]):
            return False
        _write({"version": STORE_VERSION, "keys": remaining})
    return True


def verify(provided):
    """Return the scope for a presented key, or None if it is not valid.

    Result is ``{"admin": bool, "label": str}``. Every candidate is
    compared, so the work done is independent of which key matches.
    """
    if not provided or not isinstance(provided, str) or len(provided) > 512:
        return None
    master = master_key()
    if master is not None and hmac.compare_digest(provided, master):
        return {"admin": True, "label": "master"}
    presented = _digest(provided)
    found = None
    for entry in _load()["keys"]:
        if hmac.compare_digest(presented, entry["hash"]):
            found = {"admin": entry.get("admin", False), "label": entry["label"]}
    return found


def auth_disabled():
    """True only when the operator explicitly asked for an open API."""
    return allow_open()


def key_required():
    return not allow_open()


def check(provided):
    """True if a request carrying this key may reach the data API."""
    if allow_open():
        return True
    return verify(provided) is not None


def is_admin(provided):
    """True if this key may manage other keys."""
    if allow_open():
        return True
    scope = verify(provided)
    return bool(scope and scope["admin"])


def has_keys():
    return bool(_load()["keys"])


def ensure_bootstrap():
    """Guarantee the API is never unauthenticated by accident.

    Called once at startup. Returns a freshly minted admin key when one
    had to be created, otherwise None.
    """
    if allow_open():
        logger.warning(
            "BRIDGE_ALLOW_OPEN is set: the API accepts unauthenticated requests. "
            "Never use this on a host reachable from the internet."
        )
        return None
    master_key()  # raises WeakKeyError before anything is served
    if master_key() is not None or has_keys():
        return None
    key, _ = create_key("bootstrap-admin", admin=True)
    logger.warning(
        "No API key was configured, so one was generated and saved to %s.\n"
        "    Use this key in the X-API-Key header (shown only once):\n"
        "    %s",
        _path(),
        key,
    )
    return key
