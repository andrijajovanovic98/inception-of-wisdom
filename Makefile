# Inception-of-Wisdom (IoW) — Multi-Mode Automation & Campus Runtime under /tmp/iow
# Usage:
#   make up       # Launch containerized multi-service stack via Docker Compose
#   make down     # Stop and tear down Docker Compose containers
#   make setup    # Prepare local /tmp/iow environment (venv, deps, embeddings, Ollama model)
#   make run      # Run unified IoW Dashboard (Observer + Analyst + Wisdom Loop) on :8000
#   make p1       # Run Part 1 (Observer dashboard & stream)
#   make p2       # Run Part 2 (Analyst RAG retrieval & diagnostics)
#   make p3       # Run Part 3 (Wisdom Loop autonomous self-healing)
#   make break    # Trigger intentional crash on target to demonstrate live healing
#   make heal     # Trigger manual heal cycle via REST API
#   make rollback # Trigger emergency git rollback via REST API
#   make status   # Inspect system overview status via CLI
#   make logs     # Follow Docker container logs
#   make clean    # Remove Chroma DB / pip+hf caches under /tmp/iow (keep venv)
#   make fclean   # Full wipe of /tmp/iow (venv + models cache + db)
#   make re       # fclean + setup

IOW_DIR      := /tmp/iow
VENV         := $(IOW_DIR)/venv
PIP_CACHE    := $(IOW_DIR)/pip-cache
HF_HOME      := $(IOW_DIR)/hf-cache
CHROMA_DIR   := $(IOW_DIR)/chroma_db
OLLAMA_DIR   := $(IOW_DIR)/ollama

# Private port to avoid conflict with campus server that writes to /opt/ollama
OLLAMA_HOST  := 127.0.0.1:11435
PYTHON       := /usr/bin/python3
PY_VER       := $(shell $(PYTHON) -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "3.11")
SITE_PACKAGES:= $(VENV)/lib/python$(PY_VER)/site-packages
REQ          := requirements.txt
LLM_MODEL    := qwen2.5-coder:1.5b
EMBED_MODEL  := all-MiniLM-L6-v2
PORT         := 8000
TARGET       := demo_app

# Campus image: python3 -m venv often lacks ensurepip; virtualenv is available.
VIRTUALENV   := $(shell command -v virtualenv 2>/dev/null)

export PIP_CACHE_DIR         := $(PIP_CACHE)
export HF_HOME
export TRANSFORMERS_CACHE   := $(HF_HOME)
export CHROMA_CACHE_DIR     := $(CHROMA_DIR)
export TMPDIR                := /tmp
export PYTHONNOUSERSITE      := 1
export PYTHONPATH            := $(CURDIR):$(SITE_PACKAGES)
# Writable models dir (overrides campus OLLAMA_MODELS=/opt/ollama)
export OLLAMA_MODELS         := $(OLLAMA_DIR)
export OLLAMA_HOST

.PHONY: all up down run p1 p2 p3 break heal rollback status logs setup clean fclean re help \
	ensure-dirs ensure-venv ensure-deps ensure-ready ensure-ollama ensure-ollama-quick ensure-embed

all: help

help:
	@echo "================================================================================"
	@echo "                    INCEPTION OF WISDOM (IoW) — COMMANDS                        "
	@echo "================================================================================"
	@echo " [Docker Mode]"
	@echo "   make up        - Build and launch the containerized stack (Agent + Target App)"
	@echo "   make down      - Stop and tear down Docker containers"
	@echo "   make logs      - Follow live logs across all containers"
	@echo ""
	@echo " [Live Evaluation / Defense]"
	@echo "   make break     - Trigger intentional crash on the target service"
	@echo "   make heal      - Trigger manual self-healing cycle via API"
	@echo "   make rollback  - Trigger emergency rollback to pre-loop git revision"
	@echo "   make status    - Query system health & safety guardrails status"
	@echo ""
	@echo " [Local Campus Runtime under /tmp/iow]"
	@echo "   make setup     - Prepare /tmp/iow (venv, deps, embeddings, Ollama $(LLM_MODEL))"
	@echo "   make run       - Run complete Dashboard on http://127.0.0.1:$(PORT)"
	@echo "   make p1        - Run Part 1 (Observer focus)"
	@echo "   make p2        - Run Part 2 (Analyst focus)"
	@echo "   make p3        - Run Part 3 (Wisdom Loop focus)"
	@echo "   make stop      - Stop background Ollama started by this Makefile (if any)"
	@echo "   make clean     - Remove Chroma DB & caches (keep venv)"
	@echo "   make fclean    - Full wipe of /tmp/iow"
	@echo "   make re        - fclean + setup"
	@echo "================================================================================"

