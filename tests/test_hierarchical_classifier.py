import hashlib
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import hierarchical_classifier  # noqa: E402
from hierarchical_classifier import (  # noqa: E402
    SourceUnit,
    SourceMarkerPlan,
    SOURCE_ID_RE,
    build_id_indexes,
    build_source_marker_plans,
    classify_unit,
    count_daily_markdown_files,
    is_document_preamble,
    make_markdown_link,
    make_source_anchor,
    parse_markdown_into_units,
    parse_rules_from_markdown,
    parse_args,
    persist_source_marker_plans,
    render_source_id_markers,
    rebuild_source_id_markers,
    rewrite_image_links_for_generated_document,
    resolve_source_id,
    should_persist_source_ids,
    source_unit_fingerprint,
    validate_output_targets,
    write_reviews,
)


RULES = """\
| Level 1 | Level 2 | Level 3 | Level 4 | Level 5 | Keywords |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Projects | Project Alpha | | | | Project Alpha, Alpha |
| | | Testing | | | testing |
| | | | Functional Test | | Functional Test, acceptance criteria |
| | Project Beta | | | | Project Beta, Beta |
| | | Testing | | | testing |
| Knowledge | Analysis | Qualitative | | | qualitative analysis |
| | | Quantitative | | | quantitative analysis |
| Tool | Markdown Editor | | | | markdown editor |
"""


class HierarchicalClassifierTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.rules_path = os.path.join(self.temp_dir.name, "Classification_Rules.md")
        with open(self.rules_path, "w", encoding="utf-8") as file_obj:
            file_obj.write(RULES)
        self.tree = parse_rules_from_markdown(self.rules_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_log(self, content, name="260814 일지.md"):
        path = os.path.join(self.temp_dir.name, name)
        with open(path, "w", encoding="utf-8") as file_obj:
            file_obj.write(content)
        return path

    def test_generated_document_rewrites_both_image_link_formats(self):
        source_dir = os.path.join(self.temp_dir.name, "Daily_Logs", "2026-08")
        attachments_dir = os.path.join(self.temp_dir.name, "attachments")
        os.makedirs(source_dir)
        os.makedirs(attachments_dir)
        source_path = os.path.join(source_dir, "260828 일지.md")
        image_name = "Pasted image 20260828134722.png"
        image_path = os.path.join(attachments_dir, image_name)
        with open(source_path, "w", encoding="utf-8") as file_obj:
            file_obj.write("")
        with open(image_path, "wb") as file_obj:
            file_obj.write(b"image")

        content = (
            "![](../../attachments/Pasted%20image%2020260828134722.png)\n"
            "![[Pasted image 20260828134722.png]]"
        )
        generated_path = (
            "Subject/Projects/Sample Project/Testing/260828.md"
        )
        rewritten = rewrite_image_links_for_generated_document(
            content, source_path, generated_path, self.temp_dir.name
        )

        expected = "../../../../attachments/Pasted%20image%2020260828134722.png"
        self.assertEqual(rewritten, f"![]({expected})\n![]({expected})")

        with_alt_and_title = (
            '![화면 캡처](../../attachments/Pasted%20image%2020260828134722.png "화면")\n'
            "```markdown\n![예시](not-real.png)\n```"
        )
        rewritten_alt = rewrite_image_links_for_generated_document(
            with_alt_and_title, source_path, generated_path, self.temp_dir.name
        )
        self.assertEqual(
            rewritten_alt,
            f"![화면 캡처]({expected})\n```markdown\n![예시](not-real.png)\n```",
        )

    def test_inherited_table_builds_full_tree_without_repeating_parents(self):
        self.assertFalse(self.tree.errors)
        self.assertIn(
            ("Projects", "Project Alpha", "Testing", "Functional Test"),
            self.tree.nodes,
        )
        self.assertIn(("Knowledge", "Analysis", "Quantitative"), self.tree.nodes)

    def test_production_roots_require_explicit_exact_targets(self):
        preview_args = parse_args(["--output-dir", "Subject"])
        self.assertIn("Preview mode", validate_output_targets(preview_args))

        production_args = parse_args(
            [
                "--production",
                "--output-dir",
                "Subject",
                "--review-dir",
                "Topic_Reviews",
                "--metadata-file",
                "organizer_metadata.json",
            ]
        )
        self.assertEqual(validate_output_targets(production_args), "")
        self.assertTrue(should_persist_source_ids(production_args))

        read_only_args = parse_args(
            [
                "--production",
                "--output-dir",
                "Subject",
                "--review-dir",
                "Topic_Reviews",
                "--metadata-file",
                "organizer_metadata.json",
                "--no-persist-source-ids",
            ]
        )
        self.assertFalse(should_persist_source_ids(read_only_args))
        self.assertFalse(should_persist_source_ids(preview_args))

    def test_daily_inventory_count_distinguishes_empty_and_populated_roots(self):
        daily_dir = os.path.join(self.temp_dir.name, "Daily_Logs")
        os.makedirs(os.path.join(daily_dir, "2026-08"))
        self.assertEqual(count_daily_markdown_files(daily_dir), 0)

        with open(
            os.path.join(daily_dir, "2026-08", "260815.md"),
            "w",
            encoding="utf-8",
        ) as file_obj:
            file_obj.write("# Knowledge\n")
        with open(
            os.path.join(daily_dir, "ignored.txt"),
            "w",
            encoding="utf-8",
        ) as file_obj:
            file_obj.write("ignored\n")
        with open(
            os.path.join(daily_dir, "26.09.27 일지"),
            "w",
            encoding="utf-8",
        ) as file_obj:
            file_obj.write("# Knowledge\n")

        self.assertEqual(count_daily_markdown_files(daily_dir), 1)

    def test_empty_daily_inventory_preserves_existing_generated_files(self):
        vault_dir = self.temp_dir.name
        script_dir = os.path.join(vault_dir, "scripts")
        daily_dir = os.path.join(vault_dir, "Daily_Logs")
        subject_dir = os.path.join(vault_dir, "Subject")
        review_dir = os.path.join(vault_dir, "Topic_Reviews")
        os.makedirs(script_dir)
        os.makedirs(daily_dir)
        os.makedirs(subject_dir)
        os.makedirs(review_dir)
        with open(os.path.join(daily_dir, "26.09.27 일지"), "w", encoding="utf-8") as file_obj:
            file_obj.write("# Knowledge\n## Analysis\nclassification must skip this file\n")

        with open(
            os.path.join(vault_dir, "Classification_Rules.md"),
            "w",
            encoding="utf-8",
        ) as file_obj:
            file_obj.write(RULES)
        subject_path = os.path.join(subject_dir, "existing.md")
        review_path = os.path.join(review_dir, "existing.md")
        metadata_path = os.path.join(script_dir, "organizer_metadata.json")
        for path, content in (
            (subject_path, "existing subject\n"),
            (review_path, "existing review\n"),
            (metadata_path, '{"stats":{"source_files":1}}\n'),
        ):
            with open(path, "w", encoding="utf-8") as file_obj:
                file_obj.write(content)

        fake_module_path = os.path.join(script_dir, "hierarchical_classifier.py")
        with patch.object(hierarchical_classifier, "__file__", fake_module_path):
            result = hierarchical_classifier.main(
                [
                    "--production",
                    "--output-dir",
                    "Subject",
                    "--review-dir",
                    "Topic_Reviews",
                    "--metadata-file",
                    "organizer_metadata.json",
                ]
            )

        self.assertEqual(result, 2)
        for path, expected in (
            (subject_path, "existing subject\n"),
            (review_path, "existing review\n"),
            (metadata_path, '{"stats":{"source_files":1}}\n'),
        ):
            with open(path, encoding="utf-8") as file_obj:
                self.assertEqual(file_obj.read(), expected)

    def test_exact_headings_extend_to_level_three_and_structural_h4_stays_inside(self):
        path = self.write_log(
            "# Projects\n"
            "## Project Alpha\n"
            "### Testing\n"
            "#### 동작 흐름\n"
            "테스트 설명\n"
        )
        _log_name, units = parse_markdown_into_units(path, self.tree)
        testing = [unit for unit in units if unit.title == "Testing"][0]
        self.assertEqual(
            testing.category_path,
            ("Projects", "Project Alpha", "Testing"),
        )
        self.assertIn("#### 동작 흐름", testing.content)

    def test_non_category_sibling_heading_falls_back_to_parent(self):
        path = self.write_log(
            "# Projects\n"
            "## Project Alpha\n"
            "### Testing\n"
            "테스트 내용\n"
            "### 동작 흐름\n"
            "일반 내용\n"
        )
        _log_name, units = parse_markdown_into_units(path, self.tree)
        structural = [unit for unit in units if unit.title == "동작 흐름"][0]
        self.assertEqual(structural.category_path, ("Projects", "Project Alpha"))

    def test_general_level_two_headings_remain_separate_keyword_units(self):
        path = self.write_log(
            "# Knowledge\n"
            "## First analysis\n"
            "qualitative analysis result\n"
            "## Second analysis\n"
            "quantitative analysis result\n"
        )
        _log_name, units = parse_markdown_into_units(path, self.tree)
        level_two_units = [unit for unit in units if unit.title.endswith("analysis")]
        self.assertEqual([unit.title for unit in level_two_units], [
            "First analysis",
            "Second analysis",
        ])

    def test_keywords_can_create_multiple_paths_only_inside_active_level_one(self):
        unit = SourceUnit(
            title="Result comparison",
            lines=["qualitative analysis and quantitative analysis comparison with Markdown Editor"],
            category_path=("Knowledge", "Analysis"),
            heading_path=("Knowledge", "Analysis", "Result comparison"),
            start_line=1,
        )
        result = classify_unit(unit, self.tree)
        self.assertEqual(
            set(result["paths"]),
            {("Knowledge", "Analysis", "Qualitative"), ("Knowledge", "Analysis", "Quantitative")},
        )
        self.assertNotIn(("Tool", "Markdown Editor"), result["paths"])

    def test_other_project_branch_requires_its_level_two_anchor(self):
        unit = SourceUnit(
            title="Testing",
            lines=["testing logic details"],
            category_path=("Projects", "Project Alpha"),
            heading_path=("Projects", "Project Alpha", "Testing"),
            start_line=1,
        )
        result = classify_unit(unit, self.tree)
        self.assertIn(("Projects", "Project Alpha", "Testing"), result["paths"])
        self.assertNotIn(
            ("Projects", "Project Beta", "Testing"),
            result["paths"],
        )

    def test_unknown_level_one_is_reviewed_instead_of_keyword_classified(self):
        unit = SourceUnit(
            title="Random heading",
            lines=["Markdown Editor qualitative analysis testing"],
            category_path=(),
            heading_path=("Random heading",),
            start_line=1,
        )
        result = classify_unit(unit, self.tree)
        self.assertEqual(result["paths"], [("Unclassified", "Missing Level 1")])
        self.assertTrue(result["needs_review"])

    def test_date_only_document_preamble_is_not_a_classification_unit(self):
        unit = SourceUnit(
            title="26.03.10",
            lines=["**Date**: 2026-03-10", "", "---"],
            category_path=(),
            heading_path=(),
            start_line=2,
        )
        self.assertTrue(is_document_preamble(unit))

    def test_legacy_level_one_hint_scopes_keywords_without_crossing_roots(self):
        unit = SourceUnit(
            title="Old Qualitative note",
            lines=["qualitative analysis analysis with Markdown Editor"],
            category_path=(),
            heading_path=("Old Qualitative note",),
            start_line=1,
        )
        result = classify_unit(unit, self.tree, level1_hint="Knowledge")
        self.assertEqual(result["paths"], [("Knowledge", "Analysis", "Qualitative")])
        self.assertTrue(result["legacy_level1_hint"])
        self.assertNotIn(("Tool", "Markdown Editor"), result["paths"])

    def test_manual_categories_may_cross_level_one_explicitly(self):
        unit = SourceUnit(
            title="Shared note",
            lines=[
                "Category: Knowledge/Analysis/Qualitative, Tool/Markdown Editor",
                "shared content",
            ],
            category_path=("Knowledge",),
            heading_path=("Knowledge", "Shared note"),
            start_line=1,
        )
        result = classify_unit(unit, self.tree)
        self.assertEqual(
            set(result["paths"]),
            {("Knowledge", "Analysis", "Qualitative"), ("Tool", "Markdown Editor")},
        )
        self.assertNotIn("Category:", result["content"])

    def test_invalid_manual_category_is_reported(self):
        unit = SourceUnit(
            title="Bad override",
            lines=["Category: Knowledge/Unknown branch", "body"],
            category_path=("Knowledge",),
            heading_path=("Knowledge", "Bad override"),
            start_line=1,
        )
        result = classify_unit(unit, self.tree)
        self.assertTrue(result["needs_review"])
        self.assertIn("Unknown manual category", result["issues"][0])

    def test_manual_category_is_refined_when_heading_becomes_deeper(self):
        unit = SourceUnit(
            title="Functional Test",
            lines=["Category: Projects/Project Alpha/Testing", "body"],
            category_path=("Projects", "Project Alpha", "Testing", "Functional Test"),
            heading_path=("Projects", "Project Alpha", "Testing", "Functional Test"),
            start_line=1,
        )
        result = classify_unit(unit, self.tree)
        self.assertFalse(result["needs_review"])
        self.assertEqual(
            result["paths"],
            [("Projects", "Project Alpha", "Testing", "Functional Test")],
        )

    def test_manual_category_conflicts_when_heading_changes_to_sibling_or_other_branch(self):
        sibling_unit = SourceUnit(
            title="Project Beta",
            lines=["Category: Projects/Project Alpha/Testing", "body"],
            category_path=("Projects", "Project Beta"),
            heading_path=("Projects", "Project Beta"),
            start_line=1,
        )
        sibling_result = classify_unit(sibling_unit, self.tree)
        self.assertTrue(sibling_result["needs_review"])
        self.assertIn("conflicts with heading category", sibling_result["issues"][0])

        cross_root_unit = SourceUnit(
            title="Markdown Editor",
            lines=["Category: Knowledge/Analysis/Qualitative", "body"],
            category_path=("Tool", "Markdown Editor"),
            heading_path=("Tool", "Markdown Editor"),
            start_line=1,
        )
        prev_entry = {"manual_heading_path": ["Knowledge", "Analysis"]}
        cross_result = classify_unit(cross_root_unit, self.tree, previous_entry=prev_entry)
        self.assertTrue(cross_result["needs_review"])
        self.assertIn("conflicts with heading category", cross_result["issues"][0])

    def test_long_yaml_frontmatter_and_merged_category_lines_are_extracted_cleanly(self):
        unit = SourceUnit(
            title="Note",
            lines=[
                "---",
                "title: Test",
                "author: A",
                "tags: [one, two]",
                "status: draft",
                "created: 2026-08-14",
                "---",
                "Category: Projects/Project Alpha/Testing",
                "first section",
                "#### Merged section",
                "Category: Knowledge/Analysis/Qualitative",
                "second section",
            ],
            category_path=(),
            heading_path=(),
            start_line=1,
        )
        result = classify_unit(unit, self.tree)
        self.assertFalse(result["needs_review"])
        self.assertEqual(
            set(result["paths"]),
            {
                ("Projects", "Project Alpha", "Testing"),
                ("Knowledge", "Analysis", "Qualitative"),
            },
        )
        self.assertNotIn("Category:", result["content"])

    def test_consolidation_and_relocation_clean_up_manual_category_lines(self):
        original_reloc = (
            "# Knowledge\n"
            "## Analysis\n"
            "<!-- research-notes-source-id: aaaabbbbccccdddd -->\n"
            "Category: Knowledge/Analysis/Qualitative\n"
            "### Qualitative\n"
            "body\n"
        )
        updated_reloc, *_ = render_source_id_markers(
            original_reloc,
            [(5, "aaaabbbbccccdddd")],
            relocations=[(3, 5, "aaaabbbbccccdddd")],
        )
        self.assertNotIn("Category:", updated_reloc)
        self.assertIn("### Qualitative\n<!-- research-notes-source-id: aaaabbbbccccdddd -->\nbody", updated_reloc)

        original_consol = (
            "# Projects\n"
            "## Project Alpha\n"
            "### Testing\n"
            "<!-- research-notes-source-id: 1111222233334444 -->\n"
            "Category: Projects/Project Alpha/Testing\n"
            "first\n"
            "#### Sub\n"
            "<!-- research-notes-source-id: 5555666677778888 -->\n"
            "Category: Knowledge/Analysis/Qualitative\n"
            "second\n"
        )
        updated_consol, *_ = render_source_id_markers(
            original_consol,
            [(3, "1111222233334444")],
            consolidations=[(3, "1111222233334444", [(4, "1111222233334444"), (8, "5555666677778888")])],
        )
        self.assertEqual(updated_consol.count("Category:"), 1)
        self.assertIn(
            "### Testing\n"
            "<!-- research-notes-source-id: 1111222233334444 -->\n"
            "Category: Projects/Project Alpha/Testing, Knowledge/Analysis/Qualitative\n",
            updated_consol,
        )

    def test_document_root_marker_is_inserted_after_yaml_frontmatter(self):
        original = (
            "---\r\n"
            "tags: [test]\r\n"
            "---\r\n"
            "Category: Knowledge/Analysis/Qualitative\r\n"
            "root content\r\n"
        )
        updated, inserted, headings, roots, normalized = render_source_id_markers(
            original,
            [(1, "aaaabbbbccccdddd")],
        )
        self.assertEqual(inserted, 1)
        self.assertEqual(headings, 0)
        self.assertEqual(roots, 1)
        self.assertEqual(normalized, 0)
        self.assertIn(
            "---\r\n<!-- research-notes-source-id: aaaabbbbccccdddd -->\r\nCategory:",
            updated,
        )
        self.assertTrue(updated.endswith("\r\n"))

    def test_marker_is_inserted_after_existing_blank_lines(self):
        original = "# Knowledge\n\nbody\n"
        updated, inserted, headings, roots, normalized = render_source_id_markers(
            original,
            [(1, "aaaabbbbccccdddd")],
        )
        self.assertEqual((inserted, headings, roots), (1, 1, 0))
        self.assertEqual(normalized, 0)
        self.assertEqual(
            updated,
            "# Knowledge\n\n<!-- research-notes-source-id: aaaabbbbccccdddd -->\nbody\n",
        )

    def test_prefixed_source_id_comment_is_normalized_without_changing_id(self):
        original = "# Knowledge\n<!-- legacy-tool-source-id: ABCDEF1234567890 -->\nbody\n"

        updated, inserted, headings, roots, normalized = render_source_id_markers(
            original,
            [],
        )

        self.assertEqual((inserted, headings, roots, normalized), (0, 0, 0, 1))
        self.assertIn(
            "<!-- research-notes-source-id: abcdef1234567890 -->",
            updated,
        )

    def test_quoted_source_id_comment_is_ignored_and_preserved(self):
        quoted_id = "aaaabbbbccccdddd"
        original = (
            "# Knowledge\n"
            f"> <!-- research-notes-source-id: {quoted_id} -->\n"
            "body\n"
        )

        updated, inserted, headings, roots, normalized = render_source_id_markers(
            original,
            [(1, "1111222233334444")],
        )

        self.assertEqual((inserted, headings, roots, normalized), (1, 1, 0, 0))
        self.assertIn(f"> <!-- research-notes-source-id: {quoted_id} -->", updated)
        self.assertIn("<!-- research-notes-source-id: 1111222233334444 -->", updated)

    def test_quoted_source_id_comment_does_not_define_unit_id(self):
        unit = SourceUnit(
            title="Quoted marker",
            lines=[
                "## Quoted marker",
                "> <!-- research-notes-source-id: aaaabbbbccccdddd -->",
                "body",
            ],
            category_path=("Knowledge",),
            heading_path=("Knowledge", "Quoted marker"),
            start_line=2,
        )

        source_id = resolve_source_id(
            "Daily/test.md",
            unit,
            unit.content,
            ({}, {}),
            ({}, {}),
            set(),
            allow_legacy_heading=False,
        )

        self.assertNotEqual(source_id, "aaaabbbbccccdddd")

    def test_heading_correction_moves_empty_parent_source_id_to_child(self):
        daily_dir = os.path.join(self.temp_dir.name, "Daily_Logs")
        os.makedirs(daily_dir)
        source_path = os.path.join(daily_dir, "260922.md")
        source_id = "aaaabbbbccccdddd"
        with open(source_path, "w", encoding="utf-8") as file_obj:
            file_obj.write(
                "# Knowledge\n"
                "## Analysis\n"
                f"<!-- research-notes-source-id: {source_id} -->\n"
                "### Qualitative\n"
                "body\n"
            )

        indexes = build_id_indexes({"entries": {}})
        plans, existing = build_source_marker_plans(
            daily_dir,
            self.temp_dir.name,
            self.tree,
            indexes,
            indexes,
            allow_legacy_heading=False,
        )

        self.assertEqual(existing, 1)
        self.assertEqual(plans[0].relocations, [(3, 4, source_id)])
        persist_source_marker_plans(
            plans, os.path.join(self.temp_dir.name, "backups"), self.temp_dir.name
        )
        with open(source_path, "r", encoding="utf-8") as file_obj:
            updated = file_obj.read()
        self.assertNotIn(
            f"## Analysis\n<!-- research-notes-source-id: {source_id}", updated
        )
        self.assertIn(
            f"### Qualitative\n<!-- research-notes-source-id: {source_id}", updated
        )

    def test_persisted_marker_survives_heading_rename(self):
        daily_dir = os.path.join(self.temp_dir.name, "Daily_Logs")
        os.makedirs(daily_dir)
        log_path = os.path.join(daily_dir, "260814 일지.md")
        with open(log_path, "w", encoding="utf-8") as file_obj:
            file_obj.write("# Knowledge\n## First test\nqualitative analysis result\n")
        metadata = {"entries": {}}
        indexes = build_id_indexes(metadata)
        plans, existing = build_source_marker_plans(
            daily_dir,
            self.temp_dir.name,
            self.tree,
            indexes,
            indexes,
            allow_legacy_heading=False,
        )
        self.assertEqual(existing, 0)
        inserted, _headings, _roots, normalized, backup_root = persist_source_marker_plans(
            plans,
            os.path.join(self.temp_dir.name, "backups"),
            self.temp_dir.name,
        )
        self.assertGreaterEqual(inserted, 1)
        self.assertEqual(normalized, 0)
        self.assertTrue(os.path.isdir(backup_root))

        _log_name, units = parse_markdown_into_units(log_path, self.tree)
        first_unit = [unit for unit in units if unit.title == "First test"][0]
        original_id = SOURCE_ID_RE.search(first_unit.content).group(1).lower()
        self.assertNotIn(
            "research-notes-source-id",
            classify_unit(first_unit, self.tree)["content"],
        )
        self.assertEqual(len(source_unit_fingerprint(first_unit)), 64)
        with open(log_path, "r", encoding="utf-8") as file_obj:
            renamed = file_obj.read().replace("## First test", "## Renamed test")
        with open(log_path, "w", encoding="utf-8") as file_obj:
            file_obj.write(renamed)

        _log_name, renamed_units = parse_markdown_into_units(log_path, self.tree)
        renamed_unit = [unit for unit in renamed_units if unit.title == "Renamed test"][0]
        resolved = resolve_source_id(
            "260814 일지.md",
            renamed_unit,
            renamed_unit.content,
            indexes,
            indexes,
            set(),
            allow_legacy_heading=False,
        )
        self.assertEqual(resolved, original_id)

    def test_new_unit_does_not_reuse_heading_only_legacy_id(self):
        unit = SourceUnit(
            title="Repeated heading",
            lines=["## Repeated heading", "new content"],
            category_path=("Knowledge",),
            heading_path=("Knowledge", "Repeated heading"),
            start_line=10,
        )
        legacy_id = "1111222233334444"
        legacy_metadata = {
            "entries": {
                legacy_id: {
                    "source_path": "Daily_Logs/test.md",
                    "source_heading": "Repeated heading",
                    "source_anchor": "different-anchor",
                }
            }
        }
        indexes = build_id_indexes(legacy_metadata)
        resolved = resolve_source_id(
            "Daily_Logs/test.md",
            unit,
            unit.content,
            indexes,
            indexes,
            set(),
            allow_legacy_heading=False,
        )
        self.assertNotEqual(resolved, legacy_id)
        self.assertEqual(
            resolved,
            hashlib.sha1(
                make_source_anchor("Daily_Logs/test.md", unit).encode("utf-8")
            ).hexdigest()[:16],
        )

    def test_duplicate_permanent_source_id_is_rejected(self):
        unit_a = SourceUnit(
            title="A",
            lines=["## A", "<!-- research-notes-source-id: abcdef1234567890 -->", "a"],
            category_path=("Knowledge",),
            heading_path=("Knowledge", "A"),
            start_line=1,
        )
        unit_b = SourceUnit(
            title="B",
            lines=["## B", "<!-- research-notes-source-id: abcdef1234567890 -->", "b"],
            category_path=("Knowledge",),
            heading_path=("Knowledge", "B"),
            start_line=5,
        )
        used_ids = set()
        indexes = ({}, {})
        resolve_source_id("Daily/test.md", unit_a, unit_a.content, indexes, indexes, used_ids)
        with self.assertRaisesRegex(RuntimeError, "Duplicate permanent Source ID"):
            resolve_source_id(
                "Daily/test.md",
                unit_b,
                unit_b.content,
                indexes,
                indexes,
                used_ids,
            )

    def test_rebuild_source_id_markers_backs_up_and_removes_duplicates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            daily_logs_dir = os.path.join(temp_dir, "Daily_Logs")
            backup_root = os.path.join(temp_dir, "scripts", "source_id_backups")
            os.makedirs(daily_logs_dir)
            source_path = os.path.join(daily_logs_dir, "note.md")
            original = (
                "## First\n"
                "<!-- research-notes-source-id: abcdef1234567890 -->\n"
                "one\n\n"
                "## Second\n"
                "<!-- research-notes-source-id: abcdef1234567890 -->\n"
                "two\n"
            )
            with open(source_path, "w", encoding="utf-8", newline="") as file_obj:
                file_obj.write(original)

            changed, backup_path = rebuild_source_id_markers(
                daily_logs_dir,
                temp_dir,
                backup_root,
            )

            with open(source_path, "r", encoding="utf-8", newline="") as file_obj:
                self.assertNotIn("research-notes-source-id", file_obj.read())
            with open(os.path.join(backup_path, "Daily_Logs", "note.md"), "r", encoding="utf-8") as file_obj:
                self.assertEqual(file_obj.read(), original)
            self.assertEqual(changed, 1)

    def test_multi_file_conflict_rolls_back_already_persisted_file(self):
        first_path = self.write_log("# Knowledge\nfirst\n", name="first.md")
        second_path = self.write_log("# Knowledge\nsecond\n", name="second.md")
        plans = [
            SourceMarkerPlan(
                filepath=first_path,
                source_rel_path="first.md",
                original_content="# Knowledge\nfirst\n",
                markers=[(1, "1111222233334444")],
                heading_count=1,
                document_root_count=0,
            ),
            SourceMarkerPlan(
                filepath=second_path,
                source_rel_path="second.md",
                original_content="# Knowledge\nsecond\n",
                markers=[(1, "aaaabbbbccccdddd")],
                heading_count=1,
                document_root_count=0,
            ),
        ]
        with open(second_path, "w", encoding="utf-8") as file_obj:
            file_obj.write("# Knowledge\nuser changed second\n")

        with self.assertRaisesRegex(RuntimeError, "Failed to persist"):
            persist_source_marker_plans(
                plans,
                os.path.join(self.temp_dir.name, "backups"),
                self.temp_dir.name,
            )

        with open(first_path, "r", encoding="utf-8") as file_obj:
            self.assertEqual(file_obj.read(), "# Knowledge\nfirst\n")
        with open(second_path, "r", encoding="utf-8") as file_obj:
            self.assertEqual(file_obj.read(), "# Knowledge\nuser changed second\n")

    def test_source_id_dry_run_does_not_modify_file(self):
        log_path = self.write_log("# Knowledge\nbody\n")
        plan = SourceMarkerPlan(
            filepath=log_path,
            source_rel_path="260814 일지.md",
            original_content="# Knowledge\nbody\n",
            markers=[(1, "abcdef1234567890")],
            heading_count=1,
            document_root_count=0,
        )
        inserted, headings, roots, normalized, backup = persist_source_marker_plans(
            [plan],
            os.path.join(self.temp_dir.name, "backups"),
            self.temp_dir.name,
            dry_run=True,
        )
        self.assertEqual((inserted, headings, roots), (1, 1, 0))
        self.assertEqual(normalized, 0)
        self.assertIsNone(backup)
        with open(log_path, "r", encoding="utf-8") as file_obj:
            self.assertEqual(file_obj.read(), "# Knowledge\nbody\n")

    def test_hierarchical_review_uses_source_blocks_without_issue_candidates(self):
        records = [
            {
                "source_id": "abcdef1234567890",
                "source_path": "Daily_Logs/2026-08/260814 일지.md",
                "source_log": "260814 일지",
                "source_line": 4,
                "title": "Reporting",
                "category_path": ["Projects", "Project Beta", "Reporting"],
                "generated_path": (
                    "Subject_Hierarchical/Projects/Project Beta/"
                    "Reporting/260814 - Reporting.md"
                ),
                "review_blocks": hierarchical_classifier.extract_review_blocks(
                    "### Reporting\n기능 테스트를 진행했다.\n", "Daily_Logs/2026-08/260814 일지.md",
                    "abcdef1234567890", start_line=4, root_title="Reporting",
                ),
            }
        ]
        paths = write_reviews(self.temp_dir.name, "Reviews", records)
        with open(os.path.join(self.temp_dir.name, paths[0]), "r", encoding="utf-8") as file_obj:
            review = file_obj.read()
        self.assertIn("## 기록", review)
        self.assertIn("기능 테스트를 진행했다.", review)
        self.assertNotIn("확인할 이슈 후보", review)

    def test_generated_links_use_portable_relative_markdown_paths(self):
        link = make_markdown_link(
            "Subject/Projects/Project Alpha/Testing/note.md",
            "Daily_Logs/2026-08/260814 일지.md",
            "260814 일지",
        )

        self.assertEqual(
            link,
            "[260814 일지](../../../../Daily_Logs/2026-08/260814%20%EC%9D%BC%EC%A7%80.md)",
        )
        self.assertNotIn("[[", link)

    def test_review_links_are_portable_relative_markdown_links(self):
        records = [
            {
                "source_id": "abcdef1234567890",
                "source_path": "Daily_Logs/2026-08/260814 일지.md",
                "source_log": "260814 일지",
                "source_line": 4,
                "title": "Reporting",
                "category_path": ["Projects", "Project Alpha", "Reporting"],
                "generated_path": (
                    "Subject/Projects/Project Alpha/Reporting/"
                    "260814 - Reporting.md"
                ),
                "review_blocks": hierarchical_classifier.extract_review_blocks(
                    "### Reporting\n기능 테스트를 진행했다.\n", "Daily_Logs/2026-08/260814 일지.md",
                    "abcdef1234567890", start_line=4, root_title="Reporting",
                ),
            }
        ]

        paths = write_reviews(self.temp_dir.name, "Topic_Reviews", records)
        leaf_path = [path for path in paths if path.endswith("[리뷰] Reporting.md")][0]
        with open(
            os.path.join(self.temp_dir.name, leaf_path),
            encoding="utf-8",
        ) as file_obj:
            review = file_obj.read()

        self.assertIn(
            "[상세 문서](../../../../Subject/Projects/Project%20Alpha/"
            "Reporting/260814%20-%20Reporting.md)",
            review,
        )
        self.assertIn(
            "[원본 일지](../../../../Daily_Logs/2026-08/"
            "260814%20%EC%9D%BC%EC%A7%80.md)",
            review,
        )
        self.assertNotIn("[[", review)

    def test_parent_review_deduplicates_source_id_across_child_categories(self):
        base = {
            "source_id": "same-source-id",
            "source_path": "Daily_Logs/2026-08/260814 일지.md",
            "source_log": "260814 일지",
            "source_line": 4,
            "title": "Result comparison",
            "review_blocks": hierarchical_classifier.extract_review_blocks(
                "### Result comparison\n옵션 A와 옵션 B를 비교했다.",
                "Daily_Logs/2026-08/260814 일지.md", "same-source-id",
                root_title="Result comparison",
            ),
        }
        records = [
            dict(
                base,
                category_path=["Knowledge", "Analysis", "Qualitative"],
                generated_path="Subject_Hierarchical/Knowledge/Analysis/Qualitative/note.md",
            ),
            dict(
                base,
                category_path=["Knowledge", "Analysis", "Quantitative"],
                generated_path="Subject_Hierarchical/Knowledge/Analysis/Quantitative/note.md",
            ),
        ]
        paths = write_reviews(self.temp_dir.name, "Reviews", records)
        parent_path = [path for path in paths if path.endswith("[종합 리뷰] Analysis.md")][0]
        with open(os.path.join(self.temp_dir.name, parent_path), "r", encoding="utf-8") as file_obj:
            parent_review = file_obj.read()
        self.assertIn("**Entries**: 1", parent_review)
        self.assertIn("## 주제 목차", parent_review)
        self.assertEqual(parent_review.count("옵션 A와 옵션 B를 비교했다."), 1)
        self.assertNotIn("앞서 표시한 기록", parent_review)

    def test_isolated_classifier_review_regeneration_preserves_sources_and_manual_file(self):
        root = self.temp_dir.name
        script_dir = os.path.join(root, "scripts")
        daily_dir = os.path.join(root, "Daily_Logs", "2026-08")
        os.makedirs(script_dir)
        os.makedirs(daily_dir)
        source = os.path.join(daily_dir, "260814 일지.md")
        original = "# Projects\n## Project Alpha\n### Testing\n결과를 기록했다.\n"
        with open(source, "w", encoding="utf-8") as file_obj:
            file_obj.write(original)
        manual = os.path.join(root, "Topic_Reviews", "manual.md")
        os.makedirs(os.path.dirname(manual))
        with open(manual, "w", encoding="utf-8") as file_obj:
            file_obj.write("manual document\n")
        fake_module = os.path.join(script_dir, "hierarchical_classifier.py")
        args = ["--production", "--output-dir", "Subject", "--review-dir",
                "Topic_Reviews", "--metadata-file", "organizer_metadata.json",
                "--no-persist-source-ids"]

        def generated_snapshot():
            results = {}
            for folder in ("Subject", "Topic_Reviews"):
                for base, _dirs, files in os.walk(os.path.join(root, folder)):
                    for filename in files:
                        path = os.path.join(base, filename)
                        with open(path, "rb") as file_obj:
                            results[os.path.relpath(path, root)] = file_obj.read()
            return results

        with patch.object(hierarchical_classifier, "__file__", fake_module):
            self.assertEqual(hierarchical_classifier.main(args), 0)
            first = generated_snapshot()
            self.assertEqual(hierarchical_classifier.main(args), 0)
            self.assertEqual(first, generated_snapshot())
        with open(source, encoding="utf-8") as file_obj:
            self.assertEqual(file_obj.read(), original)
        with open(manual, encoding="utf-8") as file_obj:
            self.assertEqual(file_obj.read(), "manual document\n")
        self.assertTrue(any("[리뷰] Testing.md" in path for path in first))

    def test_review_output_is_identical_when_sources_do_not_change(self):
        records = [
            {
                "source_id": "stable-source-id",
                "source_path": "Daily_Logs/2026-08/260814 일지.md",
                "source_log": "260814 일지",
                "source_line": 4,
                "title": "Qualitative test",
                "category_path": ["Knowledge", "Analysis", "Qualitative"],
                "generated_path": "Subject_Hierarchical/Knowledge/Analysis/Qualitative/note.md",
                "review_blocks": hierarchical_classifier.extract_review_blocks(
                    "### Qualitative test\n정성 분석 결과를 기록했다.",
                    "Daily_Logs/2026-08/260814 일지.md", "stable-source-id",
                    root_title="Qualitative test",
                ),
            }
        ]

        paths = write_reviews(self.temp_dir.name, "Reviews", records)
        first = {}
        for rel_path in paths:
            with open(
                os.path.join(self.temp_dir.name, rel_path),
                "rb",
            ) as file_obj:
                first[rel_path] = file_obj.read()

        second_paths = write_reviews(self.temp_dir.name, "Reviews", records)
        second = {}
        for rel_path in second_paths:
            with open(
                os.path.join(self.temp_dir.name, rel_path),
                "rb",
            ) as file_obj:
                second[rel_path] = file_obj.read()

        self.assertEqual(paths, second_paths)
        self.assertEqual(first, second)
        self.assertTrue(all(b"**Generated**" not in content for content in second.values()))

    def test_backup_retention_is_off_by_default_and_prunes_oldest_when_enabled(self):
        import json

        with tempfile.TemporaryDirectory() as temp_dir:
            script_dir = os.path.join(temp_dir, "scripts")
            runtime_dir = os.path.join(script_dir, ".runtime")
            auto_backup_dir = os.path.join(script_dir, "source_id_backups", "automatic")
            os.makedirs(runtime_dir)
            os.makedirs(auto_backup_dir)
            for idx in range(5):
                batch = os.path.join(auto_backup_dir, f"2026090{idx}_000000_000000")
                os.makedirs(batch)
                with open(os.path.join(batch, "note.md"), "w", encoding="utf-8") as f:
                    f.write("backup")

            self.assertEqual(hierarchical_classifier.resolve_backup_retention_limit(script_dir=script_dir), 0)
            with open(os.path.join(runtime_dir, "tray_settings.json"), "w", encoding="utf-8") as f:
                json.dump({"prune_backups": True, "backup_retention_count": 3}, f)
            limit = hierarchical_classifier.resolve_backup_retention_limit(script_dir=script_dir)
            self.assertEqual(limit, 3)

            removed = hierarchical_classifier.prune_backup_batches(auto_backup_dir, limit)
            self.assertEqual(removed, 2)
            remaining = sorted(os.listdir(auto_backup_dir))
            self.assertEqual(
                remaining,
                [
                    "20260902_000000_000000",
                    "20260903_000000_000000",
                    "20260904_000000_000000",
                ],
            )


if __name__ == "__main__":
    unittest.main()
