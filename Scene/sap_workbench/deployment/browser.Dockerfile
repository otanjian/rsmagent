# syntax=docker/dockerfile:1
ARG TARGETARCH
FROM debian:bookworm-20260918-slim@sha256:f3034a6ec3c1205360777c4aae76234998866ad18806ae62b63a3f84ccad782b AS base-amd64
FROM debian:bookworm-20260918-slim@sha256:0c8bbb8e987a035fe1d9704eb2e571b7e9a836e1caa46345290674b45b69e417 AS base-arm64
FROM base-${TARGETARCH} AS browser
ARG TARGETARCH

# Keep package resolution in the same Debian archive snapshot. APT retains
# signature checking; only the historical Release expiry check is disabled.
RUN rm -f /etc/apt/sources.list.d/debian.sources && \
    printf '%s\n' \
      'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20261003T000000Z bookworm main' \
      'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/20261003T000000Z bookworm-security main' \
      > /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates python3 xauth x11-utils \
      xvfb=2:21.1.7-3+deb12u13 x11vnc=0.9.16-9 \
      fonts-liberation fonts-noto-cjk \
      libasound2 libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 \
      libcairo2 libcups2 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0 \
      libgtk-3-0 libnspr4 libnss3 libpango-1.0-0 \
      libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
      libxkbcommon0 libxrandr2 libxshmfence1 && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --gid 10001 browser && useradd --uid 10001 --gid 10001 --create-home browser

WORKDIR /opt/sap-browser
COPY browser-versions.json fetch-browser-assets.py entrypoint.py healthcheck.py /opt/sap-browser/
RUN python3 -B /opt/sap-browser/fetch-browser-assets.py "${TARGETARCH}" && \
    /opt/chrome/chrome --version | grep -F '154.0.8037.92' && \
    test -f /opt/novnc/vnc.html && /opt/websockify/run --help > /tmp/websockify-help && \
    grep -F -- '--file-only' /tmp/websockify-help && rm -f /tmp/websockify-help

ENV DISPLAY=:99 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER browser
WORKDIR /home/browser
EXPOSE 6080
HEALTHCHECK --interval=15s --timeout=6s --start-period=90s --retries=3 \
  CMD ["python3", "-B", "/opt/sap-browser/healthcheck.py"]
ENTRYPOINT ["python3", "-B", "/opt/sap-browser/entrypoint.py"]
