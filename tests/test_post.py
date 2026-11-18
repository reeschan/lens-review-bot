import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import post as p  # noqa: E402
from bundle import Bundle, Trim, STAGE_DIFF_CONTEXT, STAGE_FULL_TEXT  # noqa: E402
from lens_review_config import failure_body  # noqa: E402
from triage import Finding, TriageResult, quote_hash, read_markers  # noqa: E402

REPO = "owner/app"
PR = 12
HEAD = "abcdef1234567890abcdef1234567890abcdef12"
BOT = "github-actions[bot]"


def finding(**over):
    base = {
        "file": "src/app.py",
        "line": 11,
        "severity": "major",
        "lens": "correctness",
        "heading": "b の値が違う",
        "body": "b は 2 のはず。",
        "quote": "b = 3",
        "rule_violation": False,
        "rule_source": "",
    }
    base.update(over)
    return Finding(**base)


def bundle(**over):
    base = {"instruction": "", "text": ""}
    base.update(over)
    return Bundle(**base)


class FakeGh:
    """Records gh calls and answers them from a table of (method, path) -> output."""

    def __init__(self, comments=(), fail_on=None, created_id=900):
        self.calls = []
        self.comments = list(comments)
        self.fail_on = fail_on
        self.created_id = created_id

    def __call__(self, args, input_text=None):
        method = args[args.index("--method") + 1] if "--method" in args else "GET"
        path = args[1]
        self.calls.append({"method": method, "path": path, "args": args, "input": input_text})
        if self.fail_on == (method, path):
            raise subprocess.CalledProcessError(1, ["gh", *args], output="", stderr="HTTP 422")
        if method == "GET":
            half = len(self.comments) // 2
            return json.dumps(self.comments[:half]) + "\n" + json.dumps(self.comments[half:])
        if method == "POST" and path.endswith("/comments"):
            return json.dumps({"id": self.created_id})
        return "{}"

    def payloads(self, method, suffix):
        return [
            json.loads(c["input"])
            for c in self.calls
            if c["method"] == method and c["path"].endswith(suffix)
        ]


def comment(cid, login, body):
    return {"id": cid, "user": {"login": login}, "body": body}


class InlineBodyTests(unittest.TestCase):
    def test_order_is_marker_title_body_quote(self):
        body = p.inline_body(finding())
        h = quote_hash("b = 3")
        expected = (
            f"<!-- lens-review: src/app.py:11 b の値が違う #{h} [correctness] -->\n\n"
            "**[major] b の値が違う**\n\n"
            "b は 2 のはず。\n\n"
            "根拠:\n```\nb = 3\n```"
        )
        self.assertEqual(body, expected)

    def test_marker_is_read_back_by_triage(self):
        f = finding(heading="見出し  の 空白", quote="x  =\n  1")
        markers = read_markers([p.inline_body(f)])
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0].file, "src/app.py")
        self.assertEqual(markers[0].heading, f.heading_key)
        self.assertEqual(markers[0].hash, f.hash)
        self.assertEqual(markers[0].lens, f.lens_key)

    def test_marker_with_space_in_path_is_read_back(self):
        f = finding(file="docs/my notes/a b.py")
        markers = read_markers([p.inline_body(f)])
        self.assertEqual([(m.file, m.heading, m.hash) for m in markers],
                         [("docs/my notes/a b.py", f.heading_key, f.hash)])

    def test_quote_with_backticks_gets_a_longer_fence(self):
        body = p.inline_body(finding(quote="s = ```x```"))
        self.assertTrue(body.endswith("根拠:\n````\ns = ```x```\n````"))

    def test_rule_source_follows_the_body_for_rule_violations(self):
        body = p.inline_body(finding(rule_violation=True, rule_source="CLAUDE.md"))
        self.assertLess(body.index("b は 2 のはず。"), body.index("規約: `CLAUDE.md`"))
        self.assertLess(body.index("規約: `CLAUDE.md`"), body.index("根拠:"))
        self.assertNotIn("規約:", p.inline_body(finding(rule_source="CLAUDE.md")))


class ReviewPayloadTests(unittest.TestCase):
    def test_one_comment_review(self):
        f1, f2 = finding(), finding(file="b.py", line=3, severity="critical")
        payload = p.review_payload(HEAD, [f1, f2])
        self.assertEqual(payload["commit_id"], HEAD)
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload["body"], "lens-review: 新しい指摘 2 件")
        self.assertEqual(
            payload["comments"],
            [
                {"path": "src/app.py", "line": 11, "side": "RIGHT", "body": p.inline_body(f1)},
                {"path": "b.py", "line": 3, "side": "RIGHT", "body": p.inline_body(f2)},
            ],
        )

    def test_nothing_to_post(self):
        self.assertIsNone(p.review_payload(HEAD, []))


