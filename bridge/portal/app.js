/* MT5 Bridge console.

   Two rules hold throughout this file:
   - Nothing from the server is ever assigned to innerHTML. Labels,
     symbols and order comments are attacker-influenced strings, so every
     one of them is written with textContent into an element built here.
   - The API key lives in sessionStorage, not localStorage, so it does not
     outlive the browser session on a shared machine.
*/
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const KEY_STORE = "mt5bridge_key";

  const keyInput = $("api-key");
  keyInput.value = sessionStorage.getItem(KEY_STORE) || "";

  const apiKey = () => keyInput.value.trim();

  const headers = () => {
    const base = { "Content-Type": "application/json" };
    if (apiKey()) base["X-API-Key"] = apiKey();
    return base;
  };

  const wsUrl = (path, params = {}) => {
    const url = new URL(path, location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    Object.entries(params).forEach(([name, value]) => url.searchParams.set(name, value));
    if (apiKey()) url.searchParams.set("api_key", apiKey());
    return url.toString();
  };

  const fmt = (value, digits = 2) =>
    value === undefined || value === null
      ? "--"
      : Number(value).toLocaleString("en-US", {
          minimumFractionDigits: digits,
          maximumFractionDigits: digits,
        });

  const setDot = (element, state) => {
    element.className = state ? `dot dot-${state}` : "dot";
  };

  const flash = (element, up) => {
    element.classList.remove("flash-up", "flash-down");
    void element.offsetWidth;
    element.classList.add(up ? "flash-up" : "flash-down");
  };

  /** Build a table cell with text only; never markup. */
  const cell = (text, style) => {
    const td = document.createElement("td");
    td.textContent = text === undefined || text === null ? "" : String(text);
    if (style) td.setAttribute("style", style);
    return td;
  };

  const emptyRow = (body, span, text) => {
    body.replaceChildren();
    const row = document.createElement("tr");
    const td = cell(text);
    td.colSpan = span;
    td.className = "empty";
    row.appendChild(td);
    body.appendChild(row);
  };

  let session = { admin: false, authRequired: true, readOnly: false };

  // ---------------------------------------------------------------- health

  async function pollHealth() {
    try {
      const response = await fetch("/health", { cache: "no-store" });
      const data = await response.json();
      setDot($("conn-led"), data.connected ? "on" : "off");
      $("conn-text").textContent = data.connected
        ? "terminal connected"
        : "terminal disconnected";
    } catch {
      setDot($("conn-led"), "off");
      $("conn-text").textContent = "bridge unreachable";
    }
  }

  // -------------------------------------------------------------- session

  async function loadSession() {
    const badge = $("mode-badge");
    try {
      const response = await fetch("/portal/session", { headers: headers(), cache: "no-store" });
      if (response.status === 401 || response.status === 429) {
        session = { admin: false, authRequired: true, readOnly: false };
        badge.textContent = "key required";
        badge.className = "badge badge-warn";
        $("keys-locked").classList.remove("hidden");
        $("keys-panel").classList.add("hidden");
        return;
      }
      const data = await response.json();
      session = {
        admin: Boolean(data.admin),
        authRequired: Boolean(data.auth_required),
        readOnly: Boolean(data.read_only),
      };
      badge.textContent = session.authRequired
        ? session.admin
          ? "admin key"
          : "standard key"
        : "open mode";
      badge.className = "badge " + (session.authRequired ? "badge-ok" : "badge-warn");
      $("trade-badge").textContent = session.readOnly ? "read-only" : "live orders";
      $("o-submit").disabled = session.readOnly;
      $("keys-locked").classList.toggle("hidden", session.admin);
      $("keys-panel").classList.toggle("hidden", !session.admin);
      if (session.admin) loadKeys();
    } catch {
      badge.textContent = "offline";
      badge.className = "badge";
    }
  }

  // ------------------------------------------------------- account stream

  let accountSocket = null;
  let accountUpdates = 0;
  const lastAccount = {};

  function stopAccount() {
    if (accountSocket) {
      accountSocket.onclose = null;
      accountSocket.close();
      accountSocket = null;
    }
    setDot($("acct-led"), null);
    $("acct-meta").textContent = "disconnected";
    $("src-ws").className = "btn btn-sm";
    $("src-grpc").className = "btn btn-sm";
  }

  function startAccount(source) {
    stopAccount();
    accountUpdates = 0;
    const path = source === "grpc" ? "/portal/grpc/account" : "/ws/account";
    $(source === "grpc" ? "src-grpc" : "src-ws").className = "btn btn-sm btn-primary";
    accountSocket = new WebSocket(wsUrl(path, { interval_ms: 500 }));
    accountSocket.onopen = () => {
      setDot($("acct-led"), "on");
      $("acct-meta").textContent = source.toUpperCase() + " streaming";
    };
    accountSocket.onclose = (event) => {
      setDot($("acct-led"), "off");
      if (event.code === 4401) {
        $("acct-meta").textContent = "rejected: invalid key";
        return;
      }
      $("acct-meta").textContent = "reconnecting...";
      setTimeout(() => startAccount(source), 3000);
    };
    accountSocket.onmessage = (event) => {
      const data = JSON.parse(event.data);
      accountUpdates += 1;
      $("acct-meta").textContent =
        (source === "grpc" ? "gRPC StreamAccount" : "WS /ws/account") +
        " | " + accountUpdates + " updates";
      const fields = {
        "m-balance": data.balance,
        "m-equity": data.equity,
        "m-profit": data.profit,
        "m-margin-free": data.margin_free,
      };
      for (const [id, value] of Object.entries(fields)) {
        const element = $(id);
        const previous = lastAccount[id];
        element.textContent = fmt(value);
        if (id === "m-profit") {
          element.style.color = value > 0 ? "var(--up)" : value < 0 ? "var(--down)" : "";
        }
        if (previous !== undefined && previous !== value) {
          flash(element.parentElement, value > previous);
        }
        lastAccount[id] = value;
      }
      if (data.login) {
        $("acct-account").textContent =
          "account " + data.login + " @ " + data.server + " | " + data.currency +
          (data.leverage ? " | 1:" + data.leverage : "");
      }
    };
  }

  // ---------------------------------------------------------- tick stream

  let tickSocket = null;
  let tickWanted = false;
  let tickCount = 0;
  let tickTimes = [];
  const lastBid = {};

  function stopTicks() {
    tickWanted = false;
    if (tickSocket) {
      tickSocket.onclose = null;
      tickSocket.close();
      tickSocket = null;
    }
    setDot($("tick-led"), null);
    $("tick-meta").textContent = "disconnected";
    $("tick-toggle").textContent = "Connect";
  }

  function connectTicks() {
    const symbols = $("tick-symbols").value.toUpperCase().replace(/\s/g, "");
    if (!symbols) return;
    const source = $("tick-source").value;
    const path = source === "grpc" ? "/portal/grpc/ticks" : "/ws/ticks";
    const label = source === "grpc" ? "gRPC StreamTicks" : "WS /ws/ticks";
    tickSocket = new WebSocket(
      wsUrl(path, { symbols, mode: $("tick-mode").value, interval_ms: 100 })
    );
    $("tick-toggle").textContent = "Disconnect";
    tickSocket.onopen = () => {
      setDot($("tick-led"), "on");
      $("tick-meta").textContent = label + " | " + symbols;
    };
    tickSocket.onclose = (event) => {
      setDot($("tick-led"), "off");
      tickSocket = null;
      if (event.code === 4401) {
        $("tick-meta").textContent = "rejected: invalid key";
        tickWanted = false;
        $("tick-toggle").textContent = "Connect";
        return;
      }
      if (event.code === 4400) {
        $("tick-meta").textContent = "rejected: bad symbol list";
        tickWanted = false;
        $("tick-toggle").textContent = "Connect";
        return;
      }
      if (tickWanted) {
        $("tick-meta").textContent = "reconnecting...";
        setTimeout(() => {
          if (tickWanted && !tickSocket) connectTicks();
        }, 3000);
      }
    };
    tickSocket.onmessage = (event) => {
      const tick = JSON.parse(event.data);
      tickCount += 1;
      const now = Date.now();
      tickTimes.push(now);
      while (tickTimes.length && tickTimes[0] < now - 5000) tickTimes.shift();
      $("tick-meta").textContent =
        label + " | " + tickCount + " ticks | " + (tickTimes.length / 5).toFixed(1) + "/s";

      const body = $("tick-body");
      const up = lastBid[tick.symbol] === undefined ? true : tick.bid >= lastBid[tick.symbol];
      lastBid[tick.symbol] = tick.bid;
      const time = new Date(tick.time_msc).toISOString().slice(11, 23);
      const spread = (tick.ask - tick.bid).toFixed(5);

      const row = document.createElement("tr");
      row.appendChild(cell(time, "color:var(--ink-faint)"));
      row.appendChild(cell(tick.symbol));
      row.appendChild(cell(tick.bid, "color:" + (up ? "var(--up)" : "var(--down)")));
      row.appendChild(cell(tick.ask));
      row.appendChild(cell(spread, "color:var(--ink-faint)"));
      flash(row, up);
      body.prepend(row);
      while (body.children.length > 60) body.removeChild(body.lastChild);
    };
  }

  // ------------------------------------------------------ positions table

  let positionSocket = null;

  function startPositions() {
    if (positionSocket) {
      positionSocket.onclose = null;
      positionSocket.close();
    }
    positionSocket = new WebSocket(wsUrl("/ws/positions", { interval_ms: 700 }));
    positionSocket.onopen = () => setDot($("pos-led"), "on");
    positionSocket.onclose = (event) => {
      setDot($("pos-led"), "off");
      positionSocket = null;
      if (event.code !== 4401) setTimeout(startPositions, 4000);
    };
    positionSocket.onmessage = (event) => {
      const rows = JSON.parse(event.data).positions || [];
      const body = $("pos-body");
      if (!rows.length) {
        emptyRow(body, 7, "no open positions");
        return;
      }
      body.replaceChildren();
      for (const position of rows) {
        const side = position.type === 0 ? "BUY" : "SELL";
        const row = document.createElement("tr");
        row.appendChild(cell(position.ticket, "color:var(--ink-faint)"));
        row.appendChild(cell(position.symbol));
        row.appendChild(
          cell(side, "font-weight:500;color:" + (position.type === 0 ? "var(--buy)" : "var(--sell)"))
        );
        row.appendChild(cell(position.volume));
        row.appendChild(cell(position.price_open));
        row.appendChild(
          cell(fmt(position.profit), "color:" + (position.profit >= 0 ? "var(--up)" : "var(--down)"))
        );

        const actions = document.createElement("td");
        const close = document.createElement("button");
        close.className = "btn btn-sm";
        close.type = "button";
        close.textContent = "Close";
        close.disabled = session.readOnly;
        close.addEventListener("click", async () => {
          close.disabled = true;
          try {
            const response = await fetch("/positions/" + encodeURIComponent(position.ticket) + "/close", {
              method: "POST",
              headers: headers(),
            });
            const data = await response.json();
            $("order-result").textContent = response.ok
              ? "closed " + position.ticket + " retcode " + data.retcode
              : "close failed: " + JSON.stringify(data.detail);
          } catch (error) {
            $("order-result").textContent = "close failed: " + error;
            close.disabled = false;
          }
        });
        actions.appendChild(close);
        row.appendChild(actions);
        body.appendChild(row);
      }
    };
  }

  // -------------------------------------------------------- order ticket

  let side = "buy";

  const paintSide = () => {
    $("o-buy").className = "btn " + (side === "buy" ? "btn-buy" : "");
    $("o-sell").className = "btn " + (side === "sell" ? "btn-sell" : "");
  };

  $("o-buy").addEventListener("click", () => { side = "buy"; paintSide(); });
  $("o-sell").addEventListener("click", () => { side = "sell"; paintSide(); });

  $("order-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const result = $("order-result");
    const body = {
      symbol: $("o-symbol").value.toUpperCase().trim(),
      side,
      volume: parseFloat($("o-volume").value),
      comment: "portal",
    };
    if ($("o-sl").value) body.sl = parseFloat($("o-sl").value);
    if ($("o-tp").value) body.tp = parseFloat($("o-tp").value);
    result.textContent = "transmitting...";
    try {
      const response = await fetch("/orders/market", {
        method: "POST",
        headers: headers(),
        body: JSON.stringify(body),
      });
      const data = await response.json();
      result.replaceChildren();
      const tag = document.createElement("span");
      if (response.ok && data.retcode === 10009) {
        tag.className = "ok";
        tag.textContent = "Executed";
        result.append(tag, " deal " + data.deal + " @ " + data.price);
      } else {
        tag.className = "bad";
        tag.textContent = "Rejected";
        const detail = data.detail !== undefined
          ? data.detail
          : { retcode: data.retcode, comment: data.comment };
        result.append(tag, " " + JSON.stringify(detail));
      }
    } catch (error) {
      result.textContent = "request failed: " + error;
    }
  });

  // ------------------------------------------------------------- API keys

  async function loadKeys() {
    const badge = $("keys-mode");
    try {
      const response = await fetch("/portal/keys", { headers: headers(), cache: "no-store" });
      if (!response.ok) {
        badge.textContent = "admin key required";
        badge.className = "badge badge-warn";
        return;
      }
      const data = await response.json();
      badge.textContent = data.required ? "auth required" : "open mode";
      badge.className = "badge " + (data.required ? "badge-ok" : "badge-warn");

      const body = $("keys-body");
      if (!data.keys.length) {
        emptyRow(body, 4, "no keys issued yet");
        return;
      }
      body.replaceChildren();
      for (const entry of data.keys) {
        const row = document.createElement("tr");
        row.appendChild(cell(entry.label));
        const masked = cell(entry.masked, "color:var(--ink-soft)");
        masked.className = "mono";
        row.appendChild(masked);
        row.appendChild(cell(entry.admin ? "admin" : "standard"));

        const actions = document.createElement("td");
        const revoke = document.createElement("button");
        revoke.className = "btn btn-sm";
        revoke.type = "button";
        revoke.textContent = "Revoke";
        revoke.addEventListener("click", async () => {
          revoke.disabled = true;
          await fetch("/portal/keys/" + encodeURIComponent(entry.id), {
            method: "DELETE",
            headers: headers(),
          });
          loadKeys();
        });
        actions.appendChild(revoke);
        row.appendChild(actions);
        body.appendChild(row);
      }
    } catch {
      /* bridge down; the health dot already shows it */
    }
  }

  $("key-create").addEventListener("click", async () => {
    const response = await fetch("/portal/keys", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ label: $("key-label").value || "unnamed", admin: false }),
    });
    const data = await response.json();
    if (!response.ok) {
      $("order-result").textContent = "key creation failed: " + JSON.stringify(data.detail);
      return;
    }
    $("key-new-value").textContent = data.key;
    $("key-new").classList.remove("hidden");
    loadKeys();
  });

  $("key-copy").addEventListener("click", () => {
    navigator.clipboard.writeText($("key-new-value").textContent);
  });

  $("key-use").addEventListener("click", () => {
    keyInput.value = $("key-new-value").textContent;
    applyKey();
  });

  // ----------------------------------------------------------- lifecycle

  function restartStreams() {
    stopAccount();
    stopTicks();
    if (positionSocket) {
      positionSocket.onclose = null;
      positionSocket.close();
      positionSocket = null;
    }
    startAccount("ws");
    startPositions();
    tickWanted = true;
    tickCount = 0;
    tickTimes = [];
    connectTicks();
  }

  function applyKey() {
    sessionStorage.setItem(KEY_STORE, apiKey());
    loadSession().then(restartStreams);
  }

  $("key-apply").addEventListener("click", applyKey);
  keyInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") applyKey();
  });

  $("src-ws").addEventListener("click", () => startAccount("ws"));
  $("src-grpc").addEventListener("click", () => startAccount("grpc"));
  $("src-stop").addEventListener("click", stopAccount);

  $("tick-toggle").addEventListener("click", () => {
    if (tickWanted || tickSocket) {
      stopTicks();
      return;
    }
    tickWanted = true;
    tickCount = 0;
    tickTimes = [];
    connectTicks();
  });

  paintSide();
  emptyRow($("keys-body"), 4, "no keys issued yet");
  pollHealth();
  setInterval(pollHealth, 5000);
  loadSession().then(restartStreams);
})();
