# Inception-of-Wisdom (IoW) — Multi-Mode Automation & Campus Runtime under /tmp/iow
# Usage:
#   make up / down / docker-restart / docker-clean / docker-fclean
#   make flake / mypy / lint
#   make setup
#   make p1 / p2 / p3 / bonus / run   # progressive tabs + auto-start target
#   make break / heal / rollback / status / logs
#   make clean / fclean / re
# NOTE: Do not revert lint (.flake8, mypy.ini) or these Makefile rules.

IOW_DIR      := /tmp/iow
VENV         := $(IOW_DIR)/venv
PIP_CACHE    := $(IOW_DIR)/pip-cache
HF_HOME      := $(IOW_DIR)/hf-cache
CHROMA_DIR   := $(IOW_DIR)/chroma_db
OLLAMA_DIR   := $(IOW_DIR)/ollama
MYPY_CACHE   := $(IOW_DIR)/mypy_cache
# All IoW runtime products live only under $(IOW_DIR) (= /tmp/iow), like IoC's /tmp/ioc.
# IOW_LEGACY: old stray dirs from earlier layouts — fclean still deletes them if present.
IOW_LEGACY   := /tmp/iow_cache /tmp/iow-lint /tmp/iow-lint-pip-cache /tmp/iow_docker

# Avoid campus 11434 and IoC 11435
OLLAMA_BIND  := 0.0.0.0:11436
OLLAMA_HOST  := 127.0.0.1:11436
PYTHON       := /usr/bin/python3
PY_VER       := $(shell $(PYTHON) -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
SITE_PACKAGES:= $(VENV)/lib/python$(PY_VER)/site-packages
REQ          := requirements.txt
LLM_MODEL    := qwen2.5-coder:1.5b
EMBED_MODEL  := all-MiniLM-L6-v2
PORT         := 8000
TARGET       := demo_app
TARGET_URL   ?= http://127.0.0.1:5001
LINT_DIRS    := p1 p2 p3 bonus demo_app dashboard
DOCKER_IMAGE_AGENT := inception-of-wisdom-agent
DOCKER_IMAGE_DEMO  := inception-of-wisdom-demo_app
DOCKER_NETWORK     := iow-network
# Keep Docker CLI/buildx state off the tiny campus $HOME (often 100% full)
DOCKER_CONFIG_DIR  := $(IOW_DIR)/docker-config
VIRTUALENV   := $(shell command -v virtualenv 2>/dev/null)

export PIP_CACHE_DIR     := $(PIP_CACHE)
export HF_HOME
export CHROMA_CACHE_DIR := $(CHROMA_DIR)
export TMPDIR            := /tmp
export PYTHONNOUSERSITE  := 1
export PYTHONPATH        := $(CURDIR):$(SITE_PACKAGES)
export OLLAMA_MODELS     := $(OLLAMA_DIR)
export OLLAMA_HOST
export DOCKER_CONFIG     := $(DOCKER_CONFIG_DIR)

.PHONY: all up down docker-restart docker-clean docker-fclean run p1 p2 p3 bonus \
	break heal rollback status logs setup clean fclean re help stop \
	ensure-dirs ensure-venv ensure-deps ensure-ready ensure-ollama ensure-ollama-quick \
	ensure-embed ensure-lint-tools ensure-target flake mypy lint

all: help

help:
	@echo "================================================================================"
	@echo "                    INCEPTION OF WISDOM (IoW) — COMMANDS                        "
	@echo "================================================================================"
	@echo " make up               - build and launch containerized IoW via Docker Compose"
	@echo " make down             - stop and tear down Docker containers"
	@echo " make docker-restart   - rebuild images and recreate IoW containers"
	@echo " make docker-clean     - remove IoW containers/network (keep images)"
	@echo " make docker-fclean    - remove IoW containers/network/volumes/images"
	@echo " make setup            - prepare $(IOW_DIR) (venv, pip, embeddings, ollama)"
	@echo " make p1 | p2 | p3 | bonus | run  - progressive dashboard on :$(PORT)"
	@echo " make flake | mypy | lint"
	@echo " make stop             - stop IoW ollama (pid file + orphans on $(OLLAMA_BIND))"
	@echo " make clean            - docker-clean + caches (keep venv)"
	@echo " make fclean           - docker-fclean + full wipe of $(IOW_DIR)"
	@echo " make re               - fclean + setup"
	@echo "================================================================================"

up: ensure-dirs
	@echo "[*] Launching containerized IoW stack..."
	@mkdir -p $(IOW_DIR) && chmod 777 $(IOW_DIR) 2>/dev/null || true
	@docker compose up --build -d 2>/dev/null || docker-compose up --build -d
	@echo "[+] Target: http://localhost:5001  Dashboard: http://localhost:8000"

down:
	@docker compose down 2>/dev/null || docker-compose down 2>/dev/null || true
	@echo "[+] Containers stopped."

docker-restart: ensure-dirs
	@mkdir -p $(IOW_DIR) && chmod 777 $(IOW_DIR) 2>/dev/null || true
	@docker compose up --build -d --force-recreate 2>/dev/null \
		|| docker-compose up --build -d --force-recreate
	@echo "[+] docker-restart done (rebuild + recreate IoW containers)"

# Soft Docker cleanup (IoW only): containers + project network, keep images.
docker-clean:
	@if ! command -v docker >/dev/null 2>&1; then \
		echo "[*] Docker not available — skip docker-clean"; \
	else \
		echo "[*] Docker clean (container/network; keep images)"; \
		docker compose down --remove-orphans >/dev/null 2>&1 \
			|| docker-compose down --remove-orphans >/dev/null 2>&1 \
			|| true; \
		for c in iow_agent iow_demo_target; do \
			if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$$c"; then \
				docker rm -f "$$c" >/dev/null 2>&1 || true; \
				echo "[*] Removed container $$c"; \
			fi; \
		done; \
		echo "[+] docker-clean done"; \
	fi

# Full Docker cleanup (IoW only): containers, networks, volumes, images.
# Never fails when Docker/IoW artifacts are absent.
docker-fclean:
	@if ! command -v docker >/dev/null 2>&1; then \
		echo "[*] Docker not available — skip docker-fclean"; \
	else \
		echo "[*] Docker fclean (container/network/volume/image for IoW only)"; \
		docker compose down --rmi local --volumes --remove-orphans >/dev/null 2>&1 \
			|| docker-compose down --rmi local --volumes --remove-orphans >/dev/null 2>&1 \
			|| true; \
		for c in iow_agent iow_demo_target; do \
			if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$$c"; then \
				docker rm -f "$$c" >/dev/null 2>&1 || true; \
				echo "[*] Removed container $$c"; \
			fi; \
		done; \
		for img in $(DOCKER_IMAGE_AGENT) $(DOCKER_IMAGE_DEMO); do \
			if docker image inspect "$$img:latest" >/dev/null 2>&1; then \
				docker rmi -f "$$img:latest" >/dev/null 2>&1 || true; \
				echo "[*] Removed image $$img:latest"; \
			fi; \
			ids=$$(docker images -q "$$img" 2>/dev/null || true); \
			if [ -n "$$ids" ]; then \
				docker rmi -f $$ids >/dev/null 2>&1 || true; \
				echo "[*] Removed remaining $$img image tags"; \
			fi; \
		done; \
		if docker network ls --format '{{.Name}}' 2>/dev/null | grep -qx '$(DOCKER_NETWORK)'; then \
			docker network rm $(DOCKER_NETWORK) >/dev/null 2>&1 || true; \
			echo "[*] Removed network $(DOCKER_NETWORK)"; \
		fi; \
		echo "[+] docker-fclean done (other Docker images untouched)"; \
	fi

logs:
	@docker compose logs -f 2>/dev/null || docker-compose logs -f

break:
	@curl -s -X POST http://localhost:5001/api/crash 2>/dev/null \
		|| curl -s -X POST http://127.0.0.1:5000/api/crash 2>/dev/null \
		|| echo "[!] Target unreachable"
	@echo "[!] Crash sent — watch dashboard :8000"

heal:
	@curl -s -X POST http://localhost:8000/api/loop/trigger -H "Content-Type: application/json" -d '{}' \
		| python3 -m json.tool 2>/dev/null || echo "[!] Dashboard unreachable"

rollback:
	@curl -s -X POST http://localhost:8000/api/loop/rollback -H "Content-Type: application/json" -d '{}' \
		| python3 -m json.tool 2>/dev/null || echo "[!] Dashboard unreachable"

status:
	@curl -s http://localhost:8000/api/overview | python3 -m json.tool 2>/dev/null || echo "[!] Dashboard unreachable"

ensure-dirs:
	@mkdir -p $(IOW_DIR) $(PIP_CACHE) $(HF_HOME) $(CHROMA_DIR) $(OLLAMA_DIR) $(MYPY_CACHE) \
		$(IOW_DIR)/logs $(DOCKER_CONFIG_DIR)/buildx
	@# Seed Docker CLI config under /tmp/iow so buildx never writes to full $HOME
	@if [ ! -f "$(DOCKER_CONFIG_DIR)/config.json" ] && [ -f "$$HOME/.docker/config.json" ]; then \
		cp "$$HOME/.docker/config.json" "$(DOCKER_CONFIG_DIR)/config.json"; \
	fi
	@if [ ! -d "$(DOCKER_CONFIG_DIR)/contexts" ] && [ -d "$$HOME/.docker/contexts" ]; then \
		cp -a "$$HOME/.docker/contexts" "$(DOCKER_CONFIG_DIR)/contexts"; \
	fi

ensure-venv: ensure-dirs
	@if [ ! -x "$(VENV)/bin/pip" ]; then \
		if [ -n "$(VIRTUALENV)" ]; then $(VIRTUALENV) $(VENV); \
		else $(PYTHON) -m venv $(VENV) || { echo "[!] Failed to create venv"; exit 1; }; fi; \
	else echo "[*] Virtualenv already present: $(VENV)"; fi

ensure-deps: ensure-venv
	@if [ -f "$(IOW_DIR)/.deps-ok" ] && [ "$(IOW_DIR)/.deps-ok" -nt "$(REQ)" ] \
		&& PYTHONPATH="$(CURDIR):$(SITE_PACKAGES)" $(PYTHON) -c \
			"import chromadb,fastapi,uvicorn,sentence_transformers,httpx,yaml,docker,git" 2>/dev/null; then \
		echo "[*] Dependencies already ready (skip pip)"; \
	else \
		$(VENV)/bin/pip install --upgrade pip setuptools wheel 2>/dev/null || true; \
		$(VENV)/bin/pip install --cache-dir $(PIP_CACHE) torch --index-url https://download.pytorch.org/whl/cpu; \
		$(VENV)/bin/pip install --cache-dir $(PIP_CACHE) -r $(REQ); \
		touch "$(IOW_DIR)/.deps-ok"; \
	fi

ensure-ready:
	@if [ ! -x "$(VENV)/bin/pip" ] \
		|| ! PYTHONPATH="$(CURDIR):$(SITE_PACKAGES)" $(PYTHON) -c \
			"import chromadb,fastapi,uvicorn,sentence_transformers,httpx,yaml,docker,git" 2>/dev/null; then \
		echo "[!] IoW runtime not ready. Run: make setup"; exit 1; \
	fi
	@echo "[*] Runtime ready: $(VENV)"

ensure-target: ensure-dirs
	@if ! command -v docker >/dev/null 2>&1; then echo "[!] Docker required for target"; exit 1; fi
	@if ! docker info >/dev/null 2>&1; then echo "[!] Docker daemon unavailable"; exit 1; fi
	@if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx iow_demo_target; then \
		echo "[*] Target already up: iow_demo_target → $(TARGET_URL)"; \
	else \
		echo "[*] Starting iow_demo_target on :5001..."; \
		mkdir -p $(IOW_DIR) && chmod 777 $(IOW_DIR) 2>/dev/null || true; \
		docker compose up --build -d demo_app 2>/dev/null || docker-compose up --build -d demo_app; \
	fi

ensure-ollama: ensure-dirs
	@command -v ollama >/dev/null 2>&1 || { echo "ollama not found"; exit 1; }
	@if [ -f $(IOW_DIR)/ollama.pid ] && kill -0 $$(cat $(IOW_DIR)/ollama.pid) 2>/dev/null; then \
		echo "[*] Ollama already running (bind $(OLLAMA_BIND))"; \
	else \
		echo "[*] Starting ollama serve (models=$(OLLAMA_DIR), bind=$(OLLAMA_BIND))"; \
		nohup env HOME="$(IOW_DIR)" OLLAMA_MODELS="$(OLLAMA_DIR)" OLLAMA_HOST="$(OLLAMA_BIND)" \
			ollama serve >$(IOW_DIR)/logs/ollama.log 2>&1 & echo $$! > $(IOW_DIR)/ollama.pid; \
		sleep 2; \
	fi
	@HOME="$(IOW_DIR)" OLLAMA_MODELS="$(OLLAMA_DIR)" OLLAMA_HOST="$(OLLAMA_HOST)" ollama pull $(LLM_MODEL)

ensure-ollama-quick: ensure-dirs
	@command -v ollama >/dev/null 2>&1 || exit 0
	@if [ -f $(IOW_DIR)/ollama.pid ] && kill -0 $$(cat $(IOW_DIR)/ollama.pid) 2>/dev/null; then true; \
	else \
		echo "[*] Starting ollama serve (models=$(OLLAMA_DIR), bind=$(OLLAMA_BIND))"; \
		nohup env HOME="$(IOW_DIR)" OLLAMA_MODELS="$(OLLAMA_DIR)" OLLAMA_HOST="$(OLLAMA_BIND)" \
			ollama serve >$(IOW_DIR)/logs/ollama.log 2>&1 & echo $$! > $(IOW_DIR)/ollama.pid; \
		sleep 2; \
	fi

ensure-embed: ensure-deps
	@echo "[*] Prefetching embedding model $(EMBED_MODEL) into $(HF_HOME)"
	@$(VENV)/bin/python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('$(EMBED_MODEL)'); print('[+] embedding model ready')"

setup: ensure-deps ensure-ollama ensure-embed
	@echo
	@echo "[+] Setup complete under $(IOW_DIR)"
	@echo "    Next: make p1 | p2 | p3 | bonus | run  →  http://127.0.0.1:$(PORT)"
	@echo "    Lint:  make flake | mypy | lint"
	@echo "    Docker: make up | down | docker-restart | docker-clean | docker-fclean"

# ---------------------------------------------------------------------------
# Lint (flake8 + mypy) — IoC-style: tools live in $(VENV) under $(IOW_DIR).
# After `make fclean`, run `make lint` (or `make setup`) to reinstall.
# ---------------------------------------------------------------------------

ensure-lint-tools: ensure-venv
	@if $(VENV)/bin/python -c "import flake8, mypy" 2>/dev/null; then \
		echo "[*] Lint tools already installed in $(VENV)"; \
	else \
		echo "[*] Installing flake8 + mypy into $(VENV)"; \
		if ! $(VENV)/bin/pip install --cache-dir $(PIP_CACHE) flake8 'mypy<2' types-PyYAML; then \
			echo "[*] Campus index failed — retrying lint install via PyPI"; \
			$(VENV)/bin/pip install --cache-dir $(PIP_CACHE) \
				--index-url https://pypi.org/simple flake8 'mypy<2' types-PyYAML; \
		fi; \
	fi

flake: ensure-lint-tools
	@echo "[*] flake8 → $(LINT_DIRS)"
	@$(VENV)/bin/python -m flake8 $(LINT_DIRS)

mypy: ensure-lint-tools
	@echo "[*] mypy → $(LINT_DIRS)"
	@mkdir -p $(MYPY_CACHE)
	@PYTHONPATH="$(CURDIR):$(SITE_PACKAGES)" $(VENV)/bin/python -m mypy \
		--config-file mypy.ini --cache-dir $(MYPY_CACHE) $(LINT_DIRS)

lint: flake mypy
	@echo "[+] lint OK (flake8 + mypy)"

UVICORN_OPTS := --host 127.0.0.1 --port $(PORT) --timeout-graceful-shutdown 0

# Run uvicorn; treat Ctrl+C / SIGTERM as clean exit (no make "Interrupt" noise).
define IOW_UVICORN
	@bash -c 'set +e; \
		trap "exit 0" INT TERM; \
		IOW_MODE=$(1) TARGET_URL="$(TARGET_URL)" \
			$(PYTHON) -m uvicorn dashboard.app:app $(UVICORN_OPTS); \
		ec=$$?; \
		if [ $$ec -eq 0 ] || [ $$ec -eq 130 ] || [ $$ec -eq 143 ] || [ $$ec -eq 2 ]; then exit 0; fi; \
		exit $$ec'
endef

run: ensure-ready ensure-ollama-quick ensure-target
	@echo "[*] Full stack — all tabs unlocked on :$(PORT) (target auto-started)"
	@echo "    Dashboard: http://127.0.0.1:$(PORT)  Target: $(TARGET_URL)  Ollama: $(OLLAMA_HOST)"
	$(call IOW_UVICORN,full)

p1: ensure-ready ensure-ollama-quick ensure-target
	@echo "[*] Part 1 - Observer (Docker events & HTTP probes) on :$(PORT)"
	@echo "    tabs: all visible — Observer unlocked (Analyst/Loop/Bonus locked)"
	@echo "    target: iow_demo_target → $(TARGET_URL)  Ollama: $(OLLAMA_HOST)"
	$(call IOW_UVICORN,p1)

p2: ensure-ready ensure-ollama-quick ensure-target
	@echo "[*] Part 2 - Analyst (RAG retrieve & diagnose) on :$(PORT)"
	@echo "    tabs: all visible — Observer + Analyst unlocked"
	@echo "    target: iow_demo_target → $(TARGET_URL)  Ollama: $(OLLAMA_HOST)"
	$(call IOW_UVICORN,p2)

p3: ensure-ready ensure-ollama-quick ensure-target
	@echo "[*] Part 3 - Wisdom Loop (heal / rollback / safety) on :$(PORT)"
	@echo "    tabs: all visible — Observer + Analyst + Loop unlocked"
	@echo "    target: iow_demo_target → $(TARGET_URL)  Ollama: $(OLLAMA_HOST)"
	$(call IOW_UVICORN,p3)

bonus: ensure-ready ensure-ollama-quick ensure-target
	@echo "[*] Bonus Suite (classifier / consensus / PRs) on :$(PORT)"
	@echo "    tabs: all visible — all unlocked"
	@echo "    target: iow_demo_target → $(TARGET_URL)  Ollama: $(OLLAMA_HOST)"
	$(call IOW_UVICORN,bonus)

stop:
	@# Prefer pid file written by ensure-ollama*
	@if [ -f $(IOW_DIR)/ollama.pid ]; then \
		kill $$(cat $(IOW_DIR)/ollama.pid) 2>/dev/null || true; \
		rm -f $(IOW_DIR)/ollama.pid; \
		echo "[*] Stopped ollama via $(IOW_DIR)/ollama.pid"; \
	fi
	@# Orphans after clean/fclean (pid file already gone): only IoW-bound ollama
	@# (campus /opt/ollama and IoC :11435 are left alone). Match /proc environ —
	@# OLLAMA_* is not on argv, so plain pkill -f OLLAMA_MODELS=… misses orphans.
	@for pid in $$(pgrep -x ollama 2>/dev/null || true); do \
		envfile="/proc/$$pid/environ"; \
		[ -r "$$envfile" ] || continue; \
		if tr '\0' '\n' < "$$envfile" 2>/dev/null | grep -qFx 'OLLAMA_MODELS=$(OLLAMA_DIR)' \
			|| tr '\0' '\n' < "$$envfile" 2>/dev/null | grep -qFx 'OLLAMA_HOST=$(OLLAMA_BIND)' \
			|| tr '\0' '\n' < "$$envfile" 2>/dev/null | grep -qFx 'HOME=$(IOW_DIR)'; then \
			kill $$pid 2>/dev/null || true; \
			echo "[*] Stopped orphan IoW ollama pid $$pid"; \
		fi; \
	done
	@# Free IoW listen port if a leftover still holds it (11436 only)
	@if command -v fuser >/dev/null 2>&1; then \
		fuser -k 11436/tcp >/dev/null 2>&1 || true; \
	elif command -v lsof >/dev/null 2>&1; then \
		pids=$$(lsof -t -iTCP:11436 -sTCP:LISTEN 2>/dev/null || true); \
		[ -n "$$pids" ] && kill $$pids 2>/dev/null || true; \
	fi
	@rm -f $(IOW_DIR)/ollama.pid 2>/dev/null || true
	@echo "[*] stop done (IoW ollama on $(OLLAMA_BIND) / models $(OLLAMA_DIR))"

# Soft clean: stop ollama + docker containers/network + caches (keep venv + images)
clean: stop docker-clean
	@rm -rf $(CHROMA_DIR) $(PIP_CACHE) $(HF_HOME) $(MYPY_CACHE) \
		$(IOW_DIR)/logs $(IOW_DIR)/.deps-ok $(IOW_DIR)/cache \
		.chroma .mypy_cache .lint-venv 2>/dev/null || true
	@echo "[*] Cleaned caches and chroma under $(IOW_DIR) (venv kept)"

# Full clean: stop ollama + remove IoW docker image/network/volumes + wipe all IoW products
# (same contract as IoC: everything under /tmp/ioc goes away with fclean).
fclean: stop docker-fclean
	@echo "[*] Removing IoW runtime products ($(IOW_DIR) + legacy leftovers) ..."
	@rm -rf $(IOW_DIR) $(IOW_LEGACY) .chroma .mypy_cache .lint-venv 2>/dev/null || true
	@if [ -e $(IOW_DIR) ] || ls -d $(IOW_LEGACY) >/dev/null 2>&1; then \
		if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then \
			echo "[*] Residual IoW paths not user-removable — wiping via Docker"; \
			docker run --rm -v /tmp:/hosttmp alpine:3.20 sh -c \
				'rm -rf /hosttmp/iow /hosttmp/iow_cache /hosttmp/iow-lint \
				 /hosttmp/iow-lint-pip-cache /hosttmp/iow_docker' \
				>/dev/null 2>&1 || true; \
		fi; \
	fi
	@if [ -e $(IOW_DIR) ]; then \
		echo "[!] Could not fully remove $(IOW_DIR) — check: ls -la $(IOW_DIR)"; \
		ls -la $(IOW_DIR) 2>/dev/null || true; \
		exit 1; \
	fi
	@echo "[*] Removed $(IOW_DIR)"

re: fclean setup
