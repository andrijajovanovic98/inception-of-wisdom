"""J7: a rewrite that reads a value it sets on some paths only - caught before any write."""

from __future__ import annotations

import textwrap
import unittest

from p3.unbound import reads_before_set, unset_in_a_new_way

ORIGINAL = textwrap.dedent('''\
    import os
    from flask import Flask

    app = Flask(__name__)

    if __name__ == "__main__":
        app.run(host="0.0.0.0", port=int(os.environ["IOW_DEMO_PORT"]))
''')


def _main(body: str) -> str:
    """ORIGINAL with its startup block replaced by `body` (dedented, then indented once)."""
    head = ORIGINAL.split('if __name__ == "__main__":', 1)[0]
    return head + 'if __name__ == "__main__":\n' + textwrap.indent(textwrap.dedent(body), "    ")


class UnsetNameTests(unittest.TestCase):
    def test_the_break_env_rewrites_that_fail_with_a_name_error(self) -> None:
        """Seen live: the ninth rewrite of Q1 set `port` only when the variable was there."""
        only_if = _main('''\
            if "IOW_DEMO_PORT" in os.environ:
                port = int(os.environ["IOW_DEMO_PORT"])
            app.run(host="0.0.0.0", port=port)
        ''')
        swallowed = _main('''\
            try:
                port = int(os.environ["IOW_DEMO_PORT"])
            except KeyError:
                print("IOW_DEMO_PORT is not set")
            app.run(host="0.0.0.0", port=port)
        ''')
        for broken in (only_if, swallowed):
            why = unset_in_a_new_way(ORIGINAL, broken)
            self.assertIsNotNone(why, broken)
            self.assertIn("reads port on line", why or "")

    def test_values_set_on_every_path_pass(self) -> None:
        fixes = [
            '''\
            port = int(os.environ.get("IOW_DEMO_PORT", 5000))
            app.run(host="0.0.0.0", port=port)
            ''',
            '''\
            port = 5000
            if "IOW_DEMO_PORT" in os.environ:
                port = int(os.environ["IOW_DEMO_PORT"])
            app.run(host="0.0.0.0", port=port)
            ''',
            '''\
            if "IOW_DEMO_PORT" in os.environ:
                port = int(os.environ["IOW_DEMO_PORT"])
            else:
                port = 5000
            app.run(host="0.0.0.0", port=port)
            ''',
            '''\
            try:
                port = int(os.environ["IOW_DEMO_PORT"])
            except (KeyError, ValueError):
                port = 5000
            app.run(host="0.0.0.0", port=port)
            ''',
            '''\
            try:
                port = 5000
                port = int(os.environ["IOW_DEMO_PORT"])
            except KeyError:
                pass
            app.run(host="0.0.0.0", port=port)
            ''',
            '''\
            if "IOW_DEMO_PORT" in os.environ:
                port = int(os.environ["IOW_DEMO_PORT"])
            else:
                exit(1)
            app.run(host="0.0.0.0", port=port)
            ''',
            '''\
            for attempt in range(3):
                try:
                    port = int(os.environ["IOW_DEMO_PORT"])
                    break
                except KeyError:
                    pass
            else:
                port = 5000
            app.run(host="0.0.0.0", port=port)
            ''',
            '''\
            while True:
                try:
                    port = int(os.environ["IOW_DEMO_PORT"])
                    break
                except KeyError:
                    os.environ["IOW_DEMO_PORT"] = "5000"
            app.run(host="0.0.0.0", port=port)
            ''',
            '''\
            if (raw := os.environ.get("IOW_DEMO_PORT")) is None:
                raw = "5000"
            app.run(host="0.0.0.0", port=int(raw))
            ''',
            '''\
            ports = [int(p) for p in os.environ.get("PORTS", "5000").split(",")]
            for port in ports:
                last = port
            app.run(host="0.0.0.0", port=last)
            ''',
            '''\
            with open(os.devnull) as sink:
                port = 5000
            app.run(host="0.0.0.0", port=port, debug=sink.closed)
            ''',
        ]
        for body in fixes:
            self.assertEqual(reads_before_set(_main(body)), {}, body)     # read, and nothing unset
            self.assertIsNone(unset_in_a_new_way(ORIGINAL, _main(body)), body)

    def test_function_locals_are_their_own_scope(self) -> None:
        before = "def handler(flag):\n    return 1\n"
        broken = "def handler(flag):\n    if flag:\n        value = 2\n    return value\n"
        uses_param = "def handler(flag):\n    if flag:\n        flag = 2\n    return flag\n"
        uses_global = "VALUE = 1\n\ndef handler(flag):\n    return VALUE\n"
        declared = "def handler(flag):\n    global VALUE\n    if flag:\n        VALUE = 2\n    return VALUE\n"
        self.assertIn("in handler()", unset_in_a_new_way(before, broken) or "")
        for fine in (uses_param, uses_global, declared):
            self.assertEqual(reads_before_set(fine), {}, fine)

    def test_only_what_the_patch_adds_counts(self) -> None:
        """A file that already reads a name on one path only is not the patch's doing."""
        already = _main('''\
            if "IOW_DEMO_PORT" in os.environ:
                port = int(os.environ["IOW_DEMO_PORT"])
            app.run(host="0.0.0.0", port=port)
        ''')
        edited = already.replace("0.0.0.0", "127.0.0.1")
        self.assertIsNotNone(reads_before_set(already))
        self.assertIsNone(unset_in_a_new_way(already, edited))

    def test_what_cannot_be_read_statically_is_left_alone(self) -> None:
        """A comprehension's own variable, a lambda body, a builtin at module level, a
        star import, and code that does not parse."""
        fine = [
            "squares = [n * n for n in range(3)]\n",
            "late = lambda: later\nlater = 1\n",
            "if False:\n    input = None\nprint(input)\n",
        ]
        for text in fine:
            self.assertEqual(reads_before_set(text), {}, text)
        self.assertIsNone(reads_before_set("from os import *\nprint(path)\n"))
        self.assertIsNone(reads_before_set("def broken(:\n"))
        self.assertIsNone(unset_in_a_new_way("x = 1\n", "def broken(:\n"))


if __name__ == "__main__":
    unittest.main()
