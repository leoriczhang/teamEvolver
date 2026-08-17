FROM node:22-bookworm-slim AS web-builder

WORKDIR /build/web-ui
COPY web-ui/package.json web-ui/package-lock.json ./
RUN npm ci
COPY web-ui/ ./
RUN npm run build

# OpenViking's released Linux CLI is linked against glibc >= 2.39.
FROM debian:trixie-slim AS openviking-cli

ARG TARGETARCH
ARG OPENVIKING_CLI_VERSION=0.3.14

# Use OpenViking's published, platform-specific CLI binary instead of relying
# on a host installation.  Pin both the version and checksum so rebuilds are
# deterministic and fail closed if the downloaded artifact is not expected.
# TARGETARCH is supplied by BuildKit; dpkg is the fallback for legacy
# docker-compose builds.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tar \
    && rm -rf /var/lib/apt/lists/* \
    && detected_arch="${TARGETARCH:-$(dpkg --print-architecture)}" \
    && case "$detected_arch" in \
        amd64) ov_arch="x86_64"; ov_sha256="1d20e1953328c0f18709565988aa4f079cdf7e2d66a61c03f4b753ec00c313b8" ;; \
        arm64) ov_arch="aarch64"; ov_sha256="7bf86b838c07909726d983a21aa574baaa8db77df22373182a3a6ccaf006f89e" ;; \
        *) echo "Unsupported OpenViking CLI architecture: $detected_arch" >&2; exit 1 ;; \
    esac \
    && curl --fail --location --retry 4 --retry-all-errors --silent --show-error \
        -o /tmp/ov.tar.gz \
        "https://github.com/volcengine/OpenViking/releases/download/cli%40${OPENVIKING_CLI_VERSION}/ov-linux-${ov_arch}.tar.gz" \
    && echo "${ov_sha256}  /tmp/ov.tar.gz" | sha256sum --check --strict \
    && tar -xzf /tmp/ov.tar.gz -C /usr/local/bin \
    && chmod 0755 /usr/local/bin/ov \
    && /usr/local/bin/ov --version \
    && rm -f /tmp/ov.tar.gz

# Keep the runtime on the same glibc generation as the bundled ov binary.
FROM python:3.11-slim-trixie AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/var/lib/teamEvolver \
    VIRTUAL_ENV=/opt/teamEvolver-venv \
    PATH=/opt/teamEvolver-venv/bin:$PATH \
    TEAMEVOLVER_HERMES_BIN=/opt/teamEvolver-venv/bin/hermes \
    OPENVIKING_CLI_BIN=/usr/local/bin/ov

WORKDIR /app

COPY pyproject.toml README.md README.en.md LICENSE ./
COPY teamEvolver/ ./teamEvolver/
COPY --from=web-builder /build/teamEvolver/web/dist ./teamEvolver/web/dist/
COPY --from=openviking-cli /usr/local/bin/ov /usr/local/bin/ov
COPY docker/entrypoint.sh /usr/local/bin/teamEvolver-entrypoint

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv "$VIRTUAL_ENV" \
    && "$VIRTUAL_ENV/bin/python" -m pip install --no-cache-dir --upgrade pip \
    && "$VIRTUAL_ENV/bin/python" -m pip install --no-cache-dir ".[all]" \
    && "$TEAMEVOLVER_HERMES_BIN" --version >/dev/null \
    && "$OPENVIKING_CLI_BIN" --version >/dev/null \
    && chmod +x /usr/local/bin/teamEvolver-entrypoint \
    && mkdir -p "$HOME/.teamEvolver"

EXPOSE 52010

ENTRYPOINT ["/usr/local/bin/teamEvolver-entrypoint"]
