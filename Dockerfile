# One image for both Cloud Run services; the console overrides the command:
#   sentinel-agent   : python -m sentinel.service   (default CMD)
#   sentinel-console : python -m sentinel.console
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY sentinel ./sentinel

# Cloud Run injects PORT; both entry points honour it.
EXPOSE 8080
CMD ["python", "-m", "sentinel.service"]
