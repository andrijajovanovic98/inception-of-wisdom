# Inception-of-Wisdom (IoW) - Agent Service Dockerfile
# Campus/rootless Docker often rejects USER/chown (chown /dev/stdout: invalid argument).
# Stay root in-container like IoC; host bind mounts + /tmp/iow handle permissions.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    IOW_CACHE_DIR=/tmp/iow \
    HF_HOME=/tmp/iow/hf-cache \
    SENTENCE_TRANSFORMERS_HOME=/tmp/iow/embeddings \
    CHROMA_CACHE_DIR=/tmp/iow/chroma_db \
    TORCH_HOME=/tmp/iow/torch \
    OLLAMA_HOST=http://host.docker.internal:11436

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /tmp/iow && chmod -R 777 /tmp/iow

RUN git config --system --add safe.directory /workspace && \
    git config --system user.name "IoW Autonomous Agent" && \
    git config --system user.email "agent@iow.42.fr"

WORKDIR /workspace

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "8000"]
