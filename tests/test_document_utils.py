import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from document_utils import (
    _normalize_image_target,
    extract_frontmatter_date,
    parse_stem_date,
    source_date_key,
)


class SourceDateTests(unittest.TestCase):
    def test_dotted_compact_and_iso_markdown_source_dates(self):
        for value in (
            "26.09.27 일지.md", "26.09.27 일지.MD",
            "Daily_Logs/2026-09/26.09.27 일지.md", "260927 일지.md",
            "2026-09-27-log.md", "2026.09.27 일지.md", "20260927-log.md",
        ):
            with self.subTest(value=value):
                self.assertEqual(source_date_key(value), "2026-09-27")

    def test_unknown_or_invalid_date_sorts_as_undated(self):
        for value in (
            "26.09.27 일지", "260927 일지", "2026-09-27-log",
            "undated.md", "26.02.30 일지.md", "2026-13-01-log.md",
        ):
            with self.subTest(value=value):
                self.assertEqual(source_date_key(value), "0000-00-00")

    def test_parse_stem_date_and_frontmatter_date(self):
        self.assertEqual(parse_stem_date("2026-09-27-log"), ("2026-09-27", "2026-09-27"))
        self.assertEqual(parse_stem_date("26.09.27 일지"), ("2026-09-27", "26.09.27"))
        self.assertEqual(
            extract_frontmatter_date("---\ntitle: Note\ndate: 2026-09-27\n---\nbody"),
            ("2026-09-27", "2026-09-27"),
        )

    def test_normalize_image_target_strips_commonmark_title_and_angle_brackets(self):
        self.assertEqual(
            _normalize_image_target('attachments/img (1).png "Figure 1"'),
            "attachments/img (1).png",
        )
        self.assertEqual(
            _normalize_image_target('<attachments/my image (1).png> "Figure 1"'),
            "attachments/my image (1).png",
        )


if __name__ == "__main__":
    unittest.main()
