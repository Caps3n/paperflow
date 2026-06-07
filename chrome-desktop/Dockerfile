FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99

RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chrome
    chromium-browser \
    # Virtuelles Display
    xvfb \
    # VNC
    x11vnc \
    # Fenstermanager (minimal)
    openbox \
    # noVNC + WebSocket-Proxy
    novnc \
    websockify \
    # Sonstiges
    ca-certificates \
    fonts-liberation \
    libappindicator3-1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libgbm1 \
    libnspr4 \
    libnss3 \
    libxkbcommon0 \
    libxss1 \
    lsb-release \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Chrome-Profil und Daten persistent
VOLUME /chrome-data

COPY start.sh /start.sh
RUN chmod +x /start.sh

# noVNC Web-UI
EXPOSE 6080
# Chrome Remote-Debugging (CDP)
EXPOSE 9222

CMD ["/start.sh"]
