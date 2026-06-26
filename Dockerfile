FROM python:3.12-slim

ARG CODEX_VERSION=0.142.2
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/broker \
    CODEX_BROKER_HOST=0.0.0.0 \
    CODEX_BROKER_PORT=3400 \
    CODEX_BROKER_DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tar \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      amd64) codex_arch="x86_64" ;; \
      arm64) codex_arch="aarch64" ;; \
      *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    archive="codex-${codex_arch}-unknown-linux-musl.tar.gz"; \
    binary="codex-${codex_arch}-unknown-linux-musl"; \
    curl -fsSL -o "/tmp/${archive}" "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/${archive}"; \
    tar -xzf "/tmp/${archive}" -C /tmp; \
    install -m 0755 "/tmp/${binary}" /usr/local/bin/codex; \
    rm -f "/tmp/${archive}" "/tmp/${binary}"; \
    codex --version

RUN useradd --create-home --shell /usr/sbin/nologin broker \
    && mkdir -p /data \
    && chown -R broker:broker /data /home/broker

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

USER broker
EXPOSE 3400
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${CODEX_BROKER_PORT:-3400}/readyz" >/dev/null || exit 1

CMD ["codex-broker"]
