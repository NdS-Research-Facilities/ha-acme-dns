# https://developers.home-assistant.io/docs/apps/configuration#app-dockerfile
ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

# All three are pure Python, so there is nothing to compile and the same
# versions install cleanly on both aarch64 and amd64.
RUN apk add --no-cache python3 py3-pip \
    && pip3 install --no-cache-dir \
        dnslib==0.9.26 \
        flask==3.1.3 \
        waitress==3.0.2 \
    && rm -rf /root/.cache

# Copy root filesystem
COPY rootfs /

LABEL \
    org.opencontainers.image.title="Home Assistant App: DNS-01 letsencrypt challenge server" \
    org.opencontainers.image.description="acme-dns compatible server and account manager for the Let's Encrypt app" \
    org.opencontainers.image.source="https://github.com/NdS-Research-Facilities/ha-acme-dns" \
    org.opencontainers.image.licenses="MIT"
