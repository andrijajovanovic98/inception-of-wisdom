"""The system's entry points - Makefile, compose, images, entrypoint, GitOps manifests and
scripts, the dashboard page and the docs. Each of them once drifted from the code."""

from __future__ import annotations

import glob
import html
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from typing import Dict, List, Set

import yaml

from p3.git_manager import SOURCE_STAMP_RE

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DECKS = sorted(glob.glob(os.path.join(ROOT, "presentation", "*.html")))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _deck_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        page = re.sub(r"<(style|script)\b.*?</\1>", "", f.read(), flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", " ", page))


def _make_targets() -> Set[str]:
    """Every target the Makefile defines."""
    targets: Set[str] = set()
    for line in _read("Makefile").splitlines():
        match = re.match(r"^([A-Za-z0-9_.-][A-Za-z0-9_. -]*?)\s*:(?!=)", line)
        if match:
            targets.update(match.group(1).split())
    for line in _read("Makefile").splitlines():           # test-p1 test-p2 ...: test-%: ...
        if re.match(r"^[a-z0-9 -]+: [a-z]+-%:", line):
            targets.update(line.split(":")[0].split())
    return targets


def _documented_make_commands() -> Dict[str, List[str]]:
    """`make <target>` in the README's code and in the decks, by source."""
    found: Dict[str, List[str]] = {}
    readme = _read("README.md")
    code = "\n".join(re.findall(r"```.*?```", readme, flags=re.S) + re.findall(r"`[^`\n]+`", readme))
    found["README.md"] = re.findall(r"\bmake ([a-z][a-z0-9-]+)", code)
    for deck in DECKS:
        found[os.path.basename(deck)] = re.findall(r"\bmake ([a-z][a-z0-9-]+)", _deck_text(deck))
    return found


def _run(cmd: List[str], cwd: str = ROOT, env: Dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, timeout=120, check=False)


class MakefileTests(unittest.TestCase):
    def test_make_up_is_one_docker_compose_up(self) -> None:
        """M1: `make up` used to start only the target and Gitea, and stopped the agent."""
        dry_run = _run(["make", "-n", "--no-print-directory", "up"]).stdout
        self.assertIn("docker compose up --build -d", dry_run)

    def test_a_planted_bug_is_never_planted_twice(self) -> None:
        """M6: on a modified file each break target said "nothing to break" - and planted anyway."""
        work = tempfile.mkdtemp(prefix="iow-make-")
        try:
            shutil.copy(os.path.join(ROOT, "Makefile"), work)
            os.makedirs(os.path.join(work, "demo_app"))
            with open(os.path.join(work, "demo_app", "app.py"), "w", encoding="utf-8") as f:
                f.write("already = 'modified'\n")
            out = _run(["make", "-s", "break-import", "break-env", "break-syntax",
                        "DOCKER_TARGET=iow-test-none"], cwd=work).stdout
            with open(os.path.join(work, "demo_app", "app.py"), encoding="utf-8") as f:
                self.assertEqual(f.read(), "already = 'modified'\n")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        self.assertEqual(out.count("nothing to break"), 3)
        self.assertNotIn("restarting target", out)

    def test_make_stop_only_ever_stops_ollama(self) -> None:
        """M3: `fuser -k` on the port killed rootlesskit - the parent of the Docker daemon."""
        stop = _read("Makefile").split("\nstop:", 1)[1].split("\n\n", 1)[0]
        self.assertEqual([line for line in stop.splitlines() if "fuser" in line and "#" not in line], [])
        self.assertIn('= "ollama"', stop)

    def test_every_documented_make_command_exists(self) -> None:
        """D6: the decks described `make` workflows the Makefile no longer had."""
        targets = _make_targets()
        for source, commands in _documented_make_commands().items():
            missing = sorted({c for c in commands if not c.endswith("-")} - targets)   # break-* is a family
            self.assertEqual(missing, [], f"{source} documents make targets that do not exist")

    def test_the_operator_commands_follow_port_and_clean_up_ollama(self) -> None:
        """M5: `make heal` / `rollback` / `status` talked to :8000 whatever PORT said, and the
        docker cleanups left `iow_ollama`, its volume and its image behind."""
        dry_run = _run(["make", "-n", "--no-print-directory", "heal", "rollback", "status",
                        "PORT=18123"]).stdout
        for route in ("loop/trigger", "loop/rollback", "overview"):
            self.assertIn(f"http://127.0.0.1:18123/api/{route}", dry_run)
        self.assertNotIn(":8000", dry_run)
        makefile = _read("Makefile")
        clean = makefile.split("\ndocker-clean:", 1)[1].split("\n\n", 1)[0]
        fclean = makefile.split("\ndocker-fclean:", 1)[1].split("\n\n", 1)[0]
        self.assertIn("iow_ollama", clean)
        for leftover in ("iow_ollama", "iow-ollama-models", "ollama/ollama"):
            self.assertIn(leftover, fclean)

    def test_help_shows_every_demo(self) -> None:
        help_text = _run(["make", "--no-print-directory", "help"]).stdout
        for target in ("up", "down", "break-import", "break-env", "break-syntax", "break-hard",
                       "demo-reset", "heal", "gates", "argocd-bonus", "gitops-publish"):
            self.assertRegex(help_text, rf"make {re.escape(target)}\b", target)


class ContainerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compose = yaml.safe_load(_read("docker-compose.yml"))["services"]

    def test_one_compose_up_brings_up_the_agent_the_llm_and_the_target(self) -> None:
        """Subject IV: a single docker compose up; the agent reaches its siblings and the repo."""
        for service in ("agent", "ollama", "demo_app"):
            self.assertIn(service, self.compose)
            self.assertNotIn("profiles", self.compose[service])
        volumes = self.compose["agent"]["volumes"]
        self.assertIn("${DOCKER_SOCK:-/var/run/docker.sock}:/var/run/docker.sock", volumes)
        self.assertIn(".:/workspace", volumes)
        self.assertIn("/tmp/iow:/tmp/iow", volumes)                     # one index for host and container

    def test_the_images_are_named_by_the_project_not_the_checkout(self) -> None:
        """M8: compose named the images after the checkout's directory, while the GitOps
        bootstrap, the k8s manifest and `make docker-fclean` use inception-of-wisdom-*: in a
        clone under any other name the bootstrap stopped at "missing after build"."""
        name = yaml.safe_load(_read("docker-compose.yml"))["name"]
        self.assertEqual(name, "inception-of-wisdom")
        for rel in ("scripts/argocd_bonus_up.sh", "k8s/demo-app/deployment.yaml"):
            self.assertIn(f"{name}-demo_app:latest", _read(rel), rel)
        makefile = _read("Makefile")
        self.assertRegex(makefile, rf"DOCKER_IMAGE_AGENT\s*:=\s*{name}-agent\n")
        self.assertRegex(makefile, rf"DOCKER_IMAGE_DEMO\s*:=\s*{name}-demo_app\n")

    def test_nothing_is_published_beyond_this_machine(self) -> None:
        """M9: the dashboard (an unauthenticated control API in front of the Docker socket), the
        target and Gitea (whose admin password the README prints) listened on every interface."""
        published = [(service, str(port)) for service, spec in self.compose.items()
                     for port in spec.get("ports", [])]
        self.assertEqual(len(published), 3)
        for service, port in published:
            self.assertTrue(port.startswith("127.0.0.1:"), f"{service}: {port}")

    def test_the_model_runtime_publishes_no_host_port(self) -> None:
        """M2: its :11436 collided with the host Ollama `make setup` starts."""
        self.assertNotIn("ports", self.compose["ollama"])

    def test_the_agent_does_not_wait_for_the_bonus_forge(self) -> None:
        """M5: the mandatory agent waited for Gitea's health check."""
        self.assertEqual(self.compose["agent"]["depends_on"]["gitea"]["condition"], "service_started")

    def test_the_target_stops_in_well_under_a_second(self) -> None:
        """O8: Python as PID 1 ignored SIGTERM - every restart waited 10 s for SIGKILL."""
        self.assertIn("STOPSIGNAL SIGINT", _read("demo_app/Dockerfile"))

    def test_the_agent_image(self) -> None:
        """M5 / M4: CPU torch (not 3 GB of CUDA), tests and .git kept out, the uid entrypoint."""
        dockerfile = _read("Dockerfile")
        self.assertIn("--index-url https://download.pytorch.org/whl/cpu", dockerfile)
        self.assertIn('ENTRYPOINT ["/usr/local/bin/iow-agent-entrypoint"]', dockerfile)
        ignored = _read(".dockerignore").split()
        for entry in (".git", "**/tests", "docu", "presentation"):
            self.assertIn(entry, ignored)


class EntrypointTests(unittest.TestCase):
    """M4: the agent writes the checkout's files and git objects as the checkout's owner.

    `id`, `stat` and `setpriv` are stand-ins, so both daemons can be checked anywhere:
    rootful (the checkout belongs to a user - drop to that uid, keep the socket's group) and
    rootless (the host user already is uid 0 in the container - change nothing).
    """

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="iow-entry-")
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        os.makedirs(os.path.join(self.dir, "workspace"))
        shims = {
            "id": 'echo "${FAKE_ID:-0}"',
            "stat": 'echo "${FAKE_OWNER:-0}"',
            "setpriv": 'echo "SETPRIV $*"',
            "chown": "true",
        }
        for name, body in shims.items():
            path = os.path.join(self.bin, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"#!/bin/sh\n{body}\n")
            os.chmod(path, 0o755)

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def _start(self, uid: str, owner: str) -> str:
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ.get('PATH', '')}", FAKE_ID=uid,
                   FAKE_OWNER=owner, IOW_REPO_PATH=os.path.join(self.dir, "workspace"),
                   IOW_CACHE_DIR=os.path.join(self.dir, "runtime"))
        return _run(["sh", os.path.join(ROOT, "scripts", "agent_entrypoint.sh"), "echo", "AGENT"],
                    env=env).stdout

    def test_rootful_docker_drops_to_the_checkouts_owner(self) -> None:
        out = self._start(uid="0", owner="1000")
        self.assertIn("running as uid=1000 gid=1000", out)
        self.assertRegex(
            out, r"SETPRIV --reuid=1000 --regid=1000 --groups=1000\S* --inh-caps=-all -- echo AGENT"
        )

    def test_rootless_docker_changes_nothing(self) -> None:
        out = self._start(uid="0", owner="0")
        self.assertIn("rootless Docker maps the host user", out)
        self.assertIn("AGENT", out)
        self.assertNotIn("SETPRIV", out)


