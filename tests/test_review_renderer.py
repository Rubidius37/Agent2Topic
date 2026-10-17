import sys
import tempfile
import unittest
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from review_content import extract_review_blocks
from review_renderer import review_policy, write_reviews


def record(number, category, body="text", title=None, source_id=True):
    name = title or f"entry {number}"
    path = f"Daily_Logs/2026-09/2026-09-{number % 28 + 1:02d}.md"
    identifier = f"{number:016x}" if source_id else None
    return {
        "source_id": identifier, "source_path": path, "source_line": number + 1,
        "source_log": f"26.09.{number % 28 + 1:02d} 일지", "title": name,
        "category_path": category, "generated_path": f"Subject/{'/'.join(category)}/{number}.md",
        "review_blocks": extract_review_blocks(f"# {name}\n{body}", path, identifier or "", root_title=name),
    }


def rendered(root, records):
    paths = write_reviews(root, "Reviews", records)
    return {Path(path).name: (Path(root) / path).read_text(encoding="utf-8") for path in paths}


class ReviewRendererTests(unittest.TestCase):
    def test_undated_file_modification_does_not_change_recent_five(self):
        records = [record(day, ["A", "B"], f"dated-body-{day:02d}") for day in range(1, 7)]
        undated = record(20, ["A", "B"], "undated-body")
        undated["source_path"] = "Daily_Logs/undated.md"
        undated["source_log"] = "undated"
        records.append(undated)
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / undated["source_path"]
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("# A\n", encoding="utf-8")
            os.utime(source, (2_000_000_000, 2_000_000_000))
            overview = rendered(root, records)["[리뷰] B.md"]
            self.assertNotIn("undated-body", overview)
            self.assertNotIn("dated-body-01", overview)
            for day in range(2, 7):
                self.assertIn(f"dated-body-{day:02d}", overview)

    def test_overview_selects_latest_five_by_dotted_date(self):
        dates = (10, 1, 8, 3, 7, 2, 9)
        records = []
        for number, day in enumerate(dates):
            item = record(number, ["A", "B"], f"dated-body-{day:02d}")
            item["source_path"] = f"Daily_Logs/2026-09/26.09.{day:02d} 일지.md"
            item["source_log"] = "표시용 이름"
            records.append(item)
        with tempfile.TemporaryDirectory() as root:
            overview = rendered(root, records)["[리뷰] B.md"]
            self.assertIn("2026-09-01 ~ 2026-09-10", overview)
            for day in (10, 9, 8, 7, 3):
                self.assertIn(f"dated-body-{day:02d}", overview)
            for day in (1, 2):
                self.assertNotIn(f"dated-body-{day:02d}", overview)

    def test_policy_by_depth_and_unclassified_root(self):
        self.assertEqual([review_policy(path).mode for path in [
            ("A",), ("A", "B"), ("A", "B", "C"),
            ("Unclassified", "Missing Level 1"), ("A", "Unclassified")]],
            ["navigation", "overview", "detail", "triage", "overview"])

    def test_l1_navigation_l2_global_five_and_detail_with_child(self):
        records = [record(n, ["A", "B", f"C{n % 8}", "D"], f"body-token-{n}") for n in range(12)]
        with tempfile.TemporaryDirectory() as root:
            output = rendered(root, records)
            l1 = output["[종합 리뷰] A.md"]
            l2 = output["[종합 리뷰] B.md"]
            l3 = output["[종합 리뷰] C0.md"]
            self.assertNotIn("body-token", l1)
            self.assertEqual(l2.count("body-token-"), 5)
            self.assertIn("body-token-8", l3)
            self.assertLessEqual(len(l1), 8000)
            self.assertLessEqual(len(l2), 12000)
            self.assertLessEqual(len(l3), 120000)

    def test_empty_selection_does_not_take_slot_and_fallback_links(self):
        records = [record(1, ["A", "B"], "readable earlier")]
        records += [record(n, ["A", "B"], "```\n" + "x" * 900 + "\n```") for n in range(2, 5)]
        with tempfile.TemporaryDirectory() as root:
            l2 = rendered(root, records)["[리뷰] B.md"]
            self.assertIn("readable earlier", l2)
            self.assertEqual(l2.count("#### "), 1)
            empty = rendered(root, records[1:])["[리뷰] B.md"]
            self.assertIn("표시 가능한 발췌가 없습니다", empty)
            self.assertNotIn("#### ", empty)

    def test_triage_and_many_empty_entries_remain_bounded(self):
        records = [record(n, ["Unclassified", "Missing Level 1"], "") for n in range(99)]
        with tempfile.TemporaryDirectory() as root:
            output = rendered(root, records)
            leaf = output["[리뷰] Missing Level 1.md"]
            self.assertIn("Level 1 분류가 필요한 기록", leaf)
            self.assertEqual(leaf.count("[원본 일지]"), 30)
            self.assertIn("미표시 기록 69건", leaf)
            self.assertLessEqual(len(leaf), 8000)
            many = [record(n, ["A", "B", "C"], "") for n in range(1500)]
            output = rendered(root, many)
            self.assertLessEqual(len(output["[리뷰] C.md"]), 120000)
            self.assertLessEqual(len(output["[종합 리뷰] B.md"]), 12000)

    def test_determinism_dry_run_and_missing_id(self):
        records = [record(1, ["A", "B"], source_id=False)]
        records[0]["source_path"] = "Daily_Logs/undated.md"
        records[0]["source_log"] = "undated"
        with tempfile.TemporaryDirectory() as root:
            first = rendered(root, records)
            self.assertIn("날짜 없음", first["[리뷰] B.md"])
            self.assertEqual(first, rendered(root, records))
            self.assertEqual(len(write_reviews(root, "Other", records, dry_run=True)), 2)
            self.assertFalse((Path(root) / "Other").exists())

    def test_direct_l1_subject_link_and_extreme_card(self):
        records = [record(1, ["A"], "body", title="direct")]
        records.append(record(2, ["A", "B"], "short body", title="x" * 13000))
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "Subject" / "A").mkdir(parents=True)
            output = rendered(root, records)
            self.assertIn("직접 분류된 기록", output["[종합 리뷰] A.md"])
            self.assertIn("[Subject 폴더]", output["[종합 리뷰] A.md"])
            self.assertLessEqual(len(output["[리뷰] B.md"]), 12000)
            self.assertIn("미표시", output["[리뷰] B.md"])

    def test_omitted_children_show_count_and_review_folder_link(self):
        with tempfile.TemporaryDirectory() as root:
            records = [record(n, ["A", "B", f"C{n:02d}"], "body") for n in range(35)]
            overview = rendered(root, records)["[종합 리뷰] B.md"]
            self.assertEqual(overview.count(" · 기록 1건 · 최근 "), 30)
            self.assertIn("목차에서 생략한 하위 리뷰 5개 · [전체 리뷰 폴더](./)", overview)
            self.assertLessEqual(len(overview), 12000)

            long_names = [record(n, ["A", "B", f"C{n:02d}" + "x" * 45], "body")
                          for n in range(35)]
            overview = rendered(root, long_names)["[종합 리뷰] B.md"]
            listed = overview.count(" · 기록 1건 · 최근 ")
            self.assertLess(listed, 30)
            self.assertIn(f"목차에서 생략한 하위 리뷰 {35 - listed}개 · [전체 리뷰 폴더](./)", overview)
            self.assertLessEqual(len(overview), 12000)

            level_one = [record(n, ["A", f"B{n:02d}"], "body") for n in range(35)]
            navigation = rendered(root, level_one)["[종합 리뷰] A.md"]
            self.assertEqual(navigation.count(" · 기록 1건 · 최근 "), 30)
            self.assertIn("목차에서 생략한 하위 리뷰 5개 · [전체 리뷰 폴더](./)", navigation)

    def test_english_review_rendering_when_language_is_en(self):
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {"LOG2TOPIC_LANGUAGE": "en"}
        ):
            records = [record(1, ["Projects", "Alpha", "Testing"], "test body")]
            output = rendered(root, records)
            self.assertIn("[Summary Review] Projects.md", output)
            self.assertIn("[Summary Review] Alpha.md", output)
            self.assertIn("[Review] Testing.md", output)
            self.assertIn("## Topic Index", output["[Summary Review] Alpha.md"])
            self.assertIn("[Detail Document]", output["[Review] Testing.md"])
            self.assertIn("[Source Log]", output["[Review] Testing.md"])


if __name__ == "__main__":
    unittest.main()
