FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY redink_cli/ redink_cli/
COPY adapters/ adapters/
COPY services/ services/
COPY ops/ ops/

RUN pip install --no-cache-dir -e .

EXPOSE 8080 3000