class DemoTargetTests(unittest.TestCase):
    def test_the_crash_routes_the_makefile_calls_exist(self) -> None:
        """O2: `make break-hard` called a route that did not exist (404)."""
        routes = set(re.findall(r'@app\.route\("([^"]+)"', _read("demo_app/app.py")))
        makefile = _read("Makefile")
        for target, path in (("break", "/api/crash"), ("break-hard", "/api/crash-hard")):
            self.assertIn(f"$(TARGET_URL){path}", makefile, target)
            self.assertIn(path, routes, target)
        self.assertNotIn("/api/missing-route", routes)          # the 4xx suggestion stays a 4xx


class GitOpsFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.docs = list(yaml.safe_load_all(_read("k8s/demo-app/deployment.yaml")))

    def test_pods_reach_gitea_through_the_cluster_dns(self) -> None:
        """B4: the compose name iow_gitea does not resolve in a pod - Init:CrashLoopBackOff."""
        config = next(d for d in self.docs if d["kind"] == "ConfigMap")["data"]
        self.assertIn("gitea.iow.svc.cluster.local", config["repo_url"])
        app = yaml.safe_load(_read("k8s/argocd/application.yaml"))["spec"]
        self.assertEqual((app["source"]["targetRevision"], config["revision"]), ("iow/auto-heal",) * 2)
        self.assertTrue(app["syncPolicy"]["automated"]["selfHeal"])

    def test_the_commit_alone_is_the_redeploy(self) -> None:
        """B2: the pod template carries the source stamp every commit sets; Argo CD polls fast."""
        deployment = next(d for d in self.docs if d["kind"] == "Deployment")
        self.assertIn("iow.42/source", deployment["spec"]["template"]["metadata"]["annotations"])
        self.assertRegex(_read("k8s/demo-app/deployment.yaml"), SOURCE_STAMP_RE)   # GitManager stamps it
        bootstrap = _read("scripts/argocd_bonus_up.sh")
        self.assertIn('"timeout.reconciliation":"15s"', bootstrap)
        self.assertIn("iow\\.42/source", _read("scripts/gitops_publish.sh"))

    def test_the_init_containers_image_is_imported_for_this_platform(self) -> None:
        """G3: a plain `ctr import` of a multi-platform image failed, and the cluster pulled it."""
        bootstrap = _read("scripts/argocd_bonus_up.sh").splitlines()
        imports = [line for line in bootstrap if "images import" in line]
        self.assertTrue(imports)
        for line in imports:
            self.assertIn('--platform "${platform}"', line)

    def test_tokens_travel_in_headers_not_urls(self) -> None:
        """G1 / B2: a token in a remote URL ends up in .git/config and in process listings."""
        publish = _read("scripts/gitops_publish.sh")
        self.assertIn("http.extraHeader=Authorization: token", publish)
        self.assertNotRegex(publish, r"https?://\$\{?GITEA_(USER|TOKEN)")

    def test_every_script_parses(self) -> None:
        for script in sorted(glob.glob(os.path.join(ROOT, "scripts", "*.sh"))):
            with open(script, encoding="utf-8") as f:
                shell = "bash" if "bash" in f.readline() else "sh"
            result = _run([shell, "-n", script])
            self.assertEqual(result.returncode, 0, f"{script}: {result.stdout}")

    def test_argo_cd_is_never_forwarded_onto_a_busy_port(self) -> None:
        """B15: 8080 belonged to another project here, and the agent's `argocd login` would
        have sent the admin password to it."""
        up = _read("scripts/argocd_bonus_up.sh")
        self.assertIn('ARGOCD_PORT="${IOW_ARGOCD_PORT:-8080}"', up)
        self.assertRegex(up, r'ss -ltn .*\$\{ARGOCD_PORT\}')
        self.assertIn("is taken by another program - no Argo CD port-forward", up)

    def test_publishing_stamps_the_code_and_leaves_the_roll_to_argo_cd(self) -> None:
        """G1 / G4: `make gitops-publish` pushes the working tree's demo_app/ with its source
        stamp, and Argo CD rolls the pod; publishing the same code again restarts it by hand."""
        from p3.tests.helpers import TempRepo
        checkout, work = TempRepo(), tempfile.mkdtemp(prefix="iow-publish-")
        try:
            os.makedirs(os.path.join(checkout.path, "scripts"))
            shutil.copy(os.path.join(ROOT, "scripts", "gitops_publish.sh"),
                        os.path.join(checkout.path, "scripts"))
            remote, seed = os.path.join(work, "remote.git"), os.path.join(work, "seed")
            _run(["git", "init", "-q", "--bare", remote])
            os.makedirs(os.path.join(seed, "k8s", "demo-app"))
            os.makedirs(os.path.join(seed, "demo_app"))
            shutil.copy(os.path.join(ROOT, "k8s", "demo-app", "deployment.yaml"),
                        os.path.join(seed, "k8s", "demo-app"))
            with open(os.path.join(seed, "demo_app", "app.py"), "w", encoding="utf-8") as f:
                f.write("print('what the cluster ran before')\n")
            ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
            for cmd in (["init", "-q"], ["add", "-A"], [*ident, "commit", "-qm", "seed"],
                        ["push", "-q", remote, "HEAD:refs/heads/iow/auto-heal"]):
                self.assertEqual(_run(["git", *cmd], cwd=seed).returncode, 0, cmd)
            iow = os.path.join(work, "iow")
            os.makedirs(os.path.join(iow, "gitea"))
            os.makedirs(os.path.join(iow, "bin"))
            with open(os.path.join(iow, "gitea", "gitea.env"), "w", encoding="utf-8") as f:
                f.write("GITEA_TOKEN=secret-token\n")
            calls = os.path.join(work, "kubectl.calls")
            kubectl = os.path.join(iow, "bin", "kubectl")
            with open(kubectl, "w", encoding="utf-8") as f:
                f.write(f'#!/bin/sh\necho "$*" >> "{calls}"\n')
            os.chmod(kubectl, 0o755)
            env = dict(os.environ, IOW_DIR=iow, IOW_GITOPS_REMOTE=f"file://{remote}")
            script = os.path.join(checkout.path, "scripts", "gitops_publish.sh")

            first = _run(["bash", script], env=env)
            self.assertEqual(first.returncode, 0, first.stdout)
            self.assertIn("Argo CD syncs the commit", first.stdout)
            show = lambda what: _run(["git", "--git-dir", remote, *what]).stdout.strip()  # noqa: E731
            published = show(["show", "iow/auto-heal:demo_app/app.py"])
            self.assertEqual(published, checkout.read("demo_app/app.py").strip())
            stamp = SOURCE_STAMP_RE.search(show(["show", "iow/auto-heal:k8s/demo-app/deployment.yaml"]))
            self.assertIsNotNone(stamp)
            self.assertIn(show(["rev-parse", "iow/auto-heal:demo_app"]), stamp.group(0) if stamp else "")
            with open(calls, encoding="utf-8") as f:
                self.assertNotIn("rollout restart", f.read())                # Argo CD rolls it

            again = _run(["bash", script], env=env)
            self.assertIn("already holds this demo_app/", again.stdout)
            with open(calls, encoding="utf-8") as f:
                self.assertIn("rollout restart deployment/iow-demo", f.read())
            self.assertNotIn("secret-token", first.stdout + again.stdout)
        finally:
            checkout.cleanup()
            shutil.rmtree(work, ignore_errors=True)


class DashboardPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page = _read("dashboard/templates/index.html")
        self.defined = set(re.findall(r"function\s+([A-Za-z_]\w*)\s*\(", self.page))
        arrow = r"(?:const|let|var)\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?\("
        self.defined |= set(re.findall(arrow, self.page))

    def test_every_handler_the_page_calls_exists(self) -> None:
        """D1: pollObserverLogs was called but never defined - the page never refreshed."""
        handler = r'on(?:click|change|input|submit)="\s*(?:return\s+)?([A-Za-z_]\w*)\('
        called = set(re.findall(handler, self.page))
        called |= set(re.findall(r"setInterval\(\s*([A-Za-z_]\w*)", self.page))
        called |= set(re.findall(r"addEventListener\('[a-z]+',\s*([A-Za-z_]\w*)\)", self.page))
        self.assertEqual(sorted(called - self.defined), [])

    def test_model_text_is_escaped_also_inside_attributes(self) -> None:
        """D3: model output went into innerHTML unescaped; quotes were not escaped."""
        escape = self.page.split("function escapeHtml", 1)[1].split("}", 1)[0]
        for entity in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
            self.assertIn(entity, escape)

    def test_a_refused_action_is_never_shown_as_a_success(self) -> None:
        """D5: a failed approval or rejection was shown as a success, *Trigger Heal* ignored
        HTTP errors, and the reviewer's comment went into the page unescaped."""
        for name in ("triggerManualHeal", "triggerManualRollback", "healSignature", "mergePR", "rejectPR"):
            rest = self.page.split(f"function {name}(", 1)[1]
            self.assertIn("!res.ok", re.split(r"\n    (?:async\s+)?function\s", rest, 1)[0], name)
        self.assertIn("escapeHtml(pr.reviewer_comment", self.page)

    def test_what_the_page_sends_and_keeps(self) -> None:
        """D4: *Insert Latest Error* and *Open Gitea Pull Request* used a made-up
        ZeroDivisionError in a function that does not exist; the error excerpt sent to the
        model grew without bound; *Clear* cleared the view only; every refresh collapsed the
        cycles a reader had opened."""
        self.assertNotIn("ZeroDivisionError", self.page)
        self.assertRegex(self.page, r"const MAX_ERROR_EXCERPT_LINES = \d+;")
        self.assertIn("kept.slice(-MAX_ERROR_EXCERPT_LINES", self.page)
        clear = self.page.split("async function clearLogs(", 1)[1].split("\n    }", 1)[0]
        self.assertIn("/api/observer/logs/clear", clear)
        self.assertIn("openCycles.has(c.cycle_id)", self.page)
        # ... and the cards kept their size inside the scrolling list instead of being cut off.
        self.assertRegex(self.page, r"\.cycle-card, \.attempt-box \{ flex-shrink: 0; \}")
        # Badges were clipped: the heal status cell and the flag toggles may not squeeze them.
        self.assertIn('<td style="white-space: nowrap;">${healCell}</td>', self.page)
        self.assertIn(".toggle-row { display: flex; flex-wrap: wrap;", self.page)

    def test_the_diagnosis_is_asked_for_a_crash_event(self) -> None:
        """A10: subject V.2's diagnosis takes a crash event (status, log excerpt, exit code); the
        Analyst tab could only insert the latest crash's text, and sent neither of the others."""
        self.assertIn('id="diagEventSelect"', self.page)
        self.assertIn('onchange="pickCrashEvent()"', self.page)
        diagnose = self.page.split("async function requestDiagnosis(", 1)[1].split("\n    }\n", 1)[0]
        for field in ("log_excerpt:", "status:", "exit_code:"):
            self.assertIn(field, diagnose)
        picker = self.page.split("async function refreshDiagEvents(", 1)[1].split("\n    }\n", 1)[0]
        self.assertIn("/api/observer/events?type=crash", picker)
        self.assertIn("value=\"${escapeHtml(ev.id)}\"", picker)       # by event, not signature

    def test_the_verification_verdict_reads_the_real_field(self) -> None:
        """D2: every attempt showed UNVERIFIED - the page read `healed`, the field is `is_healed`."""
        self.assertIn("is_healed", self.page)
        self.assertNotRegex(self.page, r"verification\.healed\b")


