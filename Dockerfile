FROM rust:1.90-bookworm AS voyage-builder

ARG VOYAGE_REF=4469dc3b025b299c008e7d3a752863995448c70d

RUN apt-get update \
    && apt-get install -y --no-install-recommends git libpcap-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN git init /src/voyage \
    && git -C /src/voyage remote add origin https://github.com/dioptra-io/voyage.git \
    && git -C /src/voyage fetch --depth 1 origin "$VOYAGE_REF" \
    && git -C /src/voyage checkout --detach FETCH_HEAD \
    && cargo build --release --locked --manifest-path /src/voyage/Cargo.toml

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping libpcap0.8 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=voyage-builder /src/voyage/target/release/voyage /usr/local/bin/voyage

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN mkdir -p /data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
