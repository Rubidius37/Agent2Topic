"""Build review record and path indexes without changing inputs or writing files."""

from collections import defaultdict
from dataclasses import dataclass
import os

from document_utils import (
    is_prefix,
    normalize_rel_path,
    resolve_document_language,
    sanitize_filename,
    source_date_key,
)


@dataclass(frozen=True)
class ReviewModel:
    groups: dict
    children: dict
    review_paths: dict


def _unique_records(records):
    merged = {}
    for record in records:
        key = record.get("source_id") or f"{record['source_path']}:{record['source_line']}"
        if key not in merged:
            current = dict(record)
            current["subject_documents"] = list(record.get("subject_documents", ()))
            current["matched_categories"] = list(record.get("matched_categories", ()))
            merged[key] = current
        current = merged[key]
        document = (record["generated_path"], tuple(record["category_path"]), record["title"])
        if document not in current["subject_documents"]:
            current["subject_documents"].append(document)
        path = tuple(record["category_path"])
        if path not in current["matched_categories"]:
            current["matched_categories"].append(path)
    return sorted(merged.values(), key=lambda record: (
        source_date_key(record["source_path"]),
        record["source_path"], record["source_line"], record.get("source_id") or "",
    ))


def build_review_model(generated_records, review_dir_name):
    grouped = defaultdict(list)
    for record in generated_records:
        full_path = tuple(record["category_path"])
        for depth in range(1, len(full_path) + 1):
            grouped[full_path[:depth]].append(record)
    groups = {path: _unique_records(records) for path, records in grouped.items()}
    paths = set(groups)
    children = {path: sorted(child for child in paths if len(child) == len(path) + 1
                             and is_prefix(path, child)) for path in paths}
    lang = resolve_document_language()
    summary_prefix = "[Summary Review]" if lang == "en" else "[종합 리뷰]"
    leaf_prefix = "[Review]" if lang == "en" else "[리뷰]"
    review_paths = {
        path: normalize_rel_path(os.path.join(
            review_dir_name, *path,
            f"{summary_prefix if children[path] else leaf_prefix} {sanitize_filename(path[-1])}.md",
        )) for path in paths
    }
    return ReviewModel(groups, children, review_paths)
