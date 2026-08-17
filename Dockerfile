FROM node:22-bookworm-slim AS web-builder

WORKDIR /build/web-ui
COPY web-ui/package.json web-ui/package-lock.json ./
RUN npm ci
COPY web-ui/ ./
RUN npm run build

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/var/lib/teamEvolver \
    VIRTUAL_ENV=/opt/teamEvolver-venv \
    PATH=/opt/teamEvolver-venv/bin:$PATH \
    TEAMEVOLVER_HERMES_BIN=/opt/teamEvolver-venv/bin/hermes

WORKDIR /app

COPY pyproject.toml README.md README.en.md LICENSE ./
COPY teamEvolver/ ./teamEvolver/
COPY --from=web-builder /build/teamEvolver/web/dist ./teamEvolver/web/dist/
COPY docker/entrypoint.sh /usr/local/bin/teamEvolver-entrypoint

RUN python -m venv "$VIRTUAL_ENV" \
    && "$VIRTUAL_ENV/bin/python" -m pip install --no-cache-dir --upgrade pip \
    && "$VIRTUAL_ENV/bin/python" -m pip install --no-cache-dir ".[all]" \
    && "$TEAMEVOLVER_HERMES_BIN" --version >/dev/null \
    && chmod +x /usr/local/bin/teamEvolver-entrypoint \
    && mkdir -p "$HOME/.teamEvolver"

EXPOSE 52010

ENTRYPOINT ["/usr/local/bin/teamEvolver-entrypoint"]
