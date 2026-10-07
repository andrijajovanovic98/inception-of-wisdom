"""System regression tests - `make test-dashboard` (stdlib unittest, no model, no Docker).

The orchestrator that joins the parts, and the entry points around it: Makefile, compose,
Dockerfiles, the agent's entrypoint, the GitOps manifests and scripts, the dashboard page,
and the docs that describe them.
"""

import logging

logging.disable(logging.CRITICAL)
