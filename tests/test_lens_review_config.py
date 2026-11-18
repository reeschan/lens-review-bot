import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lens_review_config as config


def pull_request(draft=False, state="open", head_repo="owner/repo", user_type="User"):
    return {
        "number": 12,
        "draft": draft,
        "state": state,
        "user": {"type": user_type},
        "head": {"sha": "abcdef123456", "repo": None if head_repo is None else {"full_name": head_repo}},
        "base": {"sha": "123456789abc", "repo": {"full_name": "owner/repo"}},
    }


def pr_event(action="opened", **kwargs):
    return {"action": action, "number": 12, "pull_request": pull_request(**kwargs)}


def comment_event(body="/lens-review", action="created", is_pr=True):
    issue = {"number": 12}
    if is_pr:
        issue["pull_request"] = {}
    return {"action": action, "issue": issue, "comment": {"body": body}}


class DecideTests(unittest.TestCase):
    def decide_pr(self, text=None, **kwargs):
        return config.decide("pull_request", pr_event(**kwargs), None, None, text)

    def decide_comment(self, event=None, pr=None, permission="write", text=None):
        return config.decide("issue_comment", event or comment_event(),
                             pull_request() if pr is None else pr, permission, text)

    def assert_skip(self, result, word):
        self.assertEqual(result["decision"], "skip")
        self.assertIn(word, result["reason"])

    def assert_config_error(self, text):
        result = self.decide_pr(text)
        self.assertEqual(result["decision"], "config-error")
        self.assertTrue(result["reason"])
        self.assertTrue(result["summary_body"].startswith("## lens-review"))
        self.assertIn(result["reason"], result["summary_body"])
        self.assertIn("1234567", result["summary_body"])

    def test_defaults(self):
        result = self.decide_pr()
        self.assertEqual(result["decision"], "run")
        self.assertEqual((result["pr_number"], result["head_sha"], result["base_sha"]),
                         (12, "abcdef123456", "123456789abc"))
        self.assertNotIn("claude_args", result)
        self.assertNotIn("prompt", result)
        self.assertEqual(json.loads(result["config"]), {
            "model": "claude-sonnet-5-5", "max_output_tokens": 16000, "min_severity": "major",
            "post_rule_violations": True, "rules": [], "exclude": [],
        })

    def test_draft_pull_request_skips(self):
        self.assert_skip(self.decide_pr(draft=True), "draft")

    def test_draft_comment_runs(self):
        self.assertEqual(self.decide_comment(pr=pull_request(draft=True))["decision"], "run")

    def test_pull_request_fork_skips(self):
        self.assert_skip(self.decide_pr(head_repo="other/repo"), "fork")

    def test_comment_fork_skips(self):
        self.assert_skip(self.decide_comment(pr=pull_request(head_repo="other/repo")), "fork")

    def test_null_head_repo_skips(self):
        self.assert_skip(self.decide_pr(head_repo=None), "fork")

    def test_read_permission_skips(self):
        self.assert_skip(self.decide_comment(permission="read"), "権限")

    def test_triage_permission_skips(self):
        self.assert_skip(self.decide_comment(permission="triage"), "権限")

    def test_none_permission_skips(self):
        self.assert_skip(self.decide_comment(permission="none"), "権限")

    def test_write_permission_runs(self):
        self.assertEqual(self.decide_comment(permission="write")["decision"], "run")

    def test_maintain_permission_runs(self):
        self.assertEqual(self.decide_comment(permission="maintain")["decision"], "run")

    def test_admin_permission_runs(self):
        self.assertEqual(self.decide_comment(permission="admin")["decision"], "run")

    def test_unrelated_comment_skips(self):
        self.assert_skip(self.decide_comment(event=comment_event("hello")), "コメント")

    def test_command_prefix_is_not_command(self):
        self.assert_skip(self.decide_comment(event=comment_event("/lens-reviewer")), "コメント")

    def test_issue_comment_skips(self):
        self.assert_skip(self.decide_comment(event=comment_event(is_pr=False)), "コメント")

    def test_leading_whitespace_command_runs(self):
        self.assertEqual(self.decide_comment(event=comment_event("  /lens-review"))["decision"], "run")

    def test_closed_action_skips(self):
        self.assert_skip(self.decide_pr(action="closed"), "イベント")

    def test_non_created_comment_skips(self):
        self.assert_skip(self.decide_comment(event=comment_event(action="edited")), "イベント")

    def test_closed_pr_comment_skips(self):
        self.assert_skip(self.decide_comment(pr=pull_request(state="closed")), "閉じ")

    def test_bot_author_skips(self):
        self.assert_skip(self.decide_pr(user_type="Bot"), "bot")

    def test_comment_missing_pr_raises(self):
        with self.assertRaises(ValueError):
            config.decide("issue_comment", comment_event(), None, "write", None)

    def test_comment_missing_permission_raises(self):
        with self.assertRaises(ValueError):
            config.decide("issue_comment", comment_event(), pull_request(), None, None)

    def test_broken_yaml(self):
        self.assert_config_error("rules: [a.md\n")

    def test_unknown_key(self):
        self.assert_config_error("unknown: value\n")

    def test_removed_keys_are_unknown(self):
        for text in ("max_turns: 80\n", "allowed_tools: []\n"):
            with self.subTest(text=text):
                result = self.decide_pr(text)
                self.assertEqual(result["decision"], "config-error")
                self.assertIn("不明な設定項目です", result["reason"])

    def test_quoted_integer(self):
        self.assert_config_error('max_output_tokens: "16000"\n')

    def test_boolean_integer(self):
        self.assert_config_error("max_output_tokens: true\n")

    def test_max_output_tokens_out_of_range(self):
        for value in ("0", "-1", "64001"):
            with self.subTest(value=value):
                self.assert_config_error(f"max_output_tokens: {value}\n")

    def test_max_output_tokens_bounds_run(self):
        for value in (1, 64000):
            with self.subTest(value=value):
                result = self.decide_pr(f"max_output_tokens: {value}\n")
                self.assertEqual(json.loads(result["config"])["max_output_tokens"], value)

    def test_scalar_rules(self):
        self.assert_config_error("rules: a.md\n")

    def test_unknown_severity(self):
        self.assert_config_error("min_severity: info\n")

    def test_parent_path(self):
        self.assert_config_error("rules: [../rules.md]\n")

    def test_unsafe_model(self):
        self.assert_config_error('model: "a;b"\n')

    def test_null_scalars_are_config_errors(self):
        for value in ("null", "Null", "NULL", "~"):
            with self.subTest(value=value):
                self.assert_config_error(f"spec: {value}\n")

    def test_trailing_tabs_and_tab_only_line(self):
        result = self.decide_pr("\t\nmin_severity: major\t # comment\n")
        self.assertEqual(result["decision"], "run")
        self.assertEqual(json.loads(result["config"])["min_severity"], "major")

    def merged(self, text):
        return json.loads(self.decide_pr(text)["config"])

    def test_disable_rule_violations(self):
        self.assertIs(self.merged("post_rule_violations: false\n")["post_rule_violations"], False)

    def test_rules_spec_and_exclude(self):
        cfg = self.merged("rules: [a.md, b.md]\nspec: spec.md\nexclude: [generated/**, '*.lock']\n")
        self.assertEqual(cfg["rules"], ["a.md", "b.md"])
        self.assertEqual(cfg["spec"], "spec.md")
        self.assertEqual(cfg["exclude"], ["generated/**", "*.lock"])

    def test_model_override(self):
        self.assertEqual(self.merged("model: other-model\n")["model"], "other-model")

    def test_block_and_flow_lists_match(self):
        flow = self.decide_pr("rules: [a.md, b.md]\n")
        block = self.decide_pr("rules:\n  - a.md\n  - b.md\n")
        self.assertEqual(flow["config"], block["config"])

    def test_bom_and_crlf(self):
        result = self.decide_pr("\ufeffmax_output_tokens: 42\r\nmin_severity: minor\r\n")
        self.assertEqual(result["decision"], "run")
        cfg = json.loads(result["config"])
        self.assertEqual((cfg["max_output_tokens"], cfg["min_severity"]), (42, "minor"))

    def test_empty_file_uses_defaults(self):
        self.assertEqual(self.decide_pr("")["config"], self.decide_pr()["config"])

    def test_hash_inside_quoted_value_is_preserved(self):
        self.assertEqual(self.merged("exclude: ['foo # bar'] # ignored\n")["exclude"], ["foo # bar"])

    def test_skip_precedes_bad_config(self):
        self.assert_skip(self.decide_pr("unknown: value\n", draft=True), "draft")


