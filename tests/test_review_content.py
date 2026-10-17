import os
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from review_content import extract_review_blocks, group_review_blocks, select_review_groups
from review_renderer import write_reviews


SOURCE = "Daily_Logs/2026-09/260909 일지.md"


def blocks(text, source_id="abcdef1234567890"):
    return extract_review_blocks(text, SOURCE, source_id, start_line=10, root_title="Sample Module")


class ReviewContentTests(unittest.TestCase):
    def test_fenced_and_quoted_headings_do_not_split_sections(self):
        content = "### Sample Module\n```sml\n#### false heading\n![[not-an-image.png]]\n```\n> [!NOTE]\n> #### quoted heading\n> - value\n#### Result\nvalue\n"
        result = blocks(content)
        self.assertEqual([block.kind for block in result], ["code", "quote", "heading", "paragraph"])
        self.assertEqual(result[0].language, "sml")
        self.assertEqual(result[0].images, ())
        self.assertEqual(result[1].heading_path, ())
        self.assertEqual(result[-1].heading_path, ("Result",))
        self.assertEqual((result[-1].start_line, result[-1].end_line), (19, 19))

    def test_nested_list_and_table_remain_complete(self):
        content = "### Sample Module\n권장 설정:\n- Option A: 50\n\t- Option B: 25\n\n| 이름 | 값 |\n| --- | --- |\n| Option A | 50 |"
        result = blocks(content)
        self.assertEqual([block.kind for block in result], ["paragraph", "list", "table"])
        grouped = group_review_blocks(result)
        self.assertEqual(grouped[0].text, "권장 설정:\n\n- Option A: 50\n\t- Option B: 25")
        self.assertIn("| --- | --- |", grouped[1].text)

    def test_image_and_following_caption_select_nearest_image(self):
        content = "### System Check\n![[first.png]]\n\n![[second.png]]\nv1.0 -> v1.1"
        selected, omitted = select_review_groups(blocks(content), 1200, 1)
        self.assertTrue(omitted)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].images, ("second.png",))
        self.assertIn("v1.0 -> v1.1", selected[0].text)

    def test_late_heading_gets_body_after_long_code(self):
        content = "### Sample Module\n시작\n\n" + "```sml\n" + "A" * 2500 + "\n```\n\n#### 1차 완료\n```sml\n" + "B" * 2500 + "\n```\nOption A: 50\n\nstep 1 -> 30, step 2 -> 31"
        result = blocks(content)
        selected, omitted = select_review_groups(result, 1200, 1)
        shown = "\n".join(group.text for group in selected)
        self.assertTrue(omitted)
        self.assertIn("step 1 -> 30, step 2 -> 31", shown)
        self.assertNotIn("A" * 100, shown)

    def test_source_marker_is_metadata_not_body(self):
        result = blocks("### Sample Module\n<!-- research-notes-source-id: abcdef1234567890 -->\n내용")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].text, "내용")
        self.assertEqual(result[0].start_line, 12)

    def test_leading_blank_keeps_original_line_numbers(self):
        result = blocks("\n### Sample Module\n내용")
        self.assertEqual(result[0].start_line, 12)
        self.assertEqual(result[0].text, "내용")

    def test_quoted_code_does_not_rewrite_fake_image(self):
        with tempfile.TemporaryDirectory() as temp:
            record = {
                "source_id": "abcdef1234567890", "source_path": SOURCE,
                "source_log": "260909 일지", "source_line": 1,
                "title": "Quote", "category_path": ["Projects", "Quote"],
                "generated_path": "Subject/Projects/Quote/note.md",
                "review_blocks": blocks("### Quote\n> [!NOTE]\n> ```\n> ![[fake.png]]\n> ```\n> Actual text"),
            }
            paths = write_reviews(temp, "Reviews", [record])
            review = (Path(temp) / next(path for path in paths if "[리뷰]" in path)).read_text(encoding="utf-8")
            self.assertIn("> ![[fake.png]]", review)
            self.assertIn("> Actual text", review)

    def test_review_rebases_images_and_links_for_korean_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / SOURCE
            source.parent.mkdir(parents=True)
            source.write_text("source", encoding="utf-8")
            attachment = root / "attachments" / "공백 이미지.png"
            attachment.parent.mkdir()
            attachment.write_bytes(b"image")
            record = {
                "source_id": "abcdef1234567890", "source_path": SOURCE,
                "source_log": "260909 일지", "source_line": 1,
                "title": "사진", "category_path": ["Projects", "사진"],
                "generated_path": "Subject/Projects/사진/260909 - 사진.md",
                "review_blocks": blocks("### 사진\n![[공백 이미지.png]]\n설명"),
            }
            paths = write_reviews(temp, "Reviews", [record])
            review = (root / next(path for path in paths if "[리뷰]" in path)).read_text(encoding="utf-8")
            self.assertIn("설명", review)
            self.assertIn("%EA%B3%B5%EB%B0%B1%20%EC%9D%B4%EB%AF%B8%EC%A7%80.png", review)
            self.assertIn("260909%20%EC%9D%BC%EC%A7%80.md", review)
            self.assertNotIn("![[", review)

    def test_review_rebases_markdown_and_wikilinks_with_fragments(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / SOURCE
            source.parent.mkdir(parents=True)
            source.write_text("source", encoding="utf-8")
            target = source.parent / "상세 기록.md"
            target.write_text("# 결과", encoding="utf-8")
            record = {
                "source_id": "abcdef1234567890", "source_path": SOURCE,
                "source_log": "260909 일지", "source_line": 1,
                "title": "참조", "category_path": ["Projects", "참조"],
                "generated_path": "Subject/Projects/참조/260909 - 참조.md",
                "review_blocks": blocks("### 참조\n[표](상세 기록.md#결과)와 [[상세 기록#결과|결과 문서]]"),
            }
            paths = write_reviews(temp, "Reviews", [record])
            review = (root / next(path for path in paths if "[리뷰]" in path)).read_text(encoding="utf-8")
            self.assertEqual(review.count("%EC%83%81%EC%84%B8%20%EA%B8%B0%EB%A1%9D.md#%EA%B2%B0%EA%B3%BC"), 2)
            self.assertNotIn("[[상세", review)

    def test_aggregate_empty_record_does_not_take_three_body_slots(self):
        with tempfile.TemporaryDirectory() as temp:
            records = []
            for n in range(5):
                records.append({
                    "source_id": f"abcd{n:012x}", "source_path": SOURCE,
                    "source_log": f"26090{n + 1} 일지", "source_line": n + 1,
                    "title": "Leaf", "category_path": ["Projects", "Leaf"],
                    "generated_path": f"Subject/Projects/Leaf/{n}.md",
                    "review_blocks": blocks("### Leaf\n" + (f"body {n}" if n != 4 else "")),
                })
            paths = write_reviews(temp, "Reviews", records)
            aggregate = next(Path(temp, path) for path in paths if "[종합 리뷰]" in path)
            content = aggregate.read_text(encoding="utf-8")
            self.assertIn("## 주제 목차", content)
            self.assertNotIn("body 3", content)
            self.assertNotIn("body 2", content)
            self.assertNotIn("body 1", content)

    def test_atomic_multi_image_and_quoted_code_budget(self):
        content = "### Sample Module\n- ![[one.png]] and ![[two.png]]\n\n> ```\n> ![[fake.png]]\n> ```\n> note"
        parsed = blocks(content)
        self.assertEqual(parsed[0].images, ("one.png", "two.png"))
        self.assertEqual(parsed[1].images, ())
        selected, omitted = select_review_groups(parsed, 800, 1)
        self.assertTrue(omitted)
        self.assertEqual(sum(len(group.images) for group in selected), 0)
        self.assertNotIn("one.png", "\n".join(group.text for group in selected))


if __name__ == "__main__":
    unittest.main()
