# Security

meta-docker puts an HTTP API in front of a logged-in MetaTrader 5
terminal. Anyone who can reach that API can read your account and place
orders on it. Treat the bridge the way you would treat your broker
password.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Email
**rajandran@marketcalls.in** with the details and a way to reproduce it.
You will get an acknowledgement within a few days.

## What the bridge does for you

| Protection | Behaviour |
| --- | --- |
| Authentication by default | Every endpoint except `GET /health` requires `X-API-Key`. If no key is configured, the bridge mints one on first boot and prints it once to the log. |
| Keys hashed at rest | Only a SHA-256 digest is stored. A stolen key file cannot be replayed. |
| Constant-time verification | Keys are compared with `hmac.compare_digest`, so response timing does not leak them. |
| Scoped keys | Ordinary keys read data and trade. Only an admin key can issue or revoke keys. |
| Brute-force lockout | 10 bad keys from one address in 5 minutes blocks that address for 5 minutes. |
| Rate limiting | 600 requests per minute per client by default (`BRIDGE_RATE_LIMIT`). |
| Loopback by default | `BRIDGE_HOST` defaults to `127.0.0.1`, and the compose file publishes ports on `127.0.0.1`. Exposing the bridge is a deliberate act. |
| Weak-key refusal | A `BRIDGE_API_KEY` shorter than 24 characters aborts startup. |
| Open-mode guard | `BRIDGE_ALLOW_OPEN` combined with a non-loopback bind aborts startup. |
| Read-only switch | `BRIDGE_READ_ONLY=1` serves market data and rejects every order. |
| Request validation | Symbols must match `^[A-Za-z0-9][A-Za-z0-9._#&-]{0,31}$`. The raw `order_send` passthrough accepts only documented trade-request fields, each type-checked. Bodies are capped at 256 KB. |
| Origin checks | WebSockets and cross-origin requests are refused unless the origin matches the host or `BRIDGE_ALLOWED_ORIGINS`. This stops a hostile web page from driving a bridge on the visitor's own machine. |
| Strict CSP | The portal loads no third-party script, stylesheet or font, so the bridge can send `default-src 'self'`. |
| No injected markup | The portal writes every server-supplied value with `textContent`; key labels and order comments cannot become script. |
| Unprivileged container | Wine, the terminal and the bridge run as uid 1000 with all capabilities dropped and `no-new-privileges`. |
| DLL imports off | `AllowDllImport=0` in the terminal config; an expert advisor cannot load native code. |

## What you must do

**Never publish port 8001 straight to the internet.** Even with a key,
the bridge speaks plain HTTP: the key travels in a header that anyone on
the path can read. Put it behind TLS.

A safe public deployment looks like this:

1. Keep `BIND_ADDR=127.0.0.1` in `docker/.env`.
2. Terminate TLS in nginx, Caddy or Traefik on the same host and proxy to
   `127.0.0.1:8001`.
3. Set `BRIDGE_TRUSTED_PROXIES` to the proxy's address so `X-Forwarded-For`
   is trusted from it and nowhere else.
4. Set `BRIDGE_ALLOWED_ORIGINS` to the public origin you serve the portal
   from, for example `https://mt5.example.com`.
5. Set a strong `BRIDGE_API_KEY`, or use the key minted on first boot.
6. Set `VNC_PASSWORD`, or leave the desktop on loopback and reach it
   through an SSH tunnel: `ssh -L 6080:127.0.0.1:6080 you@host`.
7. Start with `BRIDGE_READ_ONLY=1` and turn it off only once you trust
   the setup.

Better still, do not expose it at all. Run your bot on the same host and
talk to `127.0.0.1:8001`, or reach the bridge over a private network
(WireGuard, Tailscale, a VPC).

## Other things worth knowing

- **gRPC on 8002 is plaintext** unless you set `BRIDGE_GRPC_TLS_CERT` and
  `BRIDGE_GRPC_TLS_KEY`. The same rule applies: TLS or a private network.
- **Trade with a demo account first.** The order endpoints are real. A bug
  in your bot costs real money.
- **One bridge maps to one terminal and one account.** Run separate
  containers for separate accounts; do not share a key between them.
- **Rotate keys** you have pasted into a chat, a notebook or a CI log.
  Revoke in the portal and issue a new one; a lost key cannot be
  recovered, only replaced.
- **`BRIDGE_DOCS=1` and `BRIDGE_ALLOW_OPEN=1` are development switches.**
  Neither belongs on a host others can reach.

## Supported versions

Fixes go to the latest release. Please upgrade before reporting a bug
against an older tag.
