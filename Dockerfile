FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    API_PORT=8000

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests
COPY scripts ./scripts

EXPOSE 8000

HEALTHCHECK --interval=3s --timeout=3s --start-period=4s --retries=20 \
  CMD python -c "import os,sys,urllib.request; port=os.environ.get('API_PORT','8000'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+port+'/health', timeout=2).status==200 else 1)"

# API_PORT 可通过环境变量配置容器内监听端口与 Compose 映射端口
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${API_PORT}"]
