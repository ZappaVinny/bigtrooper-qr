# BigTrooper QR service. lib3mf only ships x86_64 Linux wheels, so build/run as linux/amd64.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so this layer is cached until requirements.txt changes
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bigtrooper_qr/ bigtrooper_qr/
COPY app/ app/

RUN useradd --system --no-create-home --uid 10001 app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/', timeout=3)"

# Shell form so PORT from the environment is expanded
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --no-server-header
