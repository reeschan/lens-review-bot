import base64
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bundle

ROOT = Path(__file__).resolve().parents[1]
LENS_BASE = f"repos/{bundle.LENS_REPO}/contents/{bundle.LENS_ROOT}"


def file_diff(path, old_lines, new_lines, start=1):
    """A one-hunk diff for a modified file."""
    lines = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}",
             f"@@ -{start},{len(old_lines)} +{start},{len(new_lines)} @@"]
    lines += [f"-{line}" for line in old_lines] + [f"+{line}" for line in new_lines]
    return "\n".join(lines) + "\n"


def new_file_diff(path, new_lines):
    lines = [f"diff --git a/{path} b/{path}", "new file mode 100644", "--- /dev/null",
             f"+++ b/{path}", f"@@ -0,0 +1,{len(new_lines)} @@"]
    lines += [f"+{line}" for line in new_lines]
    return "\n".join(lines) + "\n"


def deleted_file_diff(path, old_lines):
    lines = [f"diff --git a/{path} b/{path}", "deleted file mode 100644", f"--- a/{path}",
             "+++ /dev/null", f"@@ -1,{len(old_lines)} +0,0 @@"]
    lines += [f"-{line}" for line in old_lines]
    return "\n".join(lines) + "\n"


LENS_DOCS = [("lenses/logic.md", "LENS-LOGIC"), ("prompts/reviewer.md", "PROMPT-REVIEWER")]


def fixed_lens():
    return list(LENS_DOCS)


def fake_gh(tree, calls=None, fail_on=None):
    """A gh runner that serves the contents API from a {folder: {name: text}} dict."""
    def run(args):
        if calls is not None:
            calls.append(list(args))
        assert args[0] == "api" and args[1].startswith(LENS_BASE + "/"), args
        path = args[1][len(LENS_BASE) + 1:]
        if fail_on is not None and fail_on in path:
            raise subprocess.CalledProcessError(1, ["gh", *args])
        if path in tree:
            entries = [{"name": name, "type": "file"} for name in tree[path]]
            entries.append({"name": "sub", "type": "dir"})
            return json.dumps(entries)
        folder, name = path.rsplit("/", 1)
        text = tree[folder][name]
        return json.dumps({"encoding": "base64",
                           "content": base64.b64encode(text.encode("utf-8")).decode("ascii")})
    return run


class BundleTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def build(self, diff, config=None, bodies=(), **kwargs):
        kwargs.setdefault("fetch_lens", fixed_lens)
        kwargs.setdefault("instruction", "BOT-INSTRUCTION")
        return bundle.build_bundle(self.root, diff, config or {}, bodies, **kwargs)


