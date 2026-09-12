# Inception-of-Wisdom (IoW)

A self-healing agent for a containerized service. It watches a running target,
notices when it breaks, asks a **local** LLM what is wrong, patches the code,
commits, restarts, and verifies the fix - rolling everything back through Git
if the symptom is still there.

Four beats, in a loop:

```
observe  ->  think  ->  act  ->  verify
   ^                                |
   +------------ rollback ----------+
```

* **Observe** - container state, live logs and HTTP probes of the target.
* **Think** - retrieve the relevant code chunks from a local ChromaDB index and
  ask a local model for a structured diagnosis.
* **Act** - generate a JSON patch, sanity-check it, apply it atomically, commit
  it on a dedicated branch.
* **Verify** - restart the target, wait out a grace period, and confirm no new
  crash event fires. Three failed attempts rewind the branch to the pre-loop
  revision.

Everything runs locally. **Any call to a remote LLM service is forbidden.**

---

## Requirements

| | |
|---|---|
| Docker + Docker Compose | the whole stack comes up with one command |
| Access to `/var/run/docker.sock` | the agent inspects and restarts sibling containers |
| A local LLM runtime | Ollama (reference), llama.cpp, vLLM, … running **on the host** |
| A coder model **under 3B parameters** | e.g. `qwen2.5-coder:1.5b` |
| Python 3.11+ | the agent itself |
| `make` | entry points |

The model runtime lives on the host, not in the compose stack - the agent
reaches it over the host network.

---

## Quick start

### Mode A: Docker Compose (Standard Evaluation)
```sh
make up        # Build + start the agent and demo target via Docker Compose
make logs      # Follow the container logs in real time
make break     # Trigger intentional crash on the target service
make heal      # Trigger manual heal cycle via REST API
make rollback  # Force emergency git rollback to pre-loop revision
make status    # Query live overview status via CLI
make down      # Tear everything down cleanly
```

### Mode B: Local Campus Runtime (under `/tmp/iow`)
```sh
make setup     # Prepare /tmp/iow (virtualenv, dependencies, embeddings, Ollama model)
make run       # Launch the unified Dashboard on http://127.0.0.1:8000
make p1        # Launch with Observer focus
make p2        # Launch with Analyst focus
make p3        # Launch with Wisdom Loop focus
make clean     # Clean ChromaDB and caches (keeps venv)
make fclean    # Complete wipe of /tmp/iow
```

Then open the dashboard in your browser:
```
http://localhost:8000
```

Three tabs, one per part:

1. **Observer** - target health, live logs, deduplicated crash events.
2. **Analyst** - free-text retrieval over the index, and the structured
   diagnosis of a selected crash event.
3. **Wisdom Loop** - every heal attempt: diagnosis, patch JSON, commit hash,
   post-restart health.

### Demo

1. Break the target on purpose (missing import, bad env var, syntax error) -
   e.g. `make break`.
2. Watch the Observer raise a crash event.
3. The Wisdom Loop picks it up, patches the target, commits on
   `iow/auto-heal`, restarts it and reports the target as healed.

Stick to explicit runtime failures the model can read straight from the log.
Business logic bugs are out of scope for a 3B model.

---

## Layout

```
.
├── p1/              Observer    - Docker events, log stream, HTTP probes
├── p2/              Analyst     - ChromaDB index, retrieval, LLM diagnosis
├── p3/              Wisdom Loop - patch generation, apply, commit, verify, rollback
├── bonus/           (optional)
├── docu/            subject
├── Makefile
└── README.md
```

---

## Configuration

All timing values live in `iow.config.yml` at the target project root - nothing
is hard-coded, so a reviewer can shorten the grace period for a smooth defense
without touching the source:

```yaml
grace_period: 20        # seconds the target must stay up to count as healed
cooldown: 300           # seconds before the same crash signature may retrigger
rate_limit_per_hour: 5  # heals started per hour
hard_cap: 20            # heals over the lifetime of the process
kill_switch: false      # flip to true to refuse every new heal
```

### Safety bounds

* One heal at a time (single-flight).
* Cooldown per crash signature.
* Rate limit per hour and a hard lifetime cap.
* A global kill-switch the operator can flip at any time.

A patch is refused before any disk write if it touches more than three files,
replaces a non-empty file with a trivial placeholder, shrinks a file by more
than 60 %, or leaks retrieval markers into its content. Patches are full-file
JSON (`{path, op, content}`, `op` ∈ `create` / `modify` / `delete`) - diffs are
not accepted.

### Hard and soft signals

* **Crash** (exit ≠ 0, restart loop, error pattern in the log, connection
  refused, timeout, 5xx) → the loop fixes it on its own.
* **Suggestion** (4xx, e.g. a missing route) → surfaced in the dashboard and
  left to a human. Never patched automatically.

---

## Notes

Mounting `/var/run/docker.sock` into a container grants root-equivalent access
to the host. Treat the agent image as privileged software and do not push it to
a public registry.

LLM weights, embedding model archives and populated ChromaDB directories are
never committed - they belong in `.gitignore`. The index is persistent across
restarts and updates incrementally in watch mode; it is never wiped and rebuilt
on start.

---

## About

42 project, beta. Builds on **Inception-of-Things** (deployment substrate) and
**Inception-of-Context** (retrieval and patching substrate). The subject is in
[`docu/`](docu/).
