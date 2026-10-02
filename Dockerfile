# Inception-of-Wisdom (IoW) - Agent Service Dockerfile
#
# The image starts as root only long enough for scripts/agent_entrypoint.sh to look at
# who owns the mounted repository: on rootful Docker it drops to that uid (so patched
# files and git objects stay the host user's), on rootless Docker - where the host user
# already is uid 0 in here - it stays put. A fixed USER line cannot get both right.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    IOW_CACHE_DIR=/tmp/iow \
    HF_HOME=/tmp/iow/hf-cache \
    TORCH_HOME=/tmp/iow/hf-cache/torch \
    CHROMA_CACHE_DIR=/tmp/iow/chroma_db \
    ANONYMIZED_TELEMETRY=False \
    OLLAMA_HOST=http://ollama:11434

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /tmp/iow && chmod 1777 /tmp/iow

# The repository is bind-mounted and may belong to another uid than the process.
RUN git config --system --add safe.directory /workspace && \
    git config --system user.name "IoW Autonomous Agent" && \
    git config --system user.email "agent@iow.42.fr"

WORKDIR /workspace

# CPU-only torch first: the default PyPI wheel pulls ~3 GB of CUDA libraries that
# all-MiniLM-L6-v2 never uses. sentence-transformers then reuses this torch.
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN install -m 0755 scripts/agent_entrypoint.sh /usr/local/bin/iow-agent-entrypoint

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/iow-agent-entrypoint"]
CMD ["uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "8000"]
