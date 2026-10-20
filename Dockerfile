FROM python:3.12-slim
WORKDIR /app
COPY demo/serve_minimal.py ./serve_minimal.py
ENV PORT=8080
EXPOSE 8080
CMD ["python", "-u", "serve_minimal.py"]
