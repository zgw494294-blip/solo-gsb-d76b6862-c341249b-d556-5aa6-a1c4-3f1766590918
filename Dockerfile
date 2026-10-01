FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Local SQLite database directory. Compose mounts a named volume here; the
# directory is created writable by the runtime user so the database (and its
# WAL side files) can be auto-created on first start.
RUN mkdir -p /data && chown nobody:nogroup /data
VOLUME ["/data"]

EXPOSE 8000

USER nobody

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