class HealHistoryContractTests(unittest.TestCase):
    """D2 / D4 / bonus 5: the heal history shows only what a cycle record carries. The page
    once read `verification.healed`, which no record has, and every attempt read UNVERIFIED."""

    @staticmethod
    def _fields(function: str, var: str) -> Set[str]:
        page = _read("dashboard/templates/index.html")
        rest = page.split(f"function {function}(", 1)[1]
        body = re.split(r"\n    (?:async\s+)?function\s", rest, maxsplit=1)[0]
        return set(re.findall(rf"(?<![\w.]){var}\.([a-z_]+)", body))

    def test_every_field_the_heal_history_reads_is_in_the_record(self) -> None:
        from p3.git_manager import GitManager
        from p3.loop import WisdomLoop
        from p3.sanity import SanityChecker
        from p3.tests.helpers import TempRepo
        from p3.tests.test_loop import (FakeDiagnostician, FakeDocker, FakePatcher, FakeVerifier,
                                        _crash, _fixed)

        repo = TempRepo()
        try:
            gm = GitManager(repo_dir=repo.path)

            def cycle(answers: List[bool]) -> Dict:
                fixes = [_fixed(repo, f"a{i}") for i in range(len(answers))]
                loop = WisdomLoop(
                    git_manager=gm, diagnostician=FakeDiagnostician(),  # type: ignore[arg-type]
                    patcher=FakePatcher(fixes),  # type: ignore[arg-type]
                    sanity_checker=SanityChecker(base_dir=repo.path, path_guard=gm._is_heal_path),
                    verifier=FakeVerifier(answers), docker_monitor=FakeDocker(),  # type: ignore[arg-type]
                )
                return loop.execute_heal(_crash(), grace_period=0.0).to_dict()

            healed, rolled_back = cycle([False, True]), cycle([False, False, False])
        finally:
            repo.cleanup()
        self.assertEqual((healed["status"], rolled_back["status"]), ("healed", "rolled_back"))
        for record in (healed, rolled_back):
            self.assertLessEqual(self._fields("refreshLoopHistory", "c"), set(record))
        attempt = healed["attempts"][-1]
        parts = {"a": attempt, "d": attempt["diagnosis"], "v": attempt["verification"],
                 "patch": attempt["patch"], "sanity": attempt["sanity"]}
        for var, carried in parts.items():
            self.assertLessEqual(self._fields("renderAttempt", var), set(carried), var)
        self.assertTrue(attempt["patch"]["diff"])                       # the coloured diff (bonus 5)