class SummaryBodyTests(unittest.TestCase):
    def full_result(self):
        return TriageResult(
            inline=[finding()],
            previous=[finding(file="old.py", line=4, severity="critical", heading="前の指摘")],
            outside_diff=[finding(file="dir/a b.py", line=1, heading="外の指摘")],
            below_min=[finding(severity="minor"), finding(severity="minor")],
            repeated=[finding()],
            malformed_count=1,
            quote_dropped_count=2,
        )

    def test_contents(self):
        b = bundle(
            trimmed=[Trim("big.py", STAGE_FULL_TEXT), Trim("src/app.py", STAGE_DIFF_CONTEXT)],
            excluded=["gen/x.py"],
            missing=["docs/rules.md"],
        )
        body = p.summary_body(REPO, HEAD, self.full_result(), b)
        lines = body.splitlines()
        self.assertEqual(lines[0], "## lens-review")
        self.assertEqual(lines[1], f"対象の head: {HEAD}")
        self.assertEqual(lines[2], "指摘: critical 1 / major 2 / minor 0")
        expected_sections = [
            "### 今回の指摘",
            "- [major] src/app.py:11 — b の値が違う",
            "### 前に出した指摘",
            "- [critical] old.py:4 — 前の指摘",
            "### diff の外の指摘",
            f"- [major] [dir/a b.py:1](https://github.com/{REPO}/blob/{HEAD}/dir/a%20b.py#L1-L2)"
            " — 外の指摘",
            "### 出さなかった・捨てた指摘",
            "- 下限より軽くて出さなかった指摘: 2 件（critical 0 / major 0 / minor 2）",
            "- 捨てた指摘: 3 件（形が違う 1 件・根拠の引用が束ねに無い 2 件）",
            "- 同じ回で重なった指摘: 1 件",
            "### 削った中身",
            "- big.py: 全文を外した",
            "- src/app.py: diff を前後数行に縮めた",
            "- docs/rules.md: 読めなかった（規約・仕様）",
            "- gen/x.py: 設定の exclude で外した",
        ]
        positions = [lines.index(line) for line in expected_sections]
        self.assertEqual(positions, sorted(positions))

    def test_outside_diff_finding_keeps_body_rule_and_quote(self):
        outside = finding(file="a.py", line=3, heading="外の指摘", body="理由と直し方。",
                          rule_violation=True, rule_source="CLAUDE.md", quote="x = ```1```")
        body = p.summary_body(REPO, HEAD, TriageResult(outside_diff=[outside]), bundle())
        section = body.split("### diff の外の指摘\n")[1].split("\n### ")[0]
        self.assertIn("— 外の指摘", section)
        self.assertIn("<details>", section)
        for part in ("理由と直し方。", "規約: `CLAUDE.md`", "````\n  x = ```1```\n  ````"):
            with self.subTest(part=part):
                self.assertIn(part, section)
        self.assertNotIn("<!-- lens-review:", body)

    def test_link_includes_one_line_before_and_after(self):
        self.assertEqual(
            p.file_link(REPO, HEAD, "a.py", 5),
            f"https://github.com/{REPO}/blob/{HEAD}/a.py#L4-L6",
        )

    def test_empty_run(self):
        body = p.summary_body(REPO, HEAD, TriageResult(), bundle())
        self.assertIn("### 今回の指摘\nなし", body)
        self.assertIn("### 前に出した指摘\nなし", body)
        self.assertIn("### diff の外の指摘\nなし", body)
        self.assertIn("### 削った中身\nなし", body)
        self.assertIn("下限より軽くて出さなかった指摘: 0 件", body)

    def test_over_limit_is_written(self):
        body = p.summary_body(REPO, HEAD, TriageResult(), bundle(over_limit=True))
        self.assertIn("- 削っても束ねが上限に収まらなかった", body)


