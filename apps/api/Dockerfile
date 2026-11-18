# Contexto de build: raíz del repositorio backend (incluye apps/api y ml).
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
RUN pip install torch==2.15.0 --index-url https://download.pytorch.org/whl/cpu
COPY apps/api/requirements.txt apps/api/requirements.txt
RUN pip install -r apps/api/requirements.txt
COPY ml ml
RUN pip install --no-deps ./ml
COPY apps/api apps/api
WORKDIR /srv/apps/api
RUN useradd --create-home --uid 1001 edumaster && mkdir -p /data/models && chown -R edumaster /data
USER edumaster
ENV EDUMASTER_MODEL_DIR=/data/models
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
