FROM ghcr.io/astral-sh/uv:0.12.1 AS uv
FROM ubuntu:24.04

ARG TARGETARCH
ARG PUID=1000
ARG PGID=1000
# Pin the exact Box64 revision validated with Nighty on Raspberry Pi 5. Using a
# commit SHA keeps builds reproducible while avoiding the v0.4.2 Wine regression.
ARG BOX64_VERSION=c01888938978d85938205ac761327081d58d6ffd

ENV DEBIAN_FRONTEND=noninteractive

# Install the runtime dependencies for both supported image architectures.
# On arm64 Box64 is compiled once into the image; it is never rebuilt while the
# user is waiting for the container to start.
RUN set -eux; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in amd64|arm64) ;; *) echo "Unsupported Docker architecture: $arch" >&2; exit 1 ;; esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates curl wget tar xz-utils sudo python3 xvfb wine64 wine \
      libx11-6 libxext6 libxrender1 libxfixes3 libxrandr2 libxcomposite1 \
      libxi6 libxcursor1 libxinerama1 libxkbregistry0 libsdl2-2.0-0 libegl1 libcups2; \
    if [ "$arch" = arm64 ]; then \
      apt-get install -y --no-install-recommends build-essential cmake; \
      mkdir -p /tmp/box64; \
      curl -fSL --retry 3 "https://github.com/ptitSeb/box64/archive/${BOX64_VERSION}.tar.gz" \
        | tar -xz -C /tmp/box64 --strip-components=1; \
      cmake -S /tmp/box64 -B /tmp/box64/build -DARM_DYNAREC=ON -DCMAKE_BUILD_TYPE=Release; \
      cmake --build /tmp/box64/build --parallel "$(nproc)"; \
      cmake --install /tmp/box64/build; \
      test -x /usr/local/bin/box64; \
      apt-get purge -y --auto-remove build-essential cmake; \
      rm -rf /tmp/box64; \
    fi; \
    rm -rf /var/lib/apt/lists/*

# Host-level binfmt registrations commonly point to /usr/bin/box64. Wine starts
# x86-64 child processes after the explicitly wrapped launcher, so keep that
# conventional path available inside ARM64 containers as well.
RUN if [ -x /usr/local/bin/box64 ]; then \
      ln -sf /usr/local/bin/box64 /usr/bin/box64; \
      test -x /usr/bin/box64; \
    fi

COPY --from=uv /uv /uvx /usr/local/bin/

# Create a dedicated user for running Nighty
# Ubuntu 24.04 pre-creates an 'ubuntu' user with UID/GID 1000. We remove it so we can use 1000.
# Passwordless sudo is required because install.sh modifies /etc/hosts for RP-fetch blackholing
RUN set -eux; \
    test "$PUID" -gt 0; test "$PGID" -gt 0; \
    userdel -r ubuntu 2>/dev/null || true; \
    groupadd -g "$PGID" nighty; \
    useradd -u "$PUID" -g "$PGID" -m -s /bin/bash nighty; \
    echo "nighty ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/nighty

# Setup directories
WORKDIR /app
COPY --chown=nighty:nighty scripts/ /app/scripts/
COPY --chown=nighty:nighty config/ /app/config/
COPY --chown=nighty:nighty .env.example /app/.env.example

RUN mkdir -p /data/nighty /app/dist/ws_extensions && \
    chown -R nighty:nighty /app /data

# Default environment variables
ENV WEBUI_HOST=127.0.0.1 \
    BRIDGE_HOST=0.0.0.0 \
    PYTHONIOENCODING=utf-8 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_DIR=/etc/ssl/certs

USER nighty

# Persist data outside the container
VOLUME ["/data"]

EXPOSE 8088

HEALTHCHECK --interval=15s --timeout=5s --start-period=360s --retries=4 \
  CMD curl -fsS http://127.0.0.1:8088/ready | grep -q '"ready": true' || exit 1

CMD ["bash", "scripts/docker-entrypoint.sh"]
