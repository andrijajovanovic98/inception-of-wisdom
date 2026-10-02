"""Wisdom Loop fixture: a throwaway git repository shaped like this project."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Dict

from p2.tests import APP_PY


class TempRepo:
    """git repo with demo_app/ (heal scope) and p3/ (agent source, off limits)."""

    def __init__(self) -> None:
        self.path = tempfile.mkdtemp(prefix="iow-test-repo-")
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@iow.local")
        self.git("config", "user.name", "test")
        self.write("demo_app/app.py", APP_PY)
        self.write("demo_app/run.sh", "#!/bin/sh\necho run\n")
        os.chmod(self.abs("demo_app/run.sh"), 0o755)
        self.write("demo_app/notes.txt", "keep me\n")
        self.write("demo_app/iow.config.yml", "grace_period: 20\n")
        self.write("p3/agent.py", "AGENT = 1\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "initial")

    def abs(self, rel: str) -> str:
        return os.path.join(self.path, rel)

    def write(self, rel: str, content: str) -> None:
        os.makedirs(os.path.dirname(self.abs(rel)), exist_ok=True)
        with open(self.abs(rel), "w", encoding="utf-8") as f:
            f.write(content)

    def read(self, rel: str) -> str:
        with open(self.abs(rel), "r", encoding="utf-8") as f:
            return f.read()

    def git(self, *args: str) -> str:
        res = subprocess.run(
            ["git", *args], cwd=self.path, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        return res.stdout.strip()

    def snapshot_files(self, root: str = "demo_app") -> Dict[str, bytes]:
        """Every file under root with its exact bytes and mode."""
        out: Dict[str, bytes] = {}
        base = self.abs(root)
        for dirpath, _, files in os.walk(base):
            for name in files:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, self.path)
                with open(full, "rb") as f:
                    out[rel] = f.read() + b"|mode=" + oct(os.stat(full).st_mode & 0o777).encode()
        return out

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
