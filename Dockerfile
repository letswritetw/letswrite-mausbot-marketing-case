FROM python:3-alpine

WORKDIR /app

COPY server.py /app/server.py
COPY web /app/web

ENV DATA_DIR=/data \
    PORT=80 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Taipei

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1/healthz', timeout=3)"

CMD ["python", "/app/server.py"]
