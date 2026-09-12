# Inception-of-Wisdom (IoW) — Demo Target Service Dockerfile
FROM python:3.11-slim

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000

# Accept build arguments for matching host UID and GID to avoid permission conflicts
ARG UID=1000
ARG GID=1000

# Create a non-root application user matching the host UID/GID
RUN groupadd -g ${GID} appgroup 2>/dev/null || groupadd appgroup && \
    useradd -u ${UID} -g ${GID} -m -s /bin/bash appuser 2>/dev/null || useradd -m -s /bin/bash appuser

WORKDIR /app

# Install dependencies first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source files and adjust ownership
COPY . .
RUN chown -R ${UID}:${GID} /app

# Run as non-root user
USER appuser

EXPOSE 5000

CMD ["python", "app.py"]