# ==============================================================================
# Docker Stack Orchestration
# ==============================================================================

up:
	@echo "[*] Launching containerized IoW stack with Docker Compose..."
	@mkdir -p /tmp/iow_cache && chmod 777 /tmp/iow_cache 2>/dev/null || true
	@docker compose up --build -d 2>/dev/null || docker-compose up --build -d
	@echo "[+] Services started!"
	@echo "    - Target App:  http://localhost:5001"
	@echo "    - Dashboard:   http://localhost:8000"

down:
	@echo "[*] Stopping IoW containers..."
	@docker compose down 2>/dev/null || docker-compose down 2>/dev/null || true
	@echo "[+] Containers stopped cleanly."

logs:
	@docker compose logs -f 2>/dev/null || docker-compose logs -f

# ==============================================================================
# Defense & Live Evaluation Shortcuts
# ==============================================================================

break:
	@echo "[*] Triggering intentional crash on target application..."
	@curl -s -X POST http://localhost:5001/api/crash 2>/dev/null \
		|| curl -s -X POST http://127.0.0.1:5000/api/crash 2>/dev/null \
		|| echo "[!] Target service unreachable or already crashed."
	@echo ""
	@echo "[!] Crash event sent. Inspect Dashboard at http://localhost:8000 to observe healing!"

heal:
	@echo "[*] Requesting self-healing cycle via API..."
	@curl -s -X POST http://localhost:8000/api/loop/trigger -H "Content-Type: application/json" -d '{}' \
		| python3 -m json.tool 2>/dev/null || echo "[!] Dashboard unreachable"

rollback:
	@echo "[*] Requesting emergency rollback to pre-loop git revision..."
	@curl -s -X POST http://localhost:8000/api/loop/rollback -H "Content-Type: application/json" -d '{}' \
		| python3 -m json.tool 2>/dev/null || echo "[!] Dashboard unreachable"

status:
	@echo "[*] Querying IoW status overview..."
	@curl -s http://localhost:8000/api/overview | python3 -m json.tool 2>/dev/null || echo "[!] Dashboard unreachable at http://localhost:8000"

# ==============================================================================
# Campus Runtime & Environment Provisioning (/tmp/iow)
# ==============================================================================

ensure-dirs:
	@mkdir -p $(IOW_DIR) $(PIP_CACHE) $(HF_HOME) $(CHROMA_DIR) $(OLLAMA_DIR) $(IOW_DIR)/logs

ensure-venv: ensure-dirs
	@if [ ! -x "$(VENV)/bin/pip" ]; then \
		if [ -n "$(VIRTUALENV)" ]; then \
			echo "[*] Creating virtualenv at $(VENV) using virtualenv"; \
			$(VIRTUALENV) $(VENV); \
		else \
			echo "[*] Creating virtualenv at $(VENV) using python3 -m venv"; \
			$(PYTHON) -m venv $(VENV) || { echo "[!] Failed to create venv"; exit 1; }; \
		fi; \
	else \
		echo "[*] Virtualenv already present: $(VENV)"; \
	fi

ensure-deps: ensure-venv
	@if [ -f "$(IOW_DIR)/.deps-ok" ] && [ "$(IOW_DIR)/.deps-ok" -nt "$(REQ)" ] \
		&& $(PYTHON) -c "import chromadb,fastapi,uvicorn,sentence_transformers,httpx,yaml,docker,git" 2>/dev/null; then \
		echo "[*] Dependencies already ready (skip pip)"; \
	else \
		echo "[*] Installing CPU torch + IoW dependencies into $(VENV)"; \
		$(VENV)/bin/pip install --upgrade pip setuptools wheel 2>/dev/null || true; \
		$(VENV)/bin/pip install --cache-dir $(PIP_CACHE) \
			torch --index-url https://download.pytorch.org/whl/cpu; \
		$(VENV)/bin/pip install --cache-dir $(PIP_CACHE) -r $(REQ); \
		touch "$(IOW_DIR)/.deps-ok"; \
	fi

ensure-ready:
	@if [ ! -x "$(VENV)/bin/python" ] \
		&& ! $(PYTHON) -c "import chromadb,fastapi,uvicorn,sentence_transformers,httpx,yaml,git" 2>/dev/null; then \
		echo "[!] IoW runtime not ready under $(IOW_DIR). Run: make setup"; \
		exit 1; \
	fi
	@echo "[*] IoW runtime ready."