class DocsTests(unittest.TestCase):
    """D6: the decks described a system that no longer existed."""

    def test_files_the_docs_name_exist(self) -> None:
        pattern = r"\b((?:p1|p2|p3|bonus|dashboard|demo_app|scripts|k8s)/[\w./-]+\.(?:py|sh|yml|yaml|html))\b"
        sources = {"README.md": _read("README.md")}
        sources.update({os.path.basename(d): _deck_text(d) for d in DECKS})
        for source, text in sources.items():
            named = set(re.findall(pattern, text))
            missing = sorted(p for p in named if not os.path.exists(os.path.join(ROOT, p)))
            self.assertEqual(missing, [], source)

    def test_patch_examples_use_the_subjects_ops(self) -> None:
        for deck in DECKS:
            ops = set(re.findall(r'"op":\s*"(\w+)"', _deck_text(deck)))
            self.assertLessEqual(ops, {"create", "modify", "delete"}, deck)

    def test_config_keys_the_docs_name_exist(self) -> None:
        config = yaml.safe_load(_read("demo_app/iow.config.yml"))
        texts = [_read("README.md")] + [_deck_text(d) for d in DECKS]
        for text in texts:
            for section, key in re.findall(r"(?<![/\w])(target|analyst)\.([a-z_]+)\b", text):
                self.assertIn(key, config[section], f"{section}.{key}")

    def test_the_readme_claims_only_what_holds(self) -> None:
        """U2: "Deterministic Incident Response", "100% Air-Gapped", "Zero-Downtime": the loop
        samples, the first setup downloads packages, images and models, and a heal restarts
        the target."""
        readme = _read("README.md").lower()
        for claim in ("deterministic incident", "air-gapped", "air--gapped", "zero-downtime",
                      "continuous availability", "production-grade"):
            self.assertNotIn(claim, readme)

    def test_approvals_are_described_with_the_bounds_they_have(self) -> None:
        """D9: the README said an approval lands under the rate limit and the hard cap; it is
        counted against neither on purpose (B13) - only the kill switch and single-flight."""
        readme = _read("README.md")
        self.assertNotIn("(kill switch, single-flight, rate limit, hard cap)", readme)
        self.assertIn("not counted against the hourly rate limit or the lifetime cap", readme)
        for deck in DECKS:
            self.assertNotIn("under the heal bounds", _deck_text(deck), deck)

    def test_no_emoji_and_no_literal_em_dash(self) -> None:
        """The author's rule: no emoji anywhere; no em dash in code, and in HTML or Markdown
        only as the entity &mdash;."""
        emoji = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B50\uFE0F]")
        names = ["README.md", "Makefile", "Dockerfile", "docker-compose.yml", "docu/AUDIT.md"]
        for part in ("p1", "p2", "p3", "bonus", "dashboard", "demo_app", "scripts", "k8s", "presentation"):
            for path in glob.glob(os.path.join(ROOT, part, "**", "*"), recursive=True):
                if os.path.isfile(path) and not path.endswith((".jpg", ".png", ".pdf", ".pyc")):
                    names.append(os.path.relpath(path, ROOT))
        for name in names:
            if not os.path.isfile(os.path.join(ROOT, name)):
                continue                                  # docu/AUDIT.md is local only
            text = _read(name)
            self.assertIsNone(emoji.search(text), name)
            self.assertNotIn("\u2014", text, name)

    def test_the_decks_fit_a_1366x768_screen(self) -> None:
        """D7: on a 1366x768 laptop two slides overflowed by 7-24 px (measured in Chrome)."""
        for deck in DECKS:
            with open(deck, encoding="utf-8") as f:
                self.assertIn("@media (max-height: 800px) { .slide { padding: 16px 56px; } }", f.read(), deck)

    def test_no_raw_latex_or_retired_numbers_in_the_decks(self) -> None:
        for deck in DECKS:
            text = _deck_text(deck)
            for stale in ("$\\to$", "0.01s", "0.01 s", "0.01 mp", "ZeroDivisionError", "mixed reset"):
                self.assertNotIn(stale, text, f"{os.path.basename(deck)}: {stale}")


if __name__ == "__main__":
    unittest.main()