class FailureBodyTests(unittest.TestCase):
    def test_failure_body_has_stage_url_and_full_sha(self):
        sha = "abcdef1234567890abcdef1234567890abcdef12"
        body = config.failure_body("model", "https://example.com/runs/123", sha)
        self.assertTrue(body.startswith("## lens-review"))
        self.assertIn("model", body)
        self.assertIn("https://example.com/runs/123", body)
        self.assertIn(sha, body)

    def test_failure_body_without_sha(self):
        body = config.failure_body("checkout", "https://example.com/runs/123", None)
        self.assertNotIn("head", body)


class CliTests(unittest.TestCase):
    def test_decide_and_failure_body(self):
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "lens_review_config.py"
        with tempfile.TemporaryDirectory(dir=root) as directory:
            folder = Path(directory)
            event = folder / "event.json"
            event.write_text(json.dumps(pr_event()), encoding="utf-8")
            cfg = folder / "config.yml"
            output = folder / "output"
            command = [sys.executable, str(script), "decide", "--event-name", "pull_request",
                       "--event", str(event), "--config", str(cfg), "--github-output", str(output)]
            env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=True, env=env)
            self.assertEqual(json.loads(result.stdout)["decision"], "run")
            cfg.write_bytes(b"\xff")
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=True, env=env)
            self.assertEqual(json.loads(result.stdout)["decision"], "config-error")
            content = output.read_text(encoding="utf-8")
            self.assertIn("summary_body<<", content)
            self.assertIn("UTF-8", content)
            result = subprocess.run([sys.executable, str(script), "failure-body", "--stage", "checkout",
                                     "--run-url", "https://example.com", "--head-sha", "abcdef123456"],
                                    capture_output=True, text=True, encoding="utf-8", check=True, env=env)
            self.assertIn("abcdef1", result.stdout)


if __name__ == "__main__":
    unittest.main()
