FROM python:3.13.12-slim

WORKDIR /app

# renovate: datasource=github-tags depName=pip packageName=pypa/pip
ENV PIP_VERSION=26.0.1
RUN python -m pip install --root-user-action ignore --upgrade pip==${PIP_VERSION}

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

RUN python -m camoufox fetch

# Install browser dependencies manually (playwright install-deps doesn't support Debian Trixie)
# Also includes Xvfb + x11vnc + noVNC for live debug viewing (activated by CAMOUFOX_DEBUG=1)
RUN --mount=type=cache,target=/var/cache/apt \
    apt-get update && apt-get install -y --no-install-recommends \
    libasound2t64 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgdk-pixbuf-2.0-0 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    libxshmfence1 \
    fonts-noto-color-emoji \
    fonts-unifont \
    xvfb \
    x11vnc \
    novnc \
    websockify \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh server.py ./

EXPOSE 8000 6080

ENTRYPOINT ["./entrypoint.sh"]
