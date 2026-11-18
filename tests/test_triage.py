import hashlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import triage as t  # noqa: E402

DIFF = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -10,4 +10,5 @@ def main():
 a = 1
-b = 2
+b = 3
+c = 4
 d = 5
 e = 6
diff --git a/old.py b/old.py
deleted file mode 100644
index 3333333..0000000
--- a/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-x = 1
-y = 2
"""

FILE_LINES = {
    "app.py": ["def main():", "    total  =  compute(a,", "        b)", "b = 3", "c = 4"],
    "old.py": ["x = 1", "y = 2"],
}

CONFIG = {"min_severity": "minor", "post_rule_violations": False}


def finding(**over):
    base = {
        "file": "app.py",
        "line": 11,
        "severity": "major",
        "lens": "correctness",
        "heading": "b の値が違う",
        "body": "本文",
        "quote": "b = 3",
        "rule_violation": False,
        "rule_source": "",
    }
    base.update(over)
    return base


def sha8(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def run(findings, config=CONFIG, bodies=(), file_lines=FILE_LINES, diff=DIFF):
    return t.triage(findings, file_lines, diff, config, bodies)


class ShapeTest(unittest.TestCase):
    def test_bad_finding_is_dropped_alone(self):
        good = finding()
        bad = [
            "not a dict",
            {k: v for k, v in finding().items() if k != "heading"},
            finding(severity="info"),
            finding(severity="Major"),
            finding(line="11"),
            finding(line=True),
            finding(line=0),
            finding(rule_violation="false"),
            finding(quote=""),
            finding(file="   "),
            finding(heading="two\nlines"),
            finding(heading="breaks --> marker"),
            finding(lens="breaks --> marker"),
            finding(lens="two\nlines"),
            finding(lens="has ] bracket"),
            finding(body=None),
        ]
        result = run(bad[:5] + [good] + bad[5:])
        self.assertEqual(result.malformed_count, len(bad))
        self.assertEqual([f.heading for f in result.inline], [good["heading"]])

    def test_extra_keys_are_ignored(self):
        result = run([finding(extra="x")])
        self.assertEqual(len(result.inline), 1)
        self.assertEqual(result.malformed_count, 0)


class QuoteTest(unittest.TestCase):
    def test_quote_with_different_whitespace_is_kept(self):
        result = run([finding(quote="total = compute(a,\n b)")])
        self.assertEqual(len(result.inline), 1)
        self.assertEqual(result.quote_dropped_count, 0)

    def test_quote_not_in_bundle_is_dropped_and_counted(self):
        result = run([finding(quote="b = 4"), finding(quote="b=3"), finding()])
        self.assertEqual(result.quote_dropped_count, 2)
        self.assertEqual(len(result.inline), 1)

    def test_quote_must_be_in_the_same_file(self):
        result = run([finding(quote="x = 1")])
        self.assertEqual(result.quote_dropped_count, 1)
        self.assertEqual(result.inline, [])

    def test_file_not_in_bundle_is_dropped(self):
        result = run([finding(file="other.py")])
        self.assertEqual(result.quote_dropped_count, 1)

    def test_quote_with_line_number_prefix_is_kept(self):
        result = run([finding(quote="12: total = compute(a, b)")])
        self.assertEqual(len(result.inline), 1)
        self.assertEqual(result.quote_dropped_count, 0)

    def test_quote_with_diff_marker_is_kept(self):
        result = run([finding(quote="+total = compute(a, b)")])
        self.assertEqual(len(result.inline), 1)
        self.assertEqual(result.quote_dropped_count, 0)

    def test_multi_line_quote_with_prefixes_is_kept(self):
        result = run([finding(quote="12: total = compute(a,\n13:  b)")])
        self.assertEqual(len(result.inline), 1)

    def test_whitespace_only_quote_is_dropped(self):
        result = run([finding(quote=" \n\t")])
        self.assertEqual(result.malformed_count + result.quote_dropped_count, 1)
        self.assertEqual(result.inline, [])


class DiffRangeTest(unittest.TestCase):
    def test_new_side_lines(self):
        ranges = t.new_side_lines(DIFF)
        self.assertEqual(ranges["app.py"], {10, 11, 12, 13, 14})
        self.assertEqual(ranges["old.py"], set())

    def test_empty_context_line_counts(self):
        diff = (
            "diff --git a/f b/f\n--- a/f\n+++ b/f\n"
            "@@ -1,3 +1,3 @@\n a\n\n-b\n+c\n"
        )
        self.assertEqual(t.new_side_lines(diff)["f"], {1, 2, 3})

    def test_line_outside_diff_goes_to_outside(self):
        result = run([finding(line=2, quote="def main():"), finding(line=13)])
        self.assertEqual([f.line for f in result.outside_diff], [2])
        self.assertEqual([f.line for f in result.inline], [13])

    def test_removed_line_number_is_outside(self):
        # Line 15 exists only on the old side (old 10..13 -> new 10..14).
        result = run([finding(line=15)])
        self.assertEqual(len(result.outside_diff), 1)
        self.assertEqual(result.inline, [])


class LowerBoundTest(unittest.TestCase):
    def test_lighter_than_min_is_counted_not_shown(self):
        config = {"min_severity": "major", "post_rule_violations": False}
        result = run(
            [finding(severity="minor"), finding(severity="major", lens="l2"),
             finding(severity="critical", lens="l3"),
             finding(severity="minor", line=2, quote="def main():", heading="h4")],
            config,
        )
        self.assertEqual(result.below_min_count, 2)
        self.assertEqual(sorted(f.severity for f in result.inline), ["critical", "major"])
        self.assertEqual(result.outside_diff, [])

    def test_rule_violation_ignores_min_when_enabled(self):
        rv = finding(severity="minor", rule_violation=True, rule_source="CLAUDE.md")
        on = run([rv], {"min_severity": "critical", "post_rule_violations": True})
        off = run([rv], {"min_severity": "critical", "post_rule_violations": False})
        self.assertEqual(len(on.inline), 1)
        self.assertEqual(on.below_min_count, 0)
        self.assertEqual(off.inline, [])
        self.assertEqual(off.below_min_count, 1)

    def test_non_rule_finding_still_bounded_when_enabled(self):
        result = run([finding(severity="minor")],
                     {"min_severity": "major", "post_rule_violations": True})
        self.assertEqual(result.below_min_count, 1)

    def test_default_min_is_minor(self):
        result = run([finding(severity="minor")], {})
        self.assertEqual(len(result.inline), 1)

    def test_invalid_min_severity_raises(self):
        with self.assertRaises(ValueError):
            run([finding()], {"min_severity": "info"})


class HashTest(unittest.TestCase):
    def test_hash_is_sha256_of_squeezed_quote_first_8(self):
        self.assertEqual(t.quote_hash("  b   =\n 3 "), sha8("b = 3"))
        self.assertEqual(len(t.quote_hash("x")), 8)

    def test_hash_ignores_line_number_prefix_and_diff_marker(self):
        self.assertEqual(t.quote_hash("12: b = 3"), sha8("b = 3"))
        self.assertEqual(t.quote_hash("+b = 3"), sha8("b = 3"))
        self.assertEqual(t.quote_hash("7: +b = 3"), sha8("b = 3"))

    def test_finding_hash(self):
        result = run([finding(quote="b  =  3")])
        self.assertEqual(result.inline[0].hash, sha8("b = 3"))


class SameFindingTest(unittest.TestCase):
    def marker(self, file, line, heading, h=None, lens="correctness"):
        tail = f" #{h} [{lens}]" if h else ""
        return f"<!-- lens-review: {file}:{line} {heading}{tail} -->\n**[major] {heading}**"

    def test_read_markers_both_forms(self):
        bodies = [self.marker("app.py", 3, "見出し A", "ABCDEF12", lens="規約と  仕様"),
                  self.marker("app.py", 9, "見出し B")]
        self.assertEqual(t.read_markers(bodies), [
            t.Marker("app.py", "見出し A", "abcdef12", "規約と 仕様"),
            t.Marker("app.py", "見出し B", None, None),
        ])

    def test_hashed_marker_without_lens_is_read_as_old_form(self):
        body = "<!-- lens-review: app.py:3 見出し A #abcdef12 -->"
        self.assertEqual(t.read_markers([body]),
                         [t.Marker("app.py", "見出し A #abcdef12", None, None)])

    def test_new_marker_matches_file_lens_hash_ignoring_heading_and_line(self):
        f = finding()
        bodies = [self.marker("app.py", 999, "言い換えた見出し", sha8("b = 3"))]
        result = run([f], bodies=bodies)
        self.assertEqual(result.inline, [])
        self.assertEqual(len(result.previous), 1)

    def test_same_file_and_lens_with_other_hash_is_kept(self):
        f = finding()
        bodies = [self.marker("app.py", 11, f["heading"], sha8("c = 4"))]
        result = run([f], bodies=bodies)
        self.assertEqual(len(result.inline), 1)
        self.assertEqual(result.previous, [])

    def test_same_quote_with_other_lens_is_kept(self):
        f = finding(lens="規約と仕様")
        bodies = [self.marker("app.py", 11, f["heading"], sha8("b = 3"), lens="correctness")]
        result = run([f], bodies=bodies)
        self.assertEqual(len(result.inline), 1)

    def test_old_marker_matches_file_and_heading_only(self):
        f = finding()
        result = run([f], bodies=[self.marker("app.py", 1, f["heading"])])
        self.assertEqual(len(result.previous), 1)
        self.assertEqual(result.inline, [])

    def test_other_file_or_heading_does_not_match(self):
        f = finding()
        bodies = [self.marker("other.py", 11, f["heading"], sha8("b = 3")),
                  self.marker("app.py", 11, "別の見出し")]
        result = run([f], bodies=bodies)
        self.assertEqual(len(result.inline), 1)

    def test_previous_also_applies_outside_diff(self):
        f = finding(line=2, quote="def main():")
        result = run([f], bodies=[self.marker("app.py", 2, f["heading"])])
        self.assertEqual(len(result.previous), 1)
        self.assertEqual(result.outside_diff, [])

    def test_repeat_in_same_run_is_not_posted_twice(self):
        result = run([finding(), finding(line=12, heading="言い換え")])
        self.assertEqual(len(result.inline), 1)
        self.assertEqual(len(result.repeated), 1)

    def test_same_quote_other_lens_in_same_run_is_kept(self):
        result = run([finding(), finding(lens="規約と仕様")])
        self.assertEqual(len(result.inline), 2)


class OrderTest(unittest.TestCase):
    def test_each_finding_lands_in_one_category_by_stage_order(self):
        config = {"min_severity": "major", "post_rule_violations": False}
        heading = "b の値が違う"
        bodies = [f"<!-- lens-review: app.py:11 {heading} #{sha8('b = 3')} [correctness] -->"]
        findings = [
            finding(severity="bogus"),                              # 1: shape
            finding(severity="bogus", quote="nowhere"),             # 1 before 2
            finding(quote="nowhere"),                               # 2: quote
            finding(quote="nowhere", severity="minor"),             # 2 before 4
            finding(severity="minor"),                              # 4 before 5
            finding(),                                              # 5: previous
            finding(heading="新しい", quote="c = 4"),                # inline
            finding(heading="外", line=2, quote="def main():"),      # 3: outside
        ]
        result = run(findings, config, bodies)
        self.assertEqual(result.malformed_count, 2)
        self.assertEqual(result.quote_dropped_count, 2)
        self.assertEqual(result.below_min_count, 1)
        self.assertEqual([f.heading for f in result.previous], [heading])
        self.assertEqual([f.heading for f in result.inline], ["新しい"])
        self.assertEqual([f.heading for f in result.outside_diff], ["外"])

    def test_no_gh_or_api_calls(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("gh called")), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("api called")):
            result = run([finding()])
        self.assertEqual(len(result.inline), 1)


if __name__ == "__main__":
    unittest.main()
