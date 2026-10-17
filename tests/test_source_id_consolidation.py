"""Heading edits may merge units; fixtures contain fictional content only."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import hierarchical_classifier as classifier

RULES = """| Level 1 | Level 2 | Level 3 | Level 4 | Level 5 | Keywords |
| --- | --- | --- | --- | --- | --- |
| Projects | Sample | Testing | | | |
| | | Other | | | |
"""
A, B, C = "a" * 16, "b" * 16, "c" * 16


def marker(value):
    return classifier.source_id_comment(value)


class SourceIdConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.daily = self.root / "Workspace" / "Daily_Logs"
        self.daily.mkdir(parents=True)
        rules = self.daily.parent / "Classification_Rules.md"
        rules.write_text(RULES, encoding="utf-8")
        self.tree = classifier.parse_rules_from_markdown(str(rules))

    def tearDown(self):
        self.temp.cleanup()

    def plans(self):
        indexes = classifier.build_id_indexes({"entries": {}})
        return classifier.build_source_marker_plans(
            str(self.daily), str(self.daily.parent), self.tree, indexes, indexes
        )[0]

    def save(self, plans):
        return classifier.persist_source_marker_plans(
            plans, str(self.root / "backups"), str(self.daily.parent)
        )

    def test_keep_heading_id_preserve_body_quotes_and_backup(self):
        path = self.daily / "260630.md"
        original = (f"# Projects\r\n## Sample\r\n### Testing\r\n\r\n{marker(A)}\r\n"
                    f"body\r\n#### First item\r\n{marker(B)}\r\n"
                    f"inline {marker(C)} text\r\n> {marker(B)}\r\n")
        path.write_bytes(original.encode())
        plans = self.plans()
        self.assertEqual(plans[0].consolidations[0][1], A)
        result = self.save(plans)
        updated = path.read_bytes().decode()
        self.assertIn(f"### Testing\r\n{marker(A)}\r\n", updated)
        self.assertIn("#### First item\r\ninline  text", updated)
        self.assertIn(f"> {marker(B)}", updated)
        self.assertEqual(len(list(classifier.iter_active_source_id_matches(updated))), 1)
        self.assertEqual((Path(result[-1]) / "Daily_Logs" / path.name).read_bytes(), original.encode())
        self.assertEqual(self.plans(), [])

    def test_first_body_id_is_moved_to_heading(self):
        path = self.daily / "260623.md"
        path.write_text(f"# Projects\n## Sample\n### Testing\nbody\n#### One\n{marker(B)}\n"
                        f"#### Two\n{marker(A)}\nmore", encoding="utf-8")
        self.save(self.plans())
        updated = path.read_text(encoding="utf-8")
        self.assertIn(f"### Testing\n{marker(B)}\nbody", updated)
        self.assertNotIn(marker(A), updated)
        self.assertIn("#### Two\nmore", updated)
        self.assertFalse(updated.endswith("\n"))

    def test_relocation_and_consolidation_share_original_line_coordinates(self):
        path = self.daily / "260623.md"
        path.write_text(f"# Projects\n## Sample\n{marker(C)}\n### Testing\nchild body\n"
                        f"### Other\nbody\n{marker(B)}\n#### Nested\n{marker(A)}\nend\n",
                        encoding="utf-8")
        plans = self.plans()
        self.assertEqual(len(plans[0].relocations), 1)
        self.save(plans)
        updated = path.read_text(encoding="utf-8")
        self.assertIn(f"### Testing\n{marker(C)}\nchild body", updated)
        self.assertIn(f"### Other\n{marker(B)}\nbody", updated)
        self.assertIn("#### Nested\nend", updated)
        self.assertEqual(self.plans(), [])

    def test_repeated_same_id_in_one_unit_is_consolidated(self):
        path = self.daily / "260623.md"
        path.write_text(f"# Projects\n## Sample\n### Testing\n{marker(A)}\nbody\n{marker(A)}\n",
                        encoding="utf-8")
        self.save(self.plans())
        self.assertEqual(path.read_text(encoding="utf-8").count(marker(A)), 1)

    def test_fenced_marker_examples_and_headings_are_preserved(self):
        for fence in ("```", "~~~~"):
            with self.subTest(fence=fence):
                path = self.daily / "260623.md"
                code = f"{fence}markdown\n# Projects\n### Other\n{marker(C)}\n{fence}\n"
                original = f"# Projects\n## Sample\n### Testing\n{marker(A)}\nbody\n{code}{marker(B)}\nend\n"
                path.write_text(original, encoding="utf-8")
                plans = self.plans()
                self.assertEqual(plans[0].consolidations[0][2], [(4, A), (11, B)])
                self.save(plans)
                updated = path.read_text(encoding="utf-8")
                self.assertIn(code, updated)
                self.assertEqual([m.group(1) for m in classifier.iter_active_source_id_matches(updated)], [A])
                _, units = classifier.parse_markdown_into_units(str(path), self.tree)
                unit = next(u for u in units if u.title == "Testing")
                self.assertIn(code.strip(), classifier.classify_unit(unit, self.tree)["content"])
                self.assertEqual(self.plans(), [])

    def test_fenced_example_id_in_another_file_is_not_a_collision(self):
        first = self.daily / "260623.md"
        first.write_text(f"# Projects\n## Sample\n### Testing\n{marker(A)}\nbody\n{marker(B)}\n", encoding="utf-8")
        second = self.daily / "260630.md"
        second.write_text(f"# Projects\n## Sample\n### Testing\n{marker(C)}\n```\n{marker(B)}\n```\n", encoding="utf-8")
        self.save(self.plans())
        self.assertIn(f"```\n{marker(B)}\n```", second.read_text(encoding="utf-8"))

    def test_document_root_keeps_first_id_and_preserves_frontmatter(self):
        path = self.daily / "260623.md"
        path.write_text(f"---\ntitle: Example\n---\n{marker(A)}\n{marker(B)}\nbody\n",
                        encoding="utf-8")
        self.save(self.plans())
        self.assertEqual(path.read_text(encoding="utf-8"),
                         f"---\ntitle: Example\n---\n{marker(A)}\nbody\n")

    def test_consolidation_rolls_back_if_another_planned_file_changes(self):
        for name, first, second in (("260623.md", A, B), ("260630.md", C, "d" * 16)):
            (self.daily / name).write_text(
                f"# Projects\n## Sample\n### Testing\n{marker(first)}\nbody\n{marker(second)}\n",
                encoding="utf-8")
        first = self.daily / "260623.md"
        second = self.daily / "260630.md"
        original = first.read_bytes()
        plans = self.plans()
        second.write_text("user changed this file\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Source file changed"):
            self.save(plans)
        self.assertEqual(first.read_bytes(), original)
        self.assertEqual(second.read_text(encoding="utf-8"), "user changed this file\n")

    def test_cross_unit_or_file_duplicates_including_discarded_ids_are_rejected(self):
        for other_file in (False, True):
            with self.subTest(other_file=other_file):
                path = self.daily / "260623.md"
                original = f"# Projects\n## Sample\n### Testing\n{marker(A)}\nbody\n{marker(B)}\n"
                other = f"# Projects\n## Sample\n### Other\n{marker(B)}\nother body\n"
                path.write_text(original if other_file else original + other, encoding="utf-8")
                if other_file:
                    (self.daily / "260630.md").write_text(other, encoding="utf-8")
                before = path.read_bytes()
                with self.assertRaisesRegex(RuntimeError, "Duplicate permanent Source ID"):
                    self.plans()
                self.assertEqual(path.read_bytes(), before)

    def install_scripts(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        for source in (ROOT / "scripts").glob("*.py"):
            shutil.copy2(source, scripts)
        return scripts

    def run_classifier(self, scripts, *args):
        result = subprocess.run([sys.executable, "-B", str(scripts / "hierarchical_classifier.py"),
                                 "--production", "--output-dir", "Subject",
                                 "--review-dir", "Topic_Reviews", "--metadata-file",
                                 "organizer_metadata.json", *args], capture_output=True, text=True,
                                encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_dry_run_cleanup_metadata_stale_outputs_and_idempotence(self):
        scripts = self.install_scripts()
        path = self.daily / "260630.md"
        path.write_text(f"# Projects\n## Sample\n### Testing\n{marker(A)}\nfirst body\n"
                        f"### Other\n{marker(B)}\nsecond body\n", encoding="utf-8")
        self.run_classifier(scripts)
        metadata_path = scripts / "organizer_metadata.json"
        previous = json.loads(metadata_path.read_text(encoding="utf-8"))
        old_paths = set(previous["entries"][B]["generated_paths"])
        path.write_text(path.read_text(encoding="utf-8").replace("### Other", "#### Other"),
                        encoding="utf-8")
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*")
                  if p.is_file() and ".runtime" not in p.parts}
        output = self.run_classifier(scripts, "--dry-run")
        self.assertIn(f"keep {A}; remove {B}", output)
        self.assertIn("Source units would be consolidated: 1", output)
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*")
                 if p.is_file() and ".runtime" not in p.parts}
        self.assertEqual(before, after)
        output = self.run_classifier(scripts)
        self.assertIn("Extra Source ID markers removed: 1", output)
        current = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertIn(A, current["entries"])
        self.assertNotIn(B, current["entries"])
        for generated in old_paths:
            self.assertFalse((self.daily.parent / generated).exists())
        original = path.read_bytes()
        output = self.run_classifier(scripts)
        self.assertIn("Source units consolidated: 0", output)
        self.assertEqual(original, path.read_bytes())

    def test_default_preview_dry_run_also_shows_consolidation(self):
        scripts = self.install_scripts()
        path = self.daily / "260623.md"
        path.write_text(f"# Projects\n## Sample\n### Testing\n{marker(A)}\nbody\n{marker(B)}\n",
                        encoding="utf-8")
        original = path.read_bytes()
        result = subprocess.run([sys.executable, "-B", str(scripts / "hierarchical_classifier.py"),
                                 "--dry-run"], capture_output=True, text=True,
                                encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"keep {A}; remove {B}", result.stdout)
        self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
