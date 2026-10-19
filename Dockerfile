# The runtime image installs only a built release (cicd_scripts/build.sh):
# the application wheel plus the hashed wheelhouse. No source tree is copied
# and PYTHONPATH is not set, so the process runs exactly the installed release.
#
#   TARGET_ARCH=x86_64 cicd_scripts/build.sh
#   docker build --platform linux/amd64 -t teamevolver:<version> .

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
    OPENVIKING_CLI_BIN=/usr/local/bin/ov

COPY --from=openviking-cli /usr/local/bin/ov /usr/local/bin/ov
COPY release/ /tmp/release/

# git is used only by the isolated Skill Git transfer adapter.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv "$VIRTUAL_ENV" \
    && PYTHON_BIN="$VIRTUAL_ENV/bin/python" sh /tmp/release/install.sh \
    && install -m 0755 /tmp/release/container_start.sh /usr/local/bin/teamEvolver-start \
    && rm -rf /tmp/release \
    && "$OPENVIKING_CLI_BIN" --version >/dev/null \
    && git --version >/dev/null \
    && mkdir -p "$HOME/.teamEvolver"

WORKDIR /var/lib/teamEvolver
EXPOSE 52010 52011

ENTRYPOINT ["/usr/local/bin/teamEvolver-start"]
