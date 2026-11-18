import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import run_review as rr  # noqa: E402
from model_call import ModelError, ModelResult  # noqa: E402

REPO = "owner/app"
PR = 12
HEAD = "abcdef1234567890abcdef1234567890abcdef12"
OTHER_HEAD = "1111111111111111111111111111111111111111"
BOT = "github-actions[bot]"
RUN_URL = "https://example.com/run/1"

DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 a = 1
-b = 2
+b = 3
+c = 4
"""

FINDING = {
    "file": "src/app.py",
    "line": 2,
    "severity": "major",
    "lens": "correctness",
    "heading": "b の値が違う",
    "body": "b は 2 のはず。",
    "quote": "b = 3",
    "rule_violation": False,
    "rule_source": "",
}

CONFIG = {
    "model": "m",
    "max_output_tokens": 100,
    "min_severity": "minor",
    "post_rule_violations": True,
    "rules": [],
    "exclude": [],
}


class FakeGh:
    def __init__(self, head=HEAD, fail_on=None, inline_comments=None):
        self.head = head
        self.fail_on = fail_on
        self.inline_comments = inline_comments or []
        self.calls = []
        self.posted_summaries = []
        self.reviews = []

    def __call__(self, args, input_text=None):
        self.calls.append((list(args), input_text))
        path = args[1] if len(args) > 1 else ""
        method = args[args.index("--method") + 1] if "--method" in args else "GET"
        if self.fail_on and self.fail_on in (path, method):
            raise subprocess.CalledProcessError(1, "gh", stderr="boom")
        if path == f"repos/{REPO}/pulls/{PR}" and "--jq" in args:
            return self.head + "\n"
        if path == f"repos/{REPO}/pulls/{PR}":
            return DIFF
        if path == f"repos/{REPO}/pulls/{PR}/comments":
            return json.dumps(self.inline_comments)
        if path == f"repos/{REPO}/issues/{PR}/comments" and method == "GET":
            return "[]"
        if path == f"repos/{REPO}/issues/{PR}/comments" and method == "POST":
            self.posted_summaries.append(json.loads(input_text)["body"])
            return json.dumps({"id": 99})
        if path == f"repos/{REPO}/pulls/{PR}/reviews":
            self.reviews.append(json.loads(input_text))
            return "{}"
        raise AssertionError(f"unexpected gh call: {args}")


def fake_model(findings=None, error=None):
    calls = []

    def model(bundle, config, api_key):
        calls.append((bundle, config, api_key))
        if error is not None:
            raise error
        return ModelResult(
            findings=findings if findings is not None else [], input_tokens=7001, output_tokens=7002
        )

    model.calls = calls
    return model


def fetch_lens():
    return [("lenses/correctness.md", "lens"), ("prompts/verify.md", "prompt")]


class RunReviewTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        os.makedirs(os.path.join(self.root, "src"))
        with open(os.path.join(self.root, "src", "app.py"), "w", encoding="utf-8") as fp:
            fp.write("a = 1\nb = 3\nc = 4\n")
        self.summary_file = os.path.join(self.root, "step-summary.md")

    def run_review(self, gh, model, **over):
        kwargs = dict(
            repo=REPO, pr_number=PR, head_sha=HEAD, config=CONFIG, root=self.root,
            run_url=RUN_URL, api_key="key", gh=gh, fetch_lens=fetch_lens, model=model,
            summary_path=self.summary_file,
        )
        kwargs.update(over)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            outcome = rr.run(**kwargs)
        return outcome, out.getvalue(), err.getvalue()

    def test_success_posts_review_and_summary_and_reports_tokens(self):
        gh = FakeGh()
        model = fake_model([FINDING])
        outcome, out, _ = self.run_review(gh, model)
        self.assertEqual(outcome.kind, "posted")
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0][2], "key")
        self.assertEqual(len(gh.reviews), 1)
        self.assertEqual(gh.reviews[0]["comments"][0]["line"], 2)
        self.assertEqual(len(gh.posted_summaries), 1)
        self.assertTrue(gh.posted_summaries[0].startswith("## lens-review"))
        self.assertIn("7001", out)
        self.assertIn("7002", out)
        with open(self.summary_file, encoding="utf-8") as fp:
            self.assertIn("7001", fp.read())
        for body in gh.posted_summaries + [json.dumps(r) for r in gh.reviews]:
            self.assertNotIn("7001", body)

    def test_head_is_checked_after_the_diff_and_before_the_review(self):
        gh = FakeGh()
        self.run_review(gh, fake_model([FINDING]))
        paths = [(c[0][1], "--jq" in c[0]) for c in gh.calls]
        diff_idx = paths.index((f"repos/{REPO}/pulls/{PR}", False))
        head_idx = paths.index((f"repos/{REPO}/pulls/{PR}", True))
        review_idx = paths.index((f"repos/{REPO}/pulls/{PR}/reviews", False))
        self.assertLess(diff_idx, head_idx)
        self.assertLess(head_idx, review_idx)

    def test_only_bot_inline_comments_reach_the_bundle(self):
        marker = "<!-- lens-review: src/app.py:2 前の指摘 #aaaaaaaa -->"
        other = "<!-- lens-review: src/app.py:1 他人の指摘 #bbbbbbbb -->"
        gh = FakeGh(inline_comments=[
            {"user": {"login": BOT}, "body": marker + "\nx"},
            {"user": {"login": "someone"}, "body": other + "\nx"},
        ])
        model = fake_model([])
        self.run_review(gh, model)
        text = model.calls[0][0].text
        self.assertIn("前の指摘", text)
        self.assertNotIn("他人の指摘", text)

    def test_model_failure_writes_stage_model_and_posts_no_findings(self):
        gh = FakeGh()
        model = fake_model(error=ModelError("HTTP 500"))
        outcome, _, _ = self.run_review(gh, model)
        self.assertEqual((outcome.kind, outcome.stage), ("failed", "model"))
        self.assertEqual(outcome.exit_code, 1)
        self.assertEqual(gh.reviews, [])
        self.assertEqual(len(gh.posted_summaries), 1)
        self.assertIn("失敗した段: model", gh.posted_summaries[0])
        self.assertIn(RUN_URL, gh.posted_summaries[0])

    def test_lens_fetch_failure_is_stage_model_without_calling_model(self):
        from bundle import LensFetchError

        def broken():
            raise LensFetchError("取得できません")

        gh = FakeGh()
        model = fake_model([FINDING])
        outcome, _, _ = self.run_review(gh, model, fetch_lens=broken)
        self.assertEqual(outcome.stage, "model")
        self.assertEqual(model.calls, [])
        self.assertEqual(gh.reviews, [])
        self.assertIn("失敗した段: model", gh.posted_summaries[0])

    def test_over_limit_bundle_fails_at_model_without_calling_it(self):
        real = rr.bundle_mod.build_bundle

        def tiny_limit(root, diff_text, config, bodies=(), **kw):
            return real(root, diff_text, config, bodies, limit=10, **kw)

        gh = FakeGh()
        model = fake_model([FINDING])
        rr.bundle_mod.build_bundle = tiny_limit
        try:
            outcome, _, _ = self.run_review(gh, model)
        finally:
            rr.bundle_mod.build_bundle = real
        self.assertEqual((outcome.kind, outcome.stage), ("failed", "model"))
        self.assertEqual(model.calls, [])
        self.assertEqual(gh.reviews, [])
        self.assertIn("失敗した段: model", gh.posted_summaries[0])
        self.assertIn("上限", gh.posted_summaries[0])

    def test_missing_api_key_fails_at_model(self):
        gh = FakeGh()
        model = fake_model([FINDING])
        outcome, _, _ = self.run_review(gh, model, api_key=None)
        self.assertEqual(outcome.stage, "model")
        self.assertEqual(model.calls, [])
        self.assertEqual(gh.reviews, [])

    def test_head_change_posts_nothing_and_writes_no_failure(self):
        gh = FakeGh(head=OTHER_HEAD)
        model = fake_model([FINDING])
        outcome, _, _ = self.run_review(gh, model)
        self.assertEqual(outcome.kind, "cancelled")
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(gh.reviews, [])
        self.assertEqual(gh.posted_summaries, [])
        self.assertFalse(any(c[1] is not None for c in gh.calls))

    def test_post_failure_writes_stage_post(self):
        gh = FakeGh(fail_on=f"repos/{REPO}/pulls/{PR}/reviews")
        model = fake_model([FINDING])
        outcome, _, _ = self.run_review(gh, model)
        self.assertEqual((outcome.kind, outcome.stage), ("failed", "post"))
        self.assertEqual(outcome.exit_code, 1)
        self.assertEqual(len(gh.posted_summaries), 1)
        self.assertIn("失敗した段: post", gh.posted_summaries[0])

    def test_failure_summary_write_failure_does_not_raise(self):
        class Gh(FakeGh):
            def __call__(self, args, input_text=None):
                if args[1].endswith("/issues/12/comments"):
                    raise subprocess.CalledProcessError(1, "gh", stderr="boom")
                return super().__call__(args, input_text)

        outcome, _, err = self.run_review(Gh(), fake_model(error=ModelError("x")))
        self.assertEqual(outcome.stage, "model")
        self.assertIn("失敗のまとめを書けませんでした", err)

    def test_no_summary_path_still_prints_tokens(self):
        _, out, _ = self.run_review(FakeGh(), fake_model([]), summary_path=None)
        self.assertIn("7001", out)

    def test_main_rejects_bad_config_json(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = rr.main(
                ["--repo", REPO, "--pr-number", "1", "--head-sha", HEAD, "--config", "nope"]
            )
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