class PostTests(unittest.TestCase):
    def test_posts_review_then_creates_summary(self):
        gh = FakeGh(comments=[comment(1, "alice", "## lens-review\n人が書いた")])
        result = TriageResult(inline=[finding()])
        outcome = p.post(REPO, PR, HEAD, result, bundle(), gh=gh, bot_login=BOT)

        self.assertEqual(
            [(c["method"], c["path"]) for c in gh.calls],
            [
                ("POST", f"repos/{REPO}/pulls/{PR}/reviews"),
                ("GET", f"repos/{REPO}/issues/{PR}/comments"),
                ("POST", f"repos/{REPO}/issues/{PR}/comments"),
            ],
        )
        self.assertIn("--paginate", gh.calls[1]["args"])
        self.assertEqual(gh.payloads("POST", "/reviews"), [p.review_payload(HEAD, [finding()])])
        self.assertEqual(
            gh.payloads("POST", "/comments"),
            [{"body": p.summary_body(REPO, HEAD, result, bundle())}],
        )
        self.assertEqual(outcome, p.PostOutcome(inline_count=1, summary_id=900, summary_created=True))

    def test_no_review_when_nothing_to_post(self):
        gh = FakeGh()
        p.post(REPO, PR, HEAD, TriageResult(previous=[finding()]), bundle(), gh=gh, bot_login=BOT)
        self.assertFalse(any(c["path"].endswith("/reviews") for c in gh.calls))
        self.assertEqual(len(gh.payloads("POST", "/comments")), 1)

    def test_rewrites_the_first_bot_summary_across_pages(self):
        gh = FakeGh(
            comments=[
                comment(1, BOT, "## lens-review の話ではない\n"),
                comment(2, "alice", "## lens-review\n人"),
                comment(3, BOT, "## lens-review\n前の回"),
                comment(4, BOT, "## lens-review\nもう 1 つ"),
            ]
        )
        outcome = p.post(REPO, PR, HEAD, TriageResult(), bundle(), gh=gh, bot_login=BOT)
        patch = [c for c in gh.calls if c["method"] == "PATCH"]
        self.assertEqual([c["path"] for c in patch], [f"repos/{REPO}/issues/comments/3"])
        self.assertEqual(json.loads(patch[0]["input"])["body"].splitlines()[0], "## lens-review")
        self.assertEqual(gh.payloads("POST", "/comments"), [])
        self.assertEqual(outcome.summary_id, 3)
        self.assertFalse(outcome.summary_created)

    def test_failure_summary_is_rewritten(self):
        failed = failure_body("model", "https://example.com/run/1", HEAD)
        gh = FakeGh(comments=[comment(7, BOT, failed)])
        p.post(REPO, PR, HEAD, TriageResult(), bundle(), gh=gh, bot_login=BOT)
        bodies = gh.payloads("PATCH", "/issues/comments/7")
        self.assertEqual(len(bodies), 1)
        self.assertNotIn("失敗した段", bodies[0]["body"])

    def test_summary_shares_the_failure_body_title_and_head_label(self):
        failed = failure_body("post", "https://example.com/run/1", HEAD).splitlines()
        ok = p.summary_body(REPO, HEAD, TriageResult(), bundle()).splitlines()
        self.assertEqual(failed[0], ok[0])
        self.assertTrue(any(line.startswith("対象の head: ") for line in failed))
        self.assertTrue(ok[1].startswith("対象の head: "))


class FailureTests(unittest.TestCase):
    def assert_post_stage(self, ctx):
        self.assertIsInstance(ctx.exception, p.PostError)
        self.assertEqual(ctx.exception.stage, "post")

    def test_review_failure_stops_before_the_summary(self):
        gh = FakeGh(fail_on=("POST", f"repos/{REPO}/pulls/{PR}/reviews"))
        with self.assertRaises(p.PostError) as ctx:
            p.post(REPO, PR, HEAD, TriageResult(inline=[finding()]), bundle(), gh=gh, bot_login=BOT)
        self.assert_post_stage(ctx)
        self.assertIn("HTTP 422", str(ctx.exception))
        self.assertEqual(len(gh.calls), 1)

    def test_summary_failures(self):
        cases = [
            ("GET", f"repos/{REPO}/issues/{PR}/comments", []),
            ("POST", f"repos/{REPO}/issues/{PR}/comments", []),
            ("PATCH", f"repos/{REPO}/issues/comments/5", [comment(5, BOT, "## lens-review")]),
        ]
        for method, path, comments in cases:
            with self.subTest(method=method):
                gh = FakeGh(comments=comments, fail_on=(method, path))
                with self.assertRaises(p.PostError) as ctx:
                    p.post(REPO, PR, HEAD, TriageResult(), bundle(), gh=gh, bot_login=BOT)
                self.assert_post_stage(ctx)

    def test_gh_missing_is_a_post_error(self):
        def gh(args, input_text=None):
            raise FileNotFoundError("gh")

        with self.assertRaises(p.PostError):
            p.post(REPO, PR, HEAD, TriageResult(), bundle(), gh=gh, bot_login=BOT)

    def test_unreadable_answers(self):
        def broken(args, input_text=None):
            return "not json"

        with self.assertRaises(p.PostError):
            p.post(REPO, PR, HEAD, TriageResult(), bundle(), gh=broken, bot_login=BOT)

        def no_id(args, input_text=None):
            return "[]" if "--method" not in args else "{}"

        with self.assertRaises(p.PostError):
            p.post(REPO, PR, HEAD, TriageResult(), bundle(), gh=no_id, bot_login=BOT)

    def test_head_sha_must_be_40_hex(self):
        for sha in ("abcdef123456", HEAD.upper(), ""):
            with self.subTest(sha=sha), self.assertRaises(p.PostError):
                p.post(REPO, PR, sha, TriageResult(), bundle(), gh=FakeGh(), bot_login=BOT)

    def test_repo_name_is_checked(self):
        with self.assertRaises(p.PostError):
            p.post("owner/app/../x", PR, HEAD, TriageResult(), bundle(), gh=FakeGh(), bot_login=BOT)


if __name__ == "__main__":
    unittest.main()
