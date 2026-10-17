"""Golden Markdown for the review refactor; update only for an intentional output change."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from review_content import extract_review_blocks
from review_renderer import write_reviews


GOLDEN = Path(__file__).parent / "fixtures" / "reviews" / "expected.json"


def _record(number, category, body, title=None, source_id=None):
    title = title or f"Record {number:02d}"
    source_path = f"Daily_Logs/2026-09/26.09.{number % 28 + 1:02d} 일지.md"
    source_id = source_id or f"{number:016x}"
    return {
        "source_id": source_id,
        "source_path": source_path,
        "source_line": number + 1,
        "source_log": Path(source_path).stem,
        "title": title,
        "category_path": category,
        "generated_path": f"Subject/{'/'.join(category)}/{number}.md",
        "review_blocks": extract_review_blocks(
            f"# {title}\n{body}", source_path, source_id, root_title=title
        ),
    }


def review_fixture(vault):
    records = [_record(number, ["Atlas", "Overview", f"Topic{number:02d}"],
                       f"topic body {number}") for number in range(35)]
    records.extend(_record(100 + number, ["Atlas", "Leaf"], f"leaf body {number}")
                   for number in range(7))
    records.extend(_record(200 + number, ["Unclassified", "Missing Level 1"],
                           f"unclassified body {number}") for number in range(3))
    records.append(_record(301, ["Atlas"], "direct L1 body"))
    records.append(_record(302, ["Atlas", "Overview", "Topic00", "Detail"],
                           "detail child body"))
    records.append(_record(303, ["Atlas", "Overview", "Topic00"],
                           "중복 기록", source_id="3030303030303030"))
    records.append(_record(303, ["Atlas", "Overview", "Topic01"],
                           "중복 기록", source_id="3030303030303030"))
    records.append(_record(304, ["Atlas", "Leaf"],
                           "```\n" + "x" * 900 + "\n```"))
    records.append(_record(305, ["Atlas", "Leaf"],
                           "![[공백 이미지.png]]\n짧은 캡션\n\n[표](상세 기록.md#결과)"))
    records.append(_record(307, ["Atlas", "Leaf"],
                           "설정:\n- 첫 항목\n- 둘째 항목\n\n| 항목 | 값 |\n| --- | --- |\n"
                           "| 모드 | 정상 |\n\n> [!NOTE]\n> 인용 설명"))
    records.append(_record(306, ["Atlas", "Leaf"],
                           "very long title body", title="길다" * 4300))

    for record in records:
        source = vault / record["source_path"]
        source.parent.mkdir(parents=True, exist_ok=True)
        source.touch()
        (vault / record["generated_path"]).parent.mkdir(parents=True, exist_ok=True)
    attachments = vault / "attachments"
    attachments.mkdir()
    (attachments / "공백 이미지.png").write_bytes(b"synthetic image")
    (vault / "Daily_Logs" / "2026-09" / "상세 기록.md").write_text(
        "# 결과\n", encoding="utf-8"
    )
    return records


def capture_review_output():
    with tempfile.TemporaryDirectory() as temp:
        vault = Path(temp)
        paths = write_reviews(temp, "Topic_Reviews", review_fixture(vault))
        selected = (
            "[종합 리뷰] Atlas.md", "[종합 리뷰] Overview.md",
            "[리뷰] Leaf.md", "[종합 리뷰] Topic00.md",
            "[리뷰] Detail.md", "[리뷰] Missing Level 1.md",
        )
        output = {path: (vault / path).read_text(encoding="utf-8") for path in paths
                  if Path(path).name in selected}
        return paths, output


class ReviewGoldenTests(unittest.TestCase):
    def test_exact_review_markdown_and_paths(self):
        expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
        paths, output = capture_review_output()
        self.assertEqual(paths, expected["paths"])
        self.assertEqual(output, expected["documents"])

    def test_render_error_does_not_write_partial_reviews(self):
        with tempfile.TemporaryDirectory() as temp:
            records = [
                _record(1, ["Atlas", "A"], "ordinary body"),
                _record(2, ["Atlas", "B"], "![[missing.png]]"),
            ]
            with self.assertRaisesRegex(RuntimeError, "Review image was not found"):
                write_reviews(temp, "Topic_Reviews", records)
            self.assertFalse((Path(temp) / "Topic_Reviews").exists())


if __name__ == "__main__":
    unittest.main()