class ContentsTests(BundleTestCase):
    def test_bundle_holds_every_part(self):
        self.write("CLAUDE.md", "ROOT-CLAUDE")
        self.write("AGENTS.md", "ROOT-AGENTS")
        self.write("src/app/AGENTS.md", "APP-AGENTS")
        self.write("src/app/CLAUDE.md", "APP-CLAUDE")
        self.write("src/CLAUDE.md", "PARENT-CLAUDE-NOT-USED")
        self.write("docs/rule.md", "EXTRA-RULE")
        self.write("docs/spec.md", "THE-SPEC")
        self.write("src/app/main.py", "first\nsecond\nthird\n")
        diff = file_diff("src/app/main.py", ["old"], ["second"], start=2)
        bodies = [
            "<!-- lens-review: src/app/main.py:2 Fix the loop #0123abcd [logic] -->\n"
            "**[major] Fix the loop**",
            "<!-- lens-review: README.md:10 Old style heading -->\nbody",
            "<!-- lens-review: src/app/main.py:5 Fix the loop #0123abcd [logic] -->",
        ]
        result = self.build(diff, {"rules": ["docs/rule.md"], "spec": "docs/spec.md"}, bodies)

        self.assertEqual(result.instruction, "BOT-INSTRUCTION")
        for word in ("LENS-LOGIC", "PROMPT-REVIEWER", "ROOT-CLAUDE", "ROOT-AGENTS", "APP-AGENTS",
                     "APP-CLAUDE", "EXTRA-RULE", "THE-SPEC", "+second", "-old"):
            with self.subTest(word=word):
                self.assertIn(word, result.text)
        self.assertNotIn("PARENT-CLAUDE-NOT-USED", result.text)
        self.assertIn("1: first\n2: second\n3: third", result.text)
        self.assertIn("- src/app/main.py: Fix the loop", result.text)
        self.assertIn("- README.md: Old style heading", result.text)
        self.assertEqual(result.text.count("- src/app/main.py: Fix the loop"), 1)
        self.assertEqual((result.trimmed, result.excluded, result.missing, result.over_limit),
                         ([], [], [], False))
        self.assertEqual(result.file_lines["src/app/main.py"],
                         ["first", "second", "third", "old", "second"])
        self.assertLessEqual(result.size, bundle.BUNDLE_CHAR_LIMIT)

    def test_exclude_removes_diff_and_full_text(self):
        self.write("gen/out.js", "GENERATED-CONTENT\n")
        self.write("deep/a/b/lock.json", "LOCK-CONTENT\n")
        self.write("keep.py", "KEPT-CONTENT\n")
        diff = (file_diff("gen/out.js", ["x"], ["GENERATED-CONTENT"])
                + file_diff("deep/a/b/lock.json", ["y"], ["LOCK-CONTENT"])
                + file_diff("keep.py", ["z"], ["KEPT-CONTENT"]))
        result = self.build(diff, {"exclude": ["gen/*", "**/lock.json"]})
        self.assertNotIn("GENERATED-CONTENT", result.text)
        self.assertNotIn("LOCK-CONTENT", result.text)
        self.assertNotIn("gen/out.js", result.text)
        self.assertIn("KEPT-CONTENT", result.text)
        self.assertEqual(result.excluded, ["gen/out.js", "deep/a/b/lock.json"])
        self.assertEqual(list(result.file_lines), ["keep.py"])

    def test_glob_matching(self):
        cases = [
            ("dist/a.js", ["dist/*"], True),
            ("dist/sub/a.js", ["dist/*"], False),
            ("dist/sub/a.js", ["dist/**"], True),
            ("dist/sub/a.js", ["dist/"], True),
            ("a/b/c.min.js", ["**/*.min.js"], True),
            ("c.min.js", ["**/*.min.js"], True),
            ("src/a.py", ["*.py"], False),
            ("a.py", ["?.py"], True),
        ]
        for path, patterns, expected in cases:
            with self.subTest(path=path, patterns=patterns):
                self.assertEqual(bundle.is_excluded(path, patterns), expected)

    def test_new_and_deleted_files(self):
        self.write("added.py", "NEW-1\nNEW-2\n")
        diff = new_file_diff("added.py", ["NEW-1", "NEW-2"]) + deleted_file_diff("gone.py", ["GONE"])
        result = self.build(diff)
        self.assertIn('<file path="added.py">\n1: NEW-1\n2: NEW-2\n</file>', result.text)
        self.assertNotIn('<file path="gone.py">', result.text)
        self.assertIn('<diff path="gone.py">', result.text)
        self.assertEqual(result.file_lines["gone.py"], ["GONE"])

    def test_missing_rules_and_paths_outside_the_checkout(self):
        outside = self.root.parent / f"{self.root.name}-outside.md"
        outside.write_text("OUTSIDE-SECRET", encoding="utf-8")
        self.addCleanup(outside.unlink)
        config = {"rules": ["docs/none.md", f"../{outside.name}"], "spec": "docs/nospec.md"}
        result = self.build("", config)
        self.assertNotIn("OUTSIDE-SECRET", result.text)
        self.assertEqual(result.missing, ["docs/none.md", f"../{outside.name}", "docs/nospec.md"])

    def test_default_instruction_is_the_bot_instruction_file(self):
        result = bundle.build_bundle(self.root, "", {}, fetch_lens=fixed_lens)
        expected = (ROOT / "prompts" / "bot-instruction.md").read_text(encoding="utf-8")
        self.assertEqual(result.instruction, expected)


