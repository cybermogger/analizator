FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN useradd --create-home --uid 10001 analyzer
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data/raw /models /logs && chown -R analyzer:analyzer /app /data /models /logs
USER analyzer

VOLUME ["/data", "/models", "/logs"]
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=3 \
    CMD ["python", "status.py", "--healthcheck", "--max-age-seconds", "180"]

CMD ["python", "watchdog.py"]
