#!/bin/bash
# Headless MetaTrader 5 launcher: virtual display, VNC access, the terminal
# logged in via its start config, and the HTTP bridge attached to it.
#
# The terminal handles the broker login itself (Login/Password/Server in
# the [Common] section of the start config); the bridge then attaches
# without credentials. Passing credentials through mt5.initialize() under
# Wine fails with IPC timeouts when the terminal has no account yet.
#
# Nothing here prints a credential. The start config holds the broker
# password, so it is written with restrictive permissions and never
# echoed to the log.
set -u

export DISPLAY=:99
export WINEPREFIX=${WINEPREFIX:-/opt/wineprefix}
export WINEDEBUG=${WINEDEBUG:--all}

SCREEN_RESOLUTION=${SCREEN_RESOLUTION:-1280x800x24}
LOG_DIR=${LOG_DIR:-/var/log/mt5}
MT5_DOWNLOAD_URL=${MT5_DOWNLOAD_URL:-https://download.mql5.com/cdn/web/metaquotes.ltd/mt5/mt5setup.exe}
# The VNC desktop is a full remote session on the terminal that holds the
# broker login, so it stays on loopback unless a password is set
VNC_BIND=${VNC_BIND:-127.0.0.1}
NOVNC_PORT=${NOVNC_PORT:-6080}

umask 077
mkdir -p "$LOG_DIR"
# Fresh logs each boot; previous boot kept as .old
for name in terminal bridge x11vnc novnc; do
  [ -f "$LOG_DIR/$name.log" ] && mv -f "$LOG_DIR/$name.log" "$LOG_DIR/$name.log.old"
done
rm -f /tmp/.X99-lock

# -nolisten tcp: the X server is for Xvfb clients in this container only
Xvfb :99 -ac -nolisten tcp -screen 0 "$SCREEN_RESOLUTION" &

if [ -n "${VNC_PASSWORD:-}" ]; then
  if [ ${#VNC_PASSWORD} -lt 8 ]; then
    echo "ERROR: VNC_PASSWORD must be at least 8 characters"
    exit 1
  fi
  # x11vnc stores the password in a file rather than taking it on the
  # command line, where it would be visible to every process in the
  # container through /proc
  VNC_PASSWD_FILE="$HOME/.vncpasswd"
  x11vnc -storepasswd "$VNC_PASSWORD" "$VNC_PASSWD_FILE" >/dev/null 2>&1
  unset VNC_PASSWORD
  x11vnc -display :99 -forever -shared -localhost -rfbport 5900 \
         -rfbauth "$VNC_PASSWD_FILE" >"$LOG_DIR/x11vnc.log" 2>&1 &
  websockify --web /usr/share/novnc "${NOVNC_PORT}" localhost:5900 >"$LOG_DIR/novnc.log" 2>&1 &
  echo "noVNC desktop on :${NOVNC_PORT}, password protected"
else
  # No password: bind the desktop to loopback inside the container so it
  # cannot be published by accident. Reach it with
  #   docker exec, or: ssh -L 6080:127.0.0.1:6080 <host>
  x11vnc -display :99 -forever -shared -localhost -rfbport 5900 -nopw \
         >"$LOG_DIR/x11vnc.log" 2>&1 &
  websockify --web /usr/share/novnc "${VNC_BIND}:${NOVNC_PORT}" localhost:5900 \
         >"$LOG_DIR/novnc.log" 2>&1 &
  echo "noVNC desktop bound to ${VNC_BIND}:${NOVNC_PORT} with no password."
  echo "Set VNC_PASSWORD to reach it from outside the container."
fi

# First boot: install MetaTrader 5 into the persistent prefix, the same way
# the official MetaQuotes Linux script does it
if [ ! -e "$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe" ]; then
  echo "MetaTrader 5 not found, downloading installer"
  curl -fsSL -o /tmp/mt5setup.exe "$MT5_DOWNLOAD_URL"
  wine /tmp/mt5setup.exe /auto
  echo "Waiting for the installer to finish"
  sleep 30
  wine taskkill /IM terminal64.exe /F >/dev/null 2>&1 || true
  rm -f /tmp/mt5setup.exe
fi

TERMINAL_EXE=$(find "$WINEPREFIX/drive_c" -name terminal64.exe -print -quit)
if [ -z "$TERMINAL_EXE" ]; then
  echo "ERROR: terminal64.exe not found after installation"
  exit 1
fi
TERMINAL_DIR=$(dirname "$TERMINAL_EXE")
echo "Using terminal at $TERMINAL_DIR"

# Build the start config fresh each boot: base settings plus broker login
# when credentials are provided via environment
RUNTIME_CFG="$TERMINAL_DIR/mt5cfg.ini"
cp /opt/mt5cfg.ini "$RUNTIME_CFG"
chmod 600 "$RUNTIME_CFG"
if [ -n "${MT5_LOGIN:-}" ]; then
  sed -i "/^\[Common\]/a Login=${MT5_LOGIN}\nPassword=${MT5_PASSWORD:-}\nServer=${MT5_SERVER:-}" "$RUNTIME_CFG"
  echo "Terminal will auto-login as ${MT5_LOGIN} on ${MT5_SERVER:-unspecified server}"
else
  echo "No MT5_LOGIN provided; log in once through the noVNC desktop"
fi

start_terminal() {
  (cd "$TERMINAL_DIR" && wine terminal64.exe /portable /config:mt5cfg.ini >>"$LOG_DIR/terminal.log" 2>&1 &)
}

# The bridge attaches to the running, already-logged-in terminal; strip the
# credentials so initialize() does not attempt its own login, and so they
# are not present in the bridge process environment at all
start_bridge() {
  (cd /opt/bridge && env -u MT5_LOGIN -u MT5_PASSWORD -u MT5_SERVER wine python app.py >>"$LOG_DIR/bridge.log" 2>&1 &)
}

start_terminal
echo "Waiting for the terminal to start and log in"
sleep 25

export MT5_PORTABLE=1
MT5_PATH=$(winepath -w "$TERMINAL_EXE")
export MT5_PATH
start_bridge
echo "Bridge starting on port ${BRIDGE_PORT:-8001}"
# Wine processes are slow to appear; generous grace before the watchdog
sleep 30

# Watchdog: keep both processes alive
while true; do
  if ! pgrep -f terminal64.exe >/dev/null; then
    echo "Terminal process died, restarting"
    start_terminal
    sleep 25
  fi
  if ! pgrep -f "python.exe app.py" >/dev/null; then
    echo "Bridge process died, restarting"
    pkill -f "python.exe app.py" 2>/dev/null
    start_bridge
    sleep 30
  fi
  sleep 10
done