ensure-ollama: ensure-dirs
	@command -v ollama >/dev/null 2>&1 || { echo "[!] ollama not found in PATH. Please install or make available."; exit 1; }
	@if [ -f $(IOW_DIR)/ollama.pid ] && kill -0 $$(cat $(IOW_DIR)/ollama.pid) 2>/dev/null; then \
		echo "[*] Ollama already running (pid $$(cat $(IOW_DIR)/ollama.pid), $(OLLAMA_HOST))"; \
	else \
		echo "[*] Starting local Ollama serve (models=$(OLLAMA_DIR), host=$(OLLAMA_HOST))"; \
		mkdir -p $(IOW_DIR)/logs $(OLLAMA_DIR); \
		nohup env HOME="$(IOW_DIR)" OLLAMA_MODELS="$(OLLAMA_DIR)" OLLAMA_HOST="$(OLLAMA_HOST)" \
			ollama serve >$(IOW_DIR)/logs/ollama.log 2>&1 & echo $$! > $(IOW_DIR)/ollama.pid; \
		sleep 2; \
	fi
	@echo "[*] Ensuring Ollama model $(LLM_MODEL)"
	@HOME="$(IOW_DIR)" OLLAMA_MODELS="$(OLLAMA_DIR)" OLLAMA_HOST="$(OLLAMA_HOST)" ollama pull $(LLM_MODEL)

ensure-ollama-quick: ensure-dirs
	@command -v ollama >/dev/null 2>&1 || { echo "[*] Note: ollama binary not in PATH; skipping local daemon check."; exit 0; }
	@if [ -f $(IOW_DIR)/ollama.pid ] && kill -0 $$(cat $(IOW_DIR)/ollama.pid) 2>/dev/null; then \
		true; \
	else \
		mkdir -p $(IOW_DIR)/logs $(OLLAMA_DIR); \
		nohup env HOME="$(IOW_DIR)" OLLAMA_MODELS="$(OLLAMA_DIR)" OLLAMA_HOST="$(OLLAMA_HOST)" \
			ollama serve >$(IOW_DIR)/logs/ollama.log 2>&1 & echo $$! > $(IOW_DIR)/ollama.pid; \
		sleep 2; \
	fi

ensure-embed: ensure-deps
	@echo "[*] Prefetching embedding model $(EMBED_MODEL) into $(HF_HOME)..."
	@if [ -x "$(VENV)/bin/python" ]; then \
		$(VENV)/bin/python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('$(EMBED_MODEL)'); print('[+] embedding model ready')"; \
	else \
		$(PYTHON) -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('$(EMBED_MODEL)'); print('[+] embedding model ready')" 2>/dev/null || true; \
	fi

setup: ensure-deps ensure-ollama ensure-embed
	@echo ""
	@echo "[+] Setup complete under $(IOW_DIR)"
	@echo "    To launch the Dashboard: make run   →  http://127.0.0.1:$(PORT)"

# ==============================================================================
# Local Runners (Observer, Analyst, Wisdom Loop)
# ==============================================================================

run: ensure-ready ensure-ollama-quick
	@echo "[*] Launching Inception-of-Wisdom Unified Dashboard on http://127.0.0.1:$(PORT)"
	@if [ -x "$(VENV)/bin/uvicorn" ]; then \
		$(VENV)/bin/uvicorn dashboard.app:app --host 127.0.0.1 --port $(PORT); \
	else \
		$(PYTHON) -m uvicorn dashboard.app:app --host 127.0.0.1 --port $(PORT); \
	fi

p1: ensure-ready
	@echo "[*] Part 1 - Observer: Launching on http://127.0.0.1:$(PORT)"
	@make run

p2: ensure-ready ensure-ollama-quick
	@echo "[*] Part 2 - Analyst: Launching on http://127.0.0.1:$(PORT)"
	@make run

p3: ensure-ready ensure-ollama-quick
	@echo "[*] Part 3 - Wisdom Loop: Launching on http://127.0.0.1:$(PORT)"
	@make run

stop:
	@if [ -f $(IOW_DIR)/ollama.pid ]; then \
		kill $$(cat $(IOW_DIR)/ollama.pid) 2>/dev/null || true; \
		rm -f $(IOW_DIR)/ollama.pid; \
		echo "[*] Stopped Makefile-started Ollama"; \
	else \
		echo "[*] No Makefile Ollama pid file found"; \
	fi

clean: stop
	@rm -rf $(CHROMA_DIR) $(PIP_CACHE) $(HF_HOME) $(IOW_DIR)/logs $(IOW_DIR)/.deps-ok .chroma
	@echo "[*] Cleaned caches and Chroma DB under $(IOW_DIR) (venv preserved)"

fclean: stop
	@rm -rf $(IOW_DIR) .chroma
	@echo "[*] Full wipe of $(IOW_DIR) complete."

re: fclean setup

