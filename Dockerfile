FROM python:3.12-slim AS base

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Copy dependency files first for layer caching
COPY pyproject.toml ./

# Copy source code
COPY src/ src/

# Install dependencies + project (editable)
RUN uv sync --no-dev

# Data volume
VOLUME ["/data"]
EXPOSE 8921

ENV OPENCORTEX_DATA_ROOT=/data
ENV OPENCORTEX_HTTP_SERVER_HOST=0.0.0.0
ENV OPENCORTEX_HTTP_SERVER_PORT=8921

ENTRYPOINT ["uv", "run", "opencortex-server"]
CMD ["--host", "0.0.0.0", "--port", "8921"]
