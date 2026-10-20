FROM python:3.12-slim

WORKDIR /app

# Install system deps for openpyxl/python-docx (none for slim, but keep slim)
# No build-essential needed — all packages are pure-python wheels.

# Install Python deps first (cache layer)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the engine + demo
COPY app/        ./app/
COPY ingest/     ./ingest/
COPY modules/    ./modules/
COPY demo/       ./demo/

# Reference data: the parsed ORO case database, the CHFT Map VBA source,
# and the case database notes. Runtime artefacts (ledger.sqlite3,
# data/outputs/, data/runs/) are gitignored and recreated on boot.
COPY data/       ./data/

# The container's working dir is /app; the engine imports "app.*", "ingest.*", "modules.*"
ENV PYTHONPATH=/app
# Cloud Run sets $PORT (defaults 8080)
ENV PORT=8080

# Runtime write paths. The engine writes the idempotency ledger to
# data/ and generated artefacts to /tmp/bma-factory-demo/. Create them
# up front and make them writable so the app works whichever uid it runs as.
RUN mkdir -p /app/data/outputs /app/data/runs /tmp/bma-factory-demo/output /tmp/bma-factory-demo/tokens \
    && chmod -R 0777 /app/data /tmp/bma-factory-demo

EXPOSE 8080

# Run the demo FastAPI app. For production, swap in the full API router.
CMD ["uvicorn", "demo.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--log-level", "info"]
