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

# The container's working dir is /app; the engine imports "app.*", "ingest.*", "modules.*"
ENV PYTHONPATH=/app
# Cloud Run sets $PORT (defaults 8080)
ENV PORT=8080

EXPOSE 8080

# Run the demo FastAPI app. For production, swap in the full API router.
CMD ["python", "-u", "demo/boot.py"]
