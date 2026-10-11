FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_SERVER_PORT=3002 \
    IMAGE_MCP_DATA_DIR=/app/data \
    IMAGE_MCP_CONFIG=/app/data/config.json

# RapidOCR (onnxruntime / opencv) 运行时依赖
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd -r -u 10001 app && mkdir -p /app/data && chown -R app /app/data
USER app

EXPOSE 3002
VOLUME ["/app/data"]
# 容器内需监听 0.0.0.0；前面仍建议放 Caddy/网关做 TLS
CMD ["python", "-c", "import uvicorn, server; uvicorn.run(server.create_app(), host='0.0.0.0', port=3002, proxy_headers=True, forwarded_allow_ips='127.0.0.1')"]