class LensFetchTests(unittest.TestCase):
    TREE = {
        "lenses": {"logic.md": "LOGIC", "tests.md": "TESTS", "notes.txt": "NOT-MD"},
        "prompts": {"reviewer.md": "REVIEWER", "validator.md": "VALIDATOR", "SKILL.md": "SKILL"},
    }

    def test_fetches_every_md_file_from_the_default_branch(self):
        calls = []
        docs = bundle.fetch_lens_docs(fake_gh(self.TREE, calls))
        self.assertEqual(docs, [("lenses/logic.md", "LOGIC"), ("lenses/tests.md", "TESTS"),
                                ("prompts/reviewer.md", "REVIEWER"),
                                ("prompts/validator.md", "VALIDATOR")])
        for call in calls:
            with self.subTest(call=call):
                self.assertNotIn("ref=", " ".join(call))
                self.assertNotIn("SKILL.md", " ".join(call))

    def test_failures_are_model_stage_errors(self):
        empty = {"lenses": {}, "prompts": {"reviewer.md": "R"}}
        cases = [
            ("listing fails", fake_gh(self.TREE, fail_on="lenses")),
            ("file fails", fake_gh(self.TREE, fail_on="validator.md")),
            ("no md files", fake_gh(empty)),
            ("not json", lambda args: "<html>"),
            ("gh missing", self._raise(FileNotFoundError("gh"))),
        ]
        for name, gh in cases:
            with self.subTest(name=name):
                with self.assertRaises(bundle.LensFetchError) as ctx:
                    bundle.fetch_lens_docs(gh)
                self.assertEqual(ctx.exception.stage, "model")

    def test_build_bundle_propagates_fetch_failure(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            gh = fake_gh(self.TREE, fail_on="lenses")
            with self.assertRaises(bundle.LensFetchError):
                bundle.build_bundle(directory, "", {}, fetch_lens=lambda: bundle.fetch_lens_docs(gh),
                                    instruction="I")

    @staticmethod
    def _raise(exc):
        def run(args):
            raise exc
        return run


class TrimTests(BundleTestCase):
    def setUp(self):
        super().setUp()
        self.files = {"big.py": 3000, "mid.py": 2000, "small.py": 1000}
        diff = ""
        for name, size in self.files.items():
            lines = [f"{name}-line-{i:04d}" for i in range(size // 20)]
            self.write(name, "\n".join(lines) + "\n")
            context = [f" {line}" for line in lines[:10]]
            body = context + [f"-old-{name}", f"+{lines[10]}"] + [f" {line}" for line in lines[11:20]]
            diff += "\n".join([f"diff --git a/{name} b/{name}", f"--- a/{name}", f"+++ b/{name}",
                               "@@ -1,20 +1,20 @@"] + body) + "\n"
        self.diff = diff

    def test_default_limit_is_400k(self):
        self.assertEqual(bundle.BUNDLE_CHAR_LIMIT, 400_000)
        self.assertEqual(bundle.build_bundle.__kwdefaults__["limit"], 400_000)

    def test_drops_full_text_of_the_largest_file_first(self):
        untrimmed = self.build(self.diff, limit=10**9)
        result = self.build(self.diff, limit=untrimmed.size - 1)
        self.assertEqual([(t.path, t.stage) for t in result.trimmed], [("big.py", "full_text")])
        self.assertNotIn('<file path="big.py">', result.text)
        self.assertIn('<file path="mid.py">', result.text)
        self.assertLessEqual(result.size, untrimmed.size - 1)
        self.assertFalse(result.over_limit)
        self.assertNotIn("big.py-line-0100", result.file_lines["big.py"])
        self.assertIn("big.py-line-0010", result.file_lines["big.py"])

    def test_stages_run_in_order_until_it_fits(self):
        result = self.build(self.diff, limit=0)
        stages = [t.stage for t in result.trimmed]
        self.assertEqual(stages, ["full_text"] * 3 + ["diff_context"] * 3 + ["diff_name_only"] * 3)
        self.assertEqual([t.path for t in result.trimmed[:3]], ["big.py", "mid.py", "small.py"])
        self.assertTrue(result.over_limit)
        for name in self.files:
            with self.subTest(name=name):
                self.assertIn(f'<diff path="{name}" omitted="true"></diff>', result.text)
                self.assertEqual(result.file_lines[name], [])

    def test_stops_after_shrinking_the_diff_when_that_fits(self):
        # The size with every full text left out: build once without the files on disk.
        with tempfile.TemporaryDirectory(dir=ROOT) as empty:
            no_full = bundle.build_bundle(empty, self.diff, {}, fetch_lens=fixed_lens,
                                          instruction="BOT-INSTRUCTION", limit=10**9)
        result = self.build(self.diff, limit=no_full.size - 1)
        self.assertEqual([t.stage for t in result.trimmed],
                         ["full_text"] * 3 + ["diff_context"])
        self.assertFalse(result.over_limit)
        self.assertIn("@@ -10,3 +10,3 @@", result.text)
        self.assertEqual(result.trimmed[-1].label, "diff を前後数行に縮めた")

    def test_labels(self):
        self.assertEqual(bundle.Trim("a", "full_text").label, "全文を外した")
        self.assertEqual(bundle.Trim("a", "diff_name_only").label, "diff をファイル名だけにした")


class DiffTests(unittest.TestCase):
    def test_shrink_hunk_recomputes_headers(self):
        hunk = ["@@ -1,12 +1,12 @@ def f():",
                " c1", " c2", " c3", "-old4", "+new4", " c5", " c6",
                " c7", " c8", " c9", "-old10", "+new10", " c11", " c12"]
        self.assertEqual(bundle.shrink_hunk(hunk, 1), [
            ["@@ -3,3 +3,3 @@", " c3", "-old4", "+new4", " c5"],
            ["@@ -9,3 +9,3 @@", " c9", "-old10", "+new10", " c11"],
        ])

    def test_shrink_hunk_with_added_lines_and_no_newline_marker(self):
        hunk = ["@@ -5,3 +5,5 @@", " a", " b", "+x", "+y", " c", "\\ No newline at end of file"]
        self.assertEqual(bundle.shrink_hunk(hunk, 1), [
            ["@@ -6,2 +6,4 @@", " b", "+x", "+y", " c", "\\ No newline at end of file"],
        ])

    def test_shrink_hunk_of_a_large_new_file_is_linear(self):
        size = 200_000
        hunk = [f"@@ -0,0 +1,{size} @@"] + [f"+line{i}" for i in range(size)]
        started = time.monotonic()
        shrunk = bundle.shrink_hunk(hunk, 3)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(shrunk, [hunk])

    def test_parse_diff_paths(self):
        diff = ("diff --git a/old.py b/new.py\nsimilarity index 90%\nrename from old.py\n"
                "rename to new.py\n--- a/old.py\n+++ b/new.py\n@@ -1 +1 @@\n-a\n+b\n"
                "diff --git a/img.png b/img.png\nBinary files a/img.png and b/img.png differ\n"
                'diff --git "a/sp ace.py" "b/sp ace.py"\n--- "a/sp ace.py"\n+++ "b/sp ace.py"\n'
                "@@ -1 +1 @@\n-a\n+b\n")
        files = bundle.parse_diff(diff)
        self.assertEqual([f.path for f in files], ["new.py", "img.png", "sp ace.py"])
        self.assertEqual([f.deleted for f in files], [False, False, False])

    def test_existing_findings_from_markers(self):
        bodies = ["<!-- lens-review: a/b.py:12 Heading with spaces #deadbeef [logic] -->",
                  "<!-- lens-review: c.py:3 Old form heading -->", "no marker", None]
        self.assertEqual(bundle.existing_findings(bodies),
                         [("a/b.py", "Heading with spaces"), ("c.py", "Old form heading")])


if __name__ == "__main__":
    unittest.main()
