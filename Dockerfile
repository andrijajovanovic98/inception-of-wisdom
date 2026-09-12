# Inception-of-Wisdom (IoW) — Agent Service Dockerfile
FROM python:3.11-slim

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    HF_HOME=/tmp/iow_cache/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/tmp/iow_cache/sentence_transformers \
    CHROMA_CACHE_DIR=/tmp/iow_cache/chroma \
    TORCH_HOME=/tmp/iow_cache/torch \
    OLLAMA_HOST=http://host.docker.internal:11434

# Install essential system packages (git for repo healing, curl for healthchecks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Match host user UID and GID to avoid permission conflicts on bind mounts
ARG UID=101888
ARG GID=4224

RUN (groupadd -g ${GID} iowgroup 2>/dev/null || groupadd iowgroup 2>/dev/null || true) && \
    (useradd -u ${UID} -g ${GID} -m -s /bin/bash iowuser 2>/dev/null || useradd -m -s /bin/bash iowuser 2>/dev/null || true) && \
    (usermod -aG nogroup iowuser 2>/dev/null || true)

# Prepare cache directories in /tmp with write access
RUN mkdir -p /tmp/iow_cache && chmod -R 777 /tmp/iow_cache

# Set system-wide git configuration so commits on iow/auto-heal succeed
RUN git config --system --add safe.directory /workspace && \
    git config --system user.name "IoW Autonomous Agent" && \
    git config --system user.email "agent@iow.42.fr"

WORKDIR /workspace

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project source files
COPY . .

# Adjust permissions for workspace and run as iowuser
RUN chown -R ${UID}:${GID} /workspace 2>/dev/null || true

USER iowuser

EXPOSE 8000

CMD ["uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "8000"]

