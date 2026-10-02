# Inception-of-Wisdom (IoW) - Demo Target Service Dockerfile
# Note: campus/rootless Docker often rejects USER/chown (chown /dev/stdout: invalid argument).
# Run as root like IoC's container - the demo is a sacrificial crash target, not a hardened prod image.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

# Python as PID 1 ignores SIGTERM (no handler installed), so every `docker restart`
# used to wait the full 10 s timeout and end in SIGKILL. SIGINT is handled: the Flask
# server stops cleanly and a restart takes well under a second.
STOPSIGNAL SIGINT

CMD ["python", "app.py"]
