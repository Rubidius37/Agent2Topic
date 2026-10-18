import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import agent_cli  # noqa: E402
import hierarchical_classifier  # noqa: E402


RULES = """\
| Level 1 | Level 2 | Level 3 | Level 4 | Level 5 | Keywords |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Projects | Alpha | Testing | | | alpha test |
| | | Planning | | | alpha plan |
| Knowledge | Architecture | | | | arch |
"""


def inprocess_classifier(vault_dir, lock_held_by_parent=False, extra_args=()):
    fake_script = os.path.join(vault_dir, "scripts", "hierarchical_classifier.py")
    buf = io.StringIO()
    with redirect_stdout(buf), patch.object(
        hierarchical_classifier, "__file__", fake_script
    ), patch("hierarchical_classifier.resolve_workspace_dir", return_value=vault_dir):
        rc = hierarchical_classifier._main_unlocked(
            [
                "--production",
                "--output-dir",
                "Subject",
                "--review-dir",
                "Topic_Reviews",
                "--metadata-file",
                "organizer_metadata.json",
                *extra_args,
            ]
        )
    return {"ok": rc == 0, "returncode": rc, "output": buf.getvalue().strip()}


class AgentCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = self.temp.name
        os.makedirs(os.path.join(self.vault, "Daily_Logs"))
        os.makedirs(os.path.join(self.vault, "scripts"))
        self.rules_path = os.path.join(self.vault, "Classification_Rules.md")
        with open(self.rules_path, "w", encoding="utf-8") as file_obj:
            file_obj.write(RULES)

    def tearDown(self):
        self.temp.cleanup()

    def run_cli_json(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = agent_cli.main(["--vault-dir", self.vault, *args, "--json"])
        return code, json.loads(buf.getvalue())

    def test_init_creates_workspace_structure_when_empty(self):
        with tempfile.TemporaryDirectory() as empty_dir:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = agent_cli.main(["--vault-dir", empty_dir, "init", "--json"])
            data = json.loads(buf.getvalue())
            self.assertEqual(code, 0)
            self.assertIn("Classification_Rules.md", data["created"])
            self.assertTrue(os.path.isdir(os.path.join(empty_dir, "Daily_Logs")))
            self.assertTrue(os.path.isdir(os.path.join(empty_dir, "attachments")))

    def test_rules_and_rule_add_extend_markdown_table(self):
        code, data = self.run_cli_json("rules")
        self.assertEqual(code, 0)
        self.assertEqual(data["count"], 6)

        code, added = self.run_cli_json(
            "rule-add",
            "--path",
            "Projects/Alpha/Deployment",
            "--keywords",
            "deploy, release",
            "--no-update",
        )
        self.assertEqual(code, 0)
        self.assertTrue(added["changed"])
        self.assertEqual(added["path"], ["Projects", "Alpha", "Deployment"])

        code, updated_rules = self.run_cli_json("rules")
        self.assertEqual(code, 0)
        paths = [item["path"] for item in updated_rules["categories"]]
        self.assertIn(["Projects", "Alpha", "Deployment"], paths)

    def test_append_sanitizes_agent_headings_and_compiles_context(self):
        with patch.object(agent_cli, "run_production_classifier", side_effect=inprocess_classifier):
            code, res = self.run_cli_json(
                "append",
                "--path",
                "Projects/Alpha/Testing",
                "--title",
                "Cache invalidation fix",
                "--date",
                "2026-10-18",
                "--content",
                "# Summary\nFixed race condition.\n```python\n# comment in code\nx = 1\n```",
            )
            self.assertEqual(code, 0)
            self.assertEqual(res["log_path"], "Daily_Logs/2026-10-18.md")

            code, ctx = self.run_cli_json("context", "--topic", "Projects/Alpha")
            self.assertEqual(code, 0)
            self.assertEqual(ctx["total_entries"], 1)
            self.assertIn("###### Summary", ctx["entries"][0]["content"])
            self.assertIn("# comment in code", ctx["entries"][0]["content"])
            self.assertTrue(ctx["review_markdown"])

            source_id = ctx["entries"][0]["source_id"]
            code, src = self.run_cli_json("source", "--id", source_id)
            self.assertEqual(code, 0)
            self.assertEqual(src["source_id"], source_id)

    def test_search_by_file_kind_query_and_smart_same_topic_merge(self):
        with patch.object(agent_cli, "run_production_classifier", side_effect=inprocess_classifier):
            # First append under Projects/Alpha/Testing with explicit --files and --kind
            code, first = self.run_cli_json(
                "append",
                "--path",
                "Projects/Alpha/Testing",
                "--title",
                "Atomic write verification",
                "--kind",
                "bugfix",
                "--files",
                "scripts/review_renderer.py, scripts/atomic_io.py",
                "--date",
                "2026-10-18",
                "--content",
                "Replaced open() with atomic_write_text.",
            )
            self.assertEqual(code, 0)
            self.assertFalse(first["merged_into_last_unit"])

            # Second append on same date and same topic -> merges into last unit without repeating H1-H3
            code, second = self.run_cli_json(
                "append",
                "--path",
                "Projects/Alpha/Testing",
                "--title",
                "Follow-up edge case",
                "--date",
                "2026-10-18",
                "--content",
                "Also checked `scripts/workspace_viewer.py` for path traversal.",
            )
            self.assertEqual(code, 0)
            self.assertTrue(second["merged_into_last_unit"])

            # Third append on same date under sibling topic Projects/Alpha/Planning -> emits only ### Planning
            code, third = self.run_cli_json(
                "append",
                "--path",
                "Projects/Alpha/Planning",
                "--title",
                "Roadmap decision",
                "--kind",
                "decision",
                "--date",
                "2026-10-18",
                "--content",
                "Adopted read-only local web viewer architecture.",
            )
            self.assertEqual(code, 0)
            self.assertFalse(third["merged_into_last_unit"])

            log_file = os.path.join(self.vault, "Daily_Logs", "2026-10-18.md")
            with open(log_file, "r", encoding="utf-8") as f:
                raw_log = f.read()
            self.assertEqual(raw_log.count("# Projects"), 1)
            self.assertEqual(raw_log.count("## Alpha"), 1)
            self.assertIn("### Planning", raw_log)

            # Search by explicit file
            code, by_file = self.run_cli_json("search", "--file", "review_renderer.py")
            self.assertEqual(code, 0)
            self.assertEqual(by_file["total_matches"], 1)
            self.assertIn("scripts/review_renderer.py", by_file["results"][0]["files"])

            # Search by backticked file in prose
            code, by_inline_file = self.run_cli_json("search", "--file", "workspace_viewer.py")
            self.assertEqual(code, 0)
            self.assertEqual(by_inline_file["total_matches"], 1)

            # Search by kind
            code, by_kind = self.run_cli_json("search", "--kind", "decision")
            self.assertEqual(code, 0)
            self.assertEqual(by_kind["total_matches"], 1)
            self.assertEqual(by_kind["results"][0]["categories"], [["Projects", "Alpha", "Planning"]])

            # Search by keyword query
            code, by_query = self.run_cli_json("search", "--query", "atomic_write_text")
            self.assertEqual(code, 0)
            self.assertEqual(by_query["total_matches"], 1)
            self.assertTrue(by_query["related_reviews"])

    def test_append_with_create_rule_and_manual_classify(self):
        with patch.object(agent_cli, "run_production_classifier", side_effect=inprocess_classifier):
            code, res = self.run_cli_json(
                "append",
                "--path",
                "Projects/Beta/Benchmarks",
                "--content",
                "Measured throughput under lock contention.",
                "--date",
                "2026-10-18",
                "--create-rule",
            )
            self.assertEqual(code, 0)
            self.assertEqual(res["category_path"], ["Projects", "Beta", "Benchmarks"])

            unclassified_log = os.path.join(self.vault, "Daily_Logs", "2026-10-19.md")
            with open(unclassified_log, "w", encoding="utf-8") as file_obj:
                file_obj.write("# Unknown Heading\nMystery note needing manual triage.\n")

            code, upd = self.run_cli_json("update")
            self.assertEqual(code, 0)

            code, status = self.run_cli_json("status")
            self.assertEqual(code, 0)
            self.assertEqual(status["needs_review_count"], 1)
            target_id = status["needs_review"][0]["source_id"]

            code, classified = self.run_cli_json(
                "classify",
                "--source-id",
                target_id,
                "--path",
                "Knowledge/Architecture",
            )
            self.assertEqual(code, 0)
            self.assertTrue(classified["changed"])

            code, status_after = self.run_cli_json("status")
            self.assertEqual(code, 0)
            self.assertEqual(status_after["needs_review_count"], 0)

    def test_real_subprocess_classifier_on_external_vault_and_rebuild_ids(self):
        code, res = self.run_cli_json(
            "append",
            "--path",
            "Projects/Alpha/Testing",
            "--title",
            "End-to-end subprocess test",
            "--date",
            "2026-10-18",
            "--content",
            "Verified subprocess classifier with --vault-dir.",
        )
        self.assertEqual(code, 0)
        self.assertTrue(
            os.path.isfile(os.path.join(self.vault, "scripts", "organizer_metadata.json"))
        )

        code, rebuilt = self.run_cli_json("update", "--rebuild-ids")
        self.assertEqual(code, 0)
        self.assertTrue(rebuilt["rebuild_ids"])


if __name__ == "__main__":
    unittest.main()
