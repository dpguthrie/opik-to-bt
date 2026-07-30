FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv \
    && curl -fsSL https://bt.dev/cli/install.sh -o /tmp/install-bt.sh \
    && XDG_BIN_HOME=/usr/local/bin bash /tmp/install-bt.sh --version 0.14.0 \
    && rm /tmp/install-bt.sh
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

ENTRYPOINT ["/app/.venv/bin/opik-to-bt"]
