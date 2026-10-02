"""Part 2 (Analyst) regression tests - `make test-p2` (stdlib unittest, no model)."""

import logging

logging.disable(logging.CRITICAL)

# A small target module, shaped like demo_app/app.py (module code, a route, __main__).
APP_PY = '''"""Demo target."""
import os
from flask import Flask

app = Flask(__name__)


def index():
    return "ok"


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
'''
