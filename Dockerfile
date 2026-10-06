FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VF_DB=/data/ledger.sqlite3 \
    VF_OUTPUT_DIR=/data/outputs

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY modules ./modules

# Cloud Run mounts the writable filesystem; keep everything under /data
RUN mkdir -p /data/outputs

ENV PYTHONPATH=/app

EXPOSE 8080
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8080"]