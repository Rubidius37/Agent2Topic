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
                "--vault-dir",
                vault_dir,
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
        raw = buf.getvalue()
        return code, json.loads(raw), raw

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
        code, data, _ = self.run_cli_json("rules")
        self.assertEqual(code, 0)
        self.assertEqual(data["count"], 6)

        code, added, _ = self.run_cli_json(
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

        code, updated_rules, _ = self.run_cli_json("rules")
        self.assertEqual(code, 0)
        paths = [item["path"] for item in updated_rules["categories"]]
        self.assertIn(["Projects", "Alpha", "Deployment"], paths)

    def test_append_sanitizes_agent_headings_and_compiles_context(self):
        with patch.object(agent_cli, "run_production_classifier", side_effect=inprocess_classifier):
            code, res, _ = self.run_cli_json(
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

            code, ctx, _ = self.run_cli_json("context", "--topic", "Projects/Alpha")
            self.assertEqual(code, 0)
            self.assertEqual(ctx["total_entries"], 1)
            self.assertIn("**Summary**", ctx["entries"][0]["content"])
            self.assertIn("# comment in code", ctx["entries"][0]["content"])
            self.assertTrue(ctx["review_markdown"])

            source_id = ctx["entries"][0]["source_id"]
            code, src, _ = self.run_cli_json("source", "--id", source_id)
            self.assertEqual(code, 0)
            self.assertEqual(src["source_id"], source_id)

    def test_same_topic_entries_keep_distinct_source_ids_and_metadata_with_preindexed_search(self):
        with patch.object(agent_cli, "run_production_classifier", side_effect=inprocess_classifier):
            # Entry 1: decision on src/auth.py
            code, first, _ = self.run_cli_json(
                "append",
                "--path",
                "Projects/Alpha/Testing",
                "--title",
                "인증 아키텍처 결정",
                "--kind",
                "decision",
                "--files",
                "src/auth.py",
                "--commit",
                "a1b2c3d",
                "--date",
                "2026-10-18",
                "--content",
                "Replaced legacy token handling with session cookies.",
            )
            self.assertEqual(code, 0)
            self.assertFalse(first["reused_topic_headings"])

            # Entry 2 on same date & same topic: bugfix on src/login.py
            code, second, _ = self.run_cli_json(
                "append",
                "--path",
                "Projects/Alpha/Testing",
                "--title",
                "로그인 버그 수정",
                "--kind",
                "bugfix",
                "--files",
                "src/login.py",
                "--commit",
                "e4f5a6b",
                "--date",
                "2026-10-18",
                "--content",
                "Fixed crash on expired session and checked `scripts/workspace_viewer.py`.",
            )
            self.assertEqual(code, 0)
            self.assertTrue(second["reused_topic_headings"])

            # Entry 3 on same date under sibling topic Projects/Alpha/Planning
            code, third, _ = self.run_cli_json(
                "append",
                "--path",
                "Projects/Alpha/Planning",
                "--title",
                "Roadmap update",
                "--kind",
                "note",
                "--date",
                "2026-10-18",
                "--content",
                "Adopted bounded context modes.",
            )
            self.assertEqual(code, 0)
            self.assertFalse(third["reused_topic_headings"])

            log_file = os.path.join(self.vault, "Daily_Logs", "2026-10-18.md")
            with open(log_file, "r", encoding="utf-8") as f:
                raw_log = f.read()
            # Parent headings are written only once
            self.assertEqual(raw_log.count("# Projects"), 1)
            self.assertEqual(raw_log.count("## Alpha"), 1)
            self.assertEqual(raw_log.count("### Testing"), 1)
            self.assertIn("### Planning", raw_log)

            # Both Testing entries have distinct Source IDs in context
            code, ctx, _ = self.run_cli_json("context", "--topic", "Projects/Alpha/Testing")
            self.assertEqual(code, 0)
            self.assertEqual(ctx["total_entries"], 2)
            ids = {e["source_id"] for e in ctx["entries"]}
            self.assertEqual(len(ids), 2)

            # Prove search uses pre-indexed metadata without touching locate_source_unit
            with patch.object(
                agent_cli,
                "locate_source_unit",
                side_effect=AssertionError("search should use pre-indexed metadata without reading source files"),
            ):
                code, by_decision, _ = self.run_cli_json("search", "--kind", "decision")
                self.assertEqual(code, 0)
                self.assertEqual(by_decision["total_matches"], 1)
                self.assertEqual(by_decision["results"][0]["title"], "인증 아키텍처 결정")

                code, by_bugfix, _ = self.run_cli_json("search", "--kind", "bugfix")
                self.assertEqual(code, 0)
                self.assertEqual(by_bugfix["total_matches"], 1)
                self.assertEqual(by_bugfix["results"][0]["title"], "로그인 버그 수정")
                self.assertEqual(by_bugfix["results"][0]["kind"], "bugfix")

                code, by_commit, _ = self.run_cli_json("search", "--commit", "e4f5")
                self.assertEqual(code, 0)
                self.assertEqual(by_commit["total_matches"], 1)
                self.assertEqual(by_commit["results"][0]["title"], "로그인 버그 수정")

                code, by_inline_file, _ = self.run_cli_json("search", "--file", "workspace_viewer.py")
                self.assertEqual(code, 0)
                self.assertEqual(by_inline_file["total_matches"], 1)

    def test_context_enforces_max_chars_budget_and_modes_on_large_entry(self):
        with patch.object(agent_cli, "run_production_classifier", side_effect=inprocess_classifier):
            huge_body = "A" * 96000
            code, _res, _ = self.run_cli_json(
                "append",
                "--path",
                "Projects/Alpha/Testing",
                "--title",
                "96KB stress log",
                "--kind",
                "experiment",
                "--date",
                "2026-10-18",
                "--content",
                huge_body,
            )
            self.assertEqual(code, 0)

            code, ctx_review, raw_review_json = self.run_cli_json(
                "context",
                "--topic",
                "Projects/Alpha/Testing",
                "--mode",
                "review",
                "--max-chars",
                "4000",
                "--limit",
                "1",
            )
            self.assertEqual(code, 0)
            self.assertTrue(ctx_review["budget_truncated"])
            self.assertTrue(ctx_review["entries"][0]["truncated"])
            self.assertLess(len(raw_review_json), 6500)

            code, ctx_index, raw_index_json = self.run_cli_json(
                "context",
                "--topic",
                "Projects/Alpha/Testing",
                "--mode",
                "index",
            )
            self.assertEqual(code, 0)
            self.assertIsNone(ctx_index["review_markdown"])
            self.assertLess(len(raw_index_json), 2500)

            # Full source is still retrievable via `source --id`
            sid = ctx_index["entries"][0]["source_id"]
            code, full_src, _ = self.run_cli_json("source", "--id", sid)
            self.assertEqual(code, 0)
            self.assertGreaterEqual(len(full_src["content"]), 96000)

    def test_strict_500_char_json_budget_with_30_entries_and_pagination(self):
        with patch.object(agent_cli, "run_production_classifier", side_effect=inprocess_classifier):
            for i in range(30):
                code, _res, _ = self.run_cli_json(
                    "append",
                    "--path",
                    "Projects/Alpha/Testing",
                    "--title",
                    f"Bugfix iteration {i:02d}",
                    "--kind",
                    "bugfix",
                    "--files",
                    f"src/module_{i:02d}.py",
                    "--date",
                    "2026-10-18",
                    "--no-update",
                    "--content",
                    f"Resolved edge case {i:02d} in authentication handler.",
                )
                self.assertEqual(code, 0)

            code, _upd, _ = self.run_cli_json("update")
            self.assertEqual(code, 0)

            for mode in ("index", "review", "entries"):
                code, data, raw_json = self.run_cli_json(
                    "context",
                    "--topic",
                    "Projects/Alpha/Testing",
                    "--mode",
                    mode,
                    "--max-chars",
                    "500",
                    "--limit",
                    "30",
                )
                self.assertEqual(code, 0)
                self.assertLessEqual(
                    len(raw_json.strip()),
                    500,
                    f"context --mode {mode} exceeded 500 chars: {len(raw_json.strip())}",
                )
                self.assertTrue(data["budget_truncated"])
                self.assertTrue(data["has_more"])

            code, s_data, raw_search_json = self.run_cli_json(
                "search",
                "--kind",
                "bugfix",
                "--max-chars",
                "500",
                "--limit",
                "30",
            )
            self.assertEqual(code, 0)
            self.assertLessEqual(
                len(raw_search_json.strip()),
                500,
                f"search exceeded 500 chars: {len(raw_search_json.strip())}",
            )
            self.assertTrue(s_data["budget_truncated"])
            self.assertTrue(s_data["has_more"])

            # Pagination with larger budget (1800 chars) returns multiple entries and next_offset
            code, page1, raw_p1 = self.run_cli_json(
                "context",
                "--topic",
                "Projects/Alpha/Testing",
                "--mode",
                "index",
                "--max-chars",
                "1800",
                "--limit",
                "30",
            )
            self.assertEqual(code, 0)
            self.assertLessEqual(len(raw_p1.strip()), 1800)
            self.assertGreater(page1["returned_entries"], 0)
            self.assertLess(page1["returned_entries"], 30)
            self.assertTrue(page1["has_more"])
            self.assertEqual(page1["next_offset"], page1["returned_entries"])

    def test_search_beyond_4000_chars_and_agent_session_metadata(self):
        with patch.object(agent_cli, "run_production_classifier", side_effect=inprocess_classifier):
            long_body = ("padding_word " * 360) + "\nspecial_needle_term at 4700 chars."
            self.assertGreater(len(long_body), 4500)
            code, res, _ = self.run_cli_json(
                "append",
                "--path",
                "Projects/Alpha/Testing",
                "--title",
                "Deep search & provenance test",
                "--kind",
                "experiment",
                "--agent",
                "claude-code",
                "--session",
                "ses_998877",
                "--date",
                "2026-10-18",
                "--content",
                long_body,
            )
            self.assertEqual(code, 0)
            self.assertEqual(res["agent"], "claude-code")
            self.assertEqual(res["session"], "ses_998877")

            # Search finds special_needle_term beyond 4,000 chars automatically via truncated flag fallback
            code, found, _ = self.run_cli_json("search", "--query", "special_needle_term")
            self.assertEqual(code, 0)
            self.assertEqual(found["total_matches"], 1)
            self.assertEqual(found["results"][0]["agent"], "claude-code")
            self.assertEqual(found["results"][0]["session"], "ses_998877")

            # Also filter by --agent and --session and --deep
            code, by_agent, _ = self.run_cli_json(
                "search",
                "--agent",
                "claude-code",
                "--session",
                "ses_998877",
                "--deep",
            )
            self.assertEqual(code, 0)
            self.assertEqual(by_agent["total_matches"], 1)

    def test_append_with_create_rule_and_manual_classify(self):
        with patch.object(agent_cli, "run_production_classifier", side_effect=inprocess_classifier):
            code, res, _ = self.run_cli_json(
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

            code, upd, _ = self.run_cli_json("update")
            self.assertEqual(code, 0)

            code, status, _ = self.run_cli_json("status")
            self.assertEqual(code, 0)
            self.assertEqual(status["needs_review_count"], 1)
            target_id = status["needs_review"][0]["source_id"]

            code, classified, _ = self.run_cli_json(
                "classify",
                "--source-id",
                target_id,
                "--path",
                "Knowledge/Architecture",
            )
            self.assertEqual(code, 0)
            self.assertTrue(classified["changed"])

            code, status_after, _ = self.run_cli_json("status")
            self.assertEqual(code, 0)
            self.assertEqual(status_after["needs_review_count"], 0)

    def test_real_subprocess_classifier_on_external_vault_and_rebuild_ids(self):
        code, res, _ = self.run_cli_json(
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

        code, rebuilt, _ = self.run_cli_json("update", "--rebuild-ids")
        self.assertEqual(code, 0)
        self.assertTrue(rebuilt["rebuild_ids"])

    def test_version_subcommand_and_remote_update_check(self):
        code, ver, _ = self.run_cli_json("version")
        self.assertEqual(code, 0)
        self.assertEqual(ver["version"], agent_cli.PAGENT_VERSION)
        self.assertFalse(ver["checked_remote"])

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "tag_name": "v9.9.0",
                        "html_url": "https://github.com/Rubidius37/Project_Agent/releases/tag/v9.9.0",
                    }
                ).encode("utf-8")

        with patch.object(agent_cli.urllib.request, "urlopen", return_value=FakeResponse()):
            code, checked, _ = self.run_cli_json("version", "--check")
            self.assertEqual(code, 0)
            self.assertTrue(checked["checked_remote"])
            self.assertTrue(checked["update_available"])
            self.assertEqual(checked["latest_version"], "9.9.0")
            self.assertIn("uv tool upgrade agent2topic", checked["upgrade_commands"])


if __name__ == "__main__":
    unittest.main()
