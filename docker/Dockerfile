# Headless MetaTrader 5 with an HTTP/WebSocket bridge
#
# Replicates the official MetaQuotes Linux installation method (WineHQ +
# mt5setup.exe) inside a container, with a virtual display instead of a
# desktop. Build context is the repository root:
#   docker compose -f docker/compose.yaml up --build
#
# The terminal, Wine and the bridge all run as an unprivileged user. Wine
# runs arbitrary Windows code by design, so root inside the container is
# not a boundary worth relying on.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    WINEPREFIX=/opt/wineprefix \
    WINEARCH=win64 \
    WINEDEBUG=-all \
    WINEDLLOVERRIDES="mscoree=d;mshtml=d" \
    PYTHON_VERSION=3.13.9 \
    MT5_USER=mt5 \
    HOME=/home/mt5

# Base tools, virtual display stack, and the official WineHQ build,
# the same compatibility layer the MetaQuotes mt5linux.sh script installs
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg2 cabextract unzip procps \
        xvfb xauth x11vnc novnc websockify \
    && mkdir -pm755 /etc/apt/keyrings \
    && wget -q -O /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key \
    && wget -q -NP /etc/apt/sources.list.d/ https://dl.winehq.org/wine-builds/debian/dists/bookworm/winehq-bookworm.sources \
    && dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --install-recommends winehq-stable \
    && curl -fsSL -o /usr/local/bin/winetricks https://raw.githubusercontent.com/Winetricks/winetricks/master/src/winetricks \
    && chmod +x /usr/local/bin/winetricks \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# The account everything runs as. Created before the prefix is built so
# the Wine prefix and the bridge files can be owned by it from the start.
RUN useradd --create-home --home-dir /home/mt5 --shell /bin/bash --uid 1000 ${MT5_USER} \
    && mkdir -p /opt/wineprefix "/opt/wineprefix/drive_c/Program Files/MetaTrader 5" \
               /opt/bridge /var/log/mt5 /data \
    && chown -R ${MT5_USER}:${MT5_USER} /opt/wineprefix /opt/bridge /var/log/mt5 /data /home/mt5

USER ${MT5_USER}

# Initialize the 64-bit prefix and install the VC++ runtime needed by the
# MetaTrader5 package's compiled extension
RUN xvfb-run --auto-servernum wineboot --init && wineserver -w \
    && xvfb-run --auto-servernum winetricks --unattended vcrun2022 && wineserver -w

# Windows Python inside the prefix; the official MetaTrader5 package ships
# Windows-only wheels so it must run under Wine
RUN curl -fsSL -o /tmp/python-setup.exe \
        https://www.python.org/ftp/python/${PYTHON_VERSION}/python-${PYTHON_VERSION}-amd64.exe \
    && xvfb-run --auto-servernum wine /tmp/python-setup.exe /quiet \
        InstallAllUsers=1 PrependPath=1 Include_doc=0 Include_tcltk=0 Include_test=0 \
    && wineserver -w \
    && rm /tmp/python-setup.exe

WORKDIR /opt/bridge
COPY --chown=mt5:mt5 bridge/requirements.txt ./requirements.txt
RUN xvfb-run --auto-servernum wine python -m pip install --no-cache-dir -r requirements.txt \
    && wineserver -w

COPY --chown=mt5:mt5 bridge/app.py bridge/core.py bridge/grpc_server.py bridge/keys.py \
     bridge/security.py bridge/mt5bridge_pb2.py bridge/mt5bridge_pb2_grpc.py ./
COPY --chown=mt5:mt5 bridge/portal ./portal
COPY --chown=mt5:mt5 docker/mt5cfg.ini docker/start.sh /opt/

USER root
RUN chmod 0755 /opt/start.sh && chown ${MT5_USER}:${MT5_USER} /opt/start.sh /opt/mt5cfg.ini
USER ${MT5_USER}

# 8001 REST/WebSocket, 8002 gRPC, 6080 noVNC in the browser. Raw VNC on
# 5900 is deliberately not exposed; reach it through noVNC or an SSH
# tunnel rather than publishing an unencrypted remote desktop.
EXPOSE 8001 8002 6080

HEALTHCHECK --interval=30s --timeout=5s --retries=5 --start-period=180s \
    CMD curl -fsS http://127.0.0.1:${BRIDGE_PORT:-8001}/health || exit 1

ENTRYPOINT ["/opt/start.sh"]
