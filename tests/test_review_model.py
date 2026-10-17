import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from review_model import build_review_model


class ReviewModelTests(unittest.TestCase):
    def test_duplicate_categories_and_existing_lists_leave_input_unchanged(self):
        base = {
            "source_id": "abcdef1234567890", "source_path": "Daily_Logs/260901.md",
            "source_line": 3, "source_log": "260901 일지", "title": "Shared",
            "subject_documents": [("Subject/old.md", ("A",), "Old")],
            "matched_categories": [("A",)],
        }
        first = dict(base, category_path=["A", "B"], generated_path="Subject/A/B/one.md")
        second = dict(base, category_path=["A", "C"], generated_path="Subject/A/C/two.md")
        model = build_review_model([first, second], "Reviews")
        parent = model.groups[("A",)]
        self.assertEqual(len(parent), 1)
        self.assertEqual(len(parent[0]["subject_documents"]), 3)
        self.assertEqual(parent[0]["matched_categories"], [("A",), ("A", "B"), ("A", "C")])
        self.assertEqual(first["subject_documents"], [("Subject/old.md", ("A",), "Old")])
        self.assertEqual(second["matched_categories"], [("A",)])
        self.assertEqual(model.children[("A",)], [("A", "B"), ("A", "C")])
        self.assertEqual(model.review_paths[("A",)], "Reviews/A/[종합 리뷰] A.md")


if __name__ == "__main__":
    unittest.main()
