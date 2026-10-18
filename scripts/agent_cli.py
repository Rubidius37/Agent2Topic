"""Unified CLI for AI agents (Codex, Claude Code, OpenCode) and human inspection."""

import argparse
from datetime import date
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

from atomic_io import StateFileError, atomic_write_text, filesystem_path
from document_utils import extract_entry_metadata, normalize_rel_path, source_date_key
from hierarchical_classifier import (
    ANY_UNIT_HEADING_RE,
    CATEGORY_LEVELS,
    HEADING_RE,
    SOURCE_ID_RE,
    is_category_header,
    is_markdown_blockquote_line,
    is_table_divider,
    load_metadata,
    normalize_name,
    parse_markdown_into_units,
    parse_rules_from_markdown,
    source_lines_with_code_context,
    source_unit_fingerprint,
    split_markdown_table_row,
)
from process_lock import (
    LOCK_BUSY_EXIT_CODE,
    ProcessLockUnavailable,
    VaultProcessLock,
    command_text,
    print_lock_error,
)
from review_ops import (
    ReviewError,
    apply_manual_categories,
    build_review_items,
    locate_source_unit,
    remove_manual_categories,
    run_production_classifier,
)
from workspace_paths import (
    WORKSPACE_DIRECTORY_NAME,
    resolve_active_workspace_dir,
    resolve_workspace_dir,
    resolve_workspace_state_dir,
)


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_VAULT_DIR = resolve_workspace_dir(SCRIPT_DIR)
PAGENT_VERSION = "0.3.0"
DEFAULT_GITHUB_REPO = "Rubidius37/Project_Agent"

DEFAULT_CONTEXT_MAX_CHARS = 12000
DEFAULT_REVIEW_ENTRY_CHARS = 1200
DEFAULT_ENTRIES_MODE_CHARS = 4000
DEFAULT_SEARCH_ENTRY_CHARS = 600

DEFAULT_RULES_TEMPLATE = """\
# Classification Rules

The table below defines the 5-level hierarchical topic tree. **Keep the 6 columns and column order unchanged.**

## Active Rules

| Level 1 | Level 2 | Level 3 | Level 4 | Level 5 | Keywords |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Projects | Sample Project | Planning | Requirements | | requirement, plan |
| | | Testing | Functional Test | | functional test |
| | | | Regression Test | | regression test |
| Knowledge | Architecture | | | | architecture, design |
| | Debugging | | | | bugfix, root cause |
"""


def split_category_path(raw_path):
    if isinstance(raw_path, (list, tuple)):
        parts = [str(part).strip() for part in raw_path if str(part).strip()]
    else:
        parts = [
            part.strip()
            for part in re.split(r"[/>]", str(raw_path or ""))
            if part.strip()
        ]
    if not 1 <= len(parts) <= CATEGORY_LEVELS:
        raise ValueError(
            f"Category path must have between 1 and {CATEGORY_LEVELS} levels: {raw_path}"
        )
    return tuple(parts)


def sanitize_agent_body_headings(content):
    """Convert H1-H6 headings inside agent body text to bold lines so they never split Topic (H1-H5) or Entry (H6) units."""
    out = []
    for line, in_code in source_lines_with_code_context(content, keepends=True):
        if in_code or is_markdown_blockquote_line(line):
            out.append(line)
            continue
        stripped = line.rstrip("\r\n")
        ending = line[len(stripped):]
        match = ANY_UNIT_HEADING_RE.match(stripped)
        if match:
            title = match.group(2).strip()
            out.append((f"**{title}**" if title else stripped) + ending)
        else:
            out.append(line)
    return "".join(out)


def parse_file_list(raw_files):
    if not raw_files:
        return []
    if isinstance(raw_files, (list, tuple)):
        chunks = []
        for item in raw_files:
            chunks.extend(str(item or "").split(","))
    else:
        chunks = str(raw_files).split(",")
    seen = set()
    result = []
    for chunk in chunks:
        cleaned = normalize_rel_path(chunk.strip().strip("`\"'")).lstrip("./")
        if cleaned and cleaned.casefold() not in seen:
            seen.add(cleaned.casefold())
            result.append(cleaned)
    return result


def detect_git_head_commit(cwd=None):
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=cwd or os.getcwd(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
        if res.returncode == 0:
            sha = res.stdout.strip()
            if re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
                return sha.lower()
    except Exception:
        pass
    return None


def _truncate_text(text, limit, source_id=None):
    raw = str(text or "")
    if limit is None or limit <= 0 or len(raw) <= limit:
        return raw, False
    suffix = (
        f"\n...(truncated; run 'pagent source --id {source_id}' for full text)"
        if source_id
        else "\n...(truncated)"
    )
    keep = max(0, limit - len(suffix))
    return raw[:keep].rstrip() + suffix, True


def ensure_workspace_initialized(vault_dir):
    vault_real = os.path.realpath(vault_dir)
    daily_dir = os.path.join(vault_real, "Daily_Logs")
    attachments_dir = os.path.join(vault_real, "attachments")
    rules_path = os.path.join(vault_real, "Classification_Rules.md")

    created = []
    for folder in (vault_real, daily_dir, attachments_dir):
        if not os.path.isdir(filesystem_path(folder)):
            os.makedirs(filesystem_path(folder), exist_ok=True)
            rel = normalize_rel_path(os.path.relpath(folder, vault_real))
            created.append(rel if rel != "." else WORKSPACE_DIRECTORY_NAME)

    if not os.path.isfile(filesystem_path(rules_path)):
        atomic_write_text(rules_path, DEFAULT_RULES_TEMPLATE)
        created.append("Classification_Rules.md")

    return {
        "workspace_dir": vault_real,
        "rules_path": rules_path,
        "created": created,
    }


def add_rule_to_markdown(rules_filepath, raw_path, raw_keywords=""):
    parts = split_category_path(raw_path)
    keywords = [k.strip() for k in str(raw_keywords or "").split(",") if k.strip()]

    tree = parse_rules_from_markdown(rules_filepath)
    if tree.errors:
        raise ValueError(f"Classification_Rules.md has existing errors: {'; '.join(tree.errors)}")

    resolved = tree.resolve_manual_path(parts)
    if resolved and len(resolved) == len(parts):
        existing_node = tree.nodes.get(resolved)
        existing_kw = set(existing_node.keywords) if existing_node else set()
        new_kw = [k for k in keywords if k.casefold() not in existing_kw]
        if not new_kw:
            return {
                "changed": False,
                "path": list(resolved),
                "keywords": list(existing_node.keywords) if existing_node else [],
            }
        parts = resolved

    with open(filesystem_path(rules_filepath), "r", encoding="utf-8", newline="") as file_obj:
        original = file_obj.read()
    newline = "\r\n" if "\r\n" in original else "\n"
    had_final_newline = original.endswith(("\n", "\r"))
    lines = original.splitlines()

    last_table_index = None
    inherited = [""] * CATEGORY_LEVELS
    matching_row_index = None
    matching_row_cells = None

    for idx, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped.startswith("|"):
            continue
        cells = split_markdown_table_row(stripped)
        if len(cells) < CATEGORY_LEVELS:
            continue
        if is_table_divider(cells) or is_category_header(cells):
            last_table_index = idx
            continue
        last_table_index = idx
        path_cells = cells[:CATEGORY_LEVELS]
        for col_idx, cell in enumerate(path_cells):
            if not cell:
                continue
            inherited[col_idx] = cell
            for child_idx in range(col_idx + 1, CATEGORY_LEVELS):
                inherited[child_idx] = ""
        populated = [i for i, v in enumerate(inherited) if v]
        if populated:
            deepest = max(populated)
            row_path = tuple(inherited[: deepest + 1])
            if len(row_path) == len(parts) and all(
                normalize_name(a) == normalize_name(b) for a, b in zip(row_path, parts)
            ):
                matching_row_index = idx
                matching_row_cells = list(cells)

    if last_table_index is None:
        raise ValueError("Could not locate the category table in Classification_Rules.md.")

    canonical_parts = list(parts)
    parent = ()
    for idx, part in enumerate(canonical_parts):
        matched = tree.match_child(parent, part)
        if matched:
            canonical_parts[idx] = matched[-1]
            parent = matched
        else:
            parent = parent + (part,)

    if matching_row_index is not None and matching_row_cells is not None:
        while len(matching_row_cells) <= CATEGORY_LEVELS:
            matching_row_cells.append("")
        existing_kw_list = [
            v.strip() for v in matching_row_cells[CATEGORY_LEVELS].split(",") if v.strip()
        ]
        existing_kw_norm = {v.casefold() for v in existing_kw_list}
        for kw in keywords:
            if kw.casefold() not in existing_kw_norm:
                existing_kw_list.append(kw)
                existing_kw_norm.add(kw.casefold())
        matching_row_cells[CATEGORY_LEVELS] = ", ".join(existing_kw_list)
        lines[matching_row_index] = "| " + " | ".join(matching_row_cells[: CATEGORY_LEVELS + 1]) + " |"
        final_keywords = existing_kw_list
    else:
        row_cells = [
            canonical_parts[i] if i < len(canonical_parts) else ""
            for i in range(CATEGORY_LEVELS)
        ]
        row_cells.append(", ".join(keywords))
        new_row = "| " + " | ".join(row_cells) + " |"
        lines.insert(last_table_index + 1, new_row)
        final_keywords = keywords

    updated = newline.join(lines)
    if had_final_newline:
        updated += newline

    atomic_write_text(rules_filepath, updated)
    validated_tree = parse_rules_from_markdown(rules_filepath)
    if validated_tree.errors:
        atomic_write_text(rules_filepath, original)
        raise ValueError(
            f"Rule update rejected due to validation error: {'; '.join(validated_tree.errors)}"
        )
    return {
        "changed": True,
        "path": canonical_parts,
        "keywords": final_keywords,
    }


def _determine_headings_to_emit(existing_filepath, tree, resolved, merge_same_topic):
    """Determine which H1-H5 topic headings need to be written when appending a new Entry."""
    full_headings = [f"{'#' * (idx + 1)} {part}" for idx, part in enumerate(resolved)]
    if not merge_same_topic or not os.path.isfile(filesystem_path(existing_filepath)):
        return full_headings, False

    try:
        _log_name, units = parse_markdown_into_units(existing_filepath, tree)
    except Exception:
        return full_headings, False

    if not units:
        return full_headings, False

    last_unit = units[-1]
    extra_h1_h5_in_last = sum(
        1
        for idx, (line, in_code) in enumerate(source_lines_with_code_context(last_unit.content))
        if idx > 0 and not in_code and HEADING_RE.match(line)
    )
    if extra_h1_h5_in_last > 0:
        return full_headings, False

    last_cat = tuple(last_unit.category_path)
    last_head = tuple(last_unit.heading_path)
    if len(last_cat) != len(last_head) or not all(
        normalize_name(a) == normalize_name(b) for a, b in zip(last_cat, last_head)
    ):
        return full_headings, False

    # Same active topic at EOF -> reuse parent H1-H5 headings without repeating them
    if len(last_cat) == len(resolved) and all(
        normalize_name(a) == normalize_name(b) for a, b in zip(last_cat, resolved)
    ):
        return [], True

    # Shared prefix -> emit only diverging H2-H5 headings
    common_len = 0
    for a, b in zip(last_cat, resolved):
        if normalize_name(a) == normalize_name(b):
            common_len += 1
        else:
            break
    if common_len >= 1 and common_len < len(resolved):
        return full_headings[common_len:], False

    return full_headings, False


def append_log_entry(
    vault_dir,
    rules_path,
    raw_category_path,
    content,
    title=None,
    date_str=None,
    log_file=None,
    auto_create_rule=False,
    escape_headings=True,
    files=None,
    kind=None,
    commit=None,
    merge_same_topic=True,
):
    parts = split_category_path(raw_category_path)
    tree = parse_rules_from_markdown(rules_path)
    if tree.errors:
        raise ValueError(f"Classification_Rules.md has errors: {'; '.join(tree.errors)}")

    resolved = tree.resolve_manual_path(parts)
    if not resolved:
        if auto_create_rule:
            rule_res = add_rule_to_markdown(rules_path, parts)
            resolved = tuple(rule_res["path"])
            tree = parse_rules_from_markdown(rules_path)
        else:
            raise ValueError(
                f"Category path '{' > '.join(parts)}' is not in Classification_Rules.md. "
                "Pass --create-rule or run 'rule-add' first."
            )

    daily_root = os.path.realpath(os.path.join(vault_dir, "Daily_Logs"))
    os.makedirs(filesystem_path(daily_root), exist_ok=True)
    if log_file:
        cleaned = str(log_file).replace("\\", "/").lstrip("/")
        if cleaned.casefold().startswith("daily_logs/"):
            cleaned = cleaned[len("daily_logs/"):]
        if not cleaned.casefold().endswith(".md"):
            cleaned += ".md"
        target_path = os.path.realpath(os.path.join(daily_root, *cleaned.split("/")))
    else:
        effective_date = date_str or date.today().isoformat()
        target_path = os.path.realpath(os.path.join(daily_root, f"{effective_date}.md"))

    try:
        inside = os.path.commonpath([target_path, daily_root]) == daily_root
    except ValueError:
        inside = False
    if not inside:
        raise ValueError(f"Log file must be inside Daily_Logs: {log_file}")

    body = str(content or "").strip()
    if not body:
        raise ValueError("Entry content must not be empty.")
    if escape_headings:
        body = sanitize_agent_body_headings(body)

    normalized_files = parse_file_list(files)
    normalized_kind = str(kind or "").strip().lower() or None
    normalized_commit = str(commit or "").strip() or None

    meta_lines = []
    if normalized_kind:
        meta_lines.append(f"**Kind**: {normalized_kind}")
    if normalized_files:
        formatted_files = ", ".join(f"`{f}`" for f in normalized_files)
        meta_lines.append(f"**Files**: {formatted_files}")
    if normalized_commit:
        meta_lines.append(f"**Commit**: `{normalized_commit}`")
    if meta_lines:
        body = "\n".join(meta_lines) + "\n\n" + body

    heading_lines, reused_topic_headings = _determine_headings_to_emit(
        target_path, tree, resolved, merge_same_topic=merge_same_topic
    )

    entry_title = str(title).strip() if title and str(title).strip() else resolved[-1]
    heading_lines.append(f"###### {entry_title}")

    section_text = "\n".join(heading_lines) + "\n\n" + body + "\n"

    existing = ""
    if os.path.isfile(filesystem_path(target_path)):
        with open(filesystem_path(target_path), "r", encoding="utf-8", newline="") as file_obj:
            existing = file_obj.read()
    newline = "\r\n" if "\r\n" in existing else "\n"
    section_normalized = section_text.replace("\r\n", "\n").replace("\n", newline)
    if existing:
        separator = "" if existing.endswith(("\n\n", "\r\n\r\n")) else (
            newline if existing.endswith(("\n", "\r\n")) else newline + newline
        )
        updated = existing + separator + section_normalized
    else:
        updated = section_normalized

    atomic_write_text(target_path, updated)
    rel_log_path = normalize_rel_path(
        os.path.relpath(target_path, os.path.realpath(vault_dir))
    )
    return {
        "log_path": rel_log_path,
        "category_path": list(resolved),
        "title": entry_title,
        "kind": normalized_kind,
        "files": normalized_files,
        "commit": normalized_commit,
        "reused_topic_headings": reused_topic_headings,
        "merged_into_last_unit": reused_topic_headings,
    }


def _find_review_path_for_category(vault_dir, category_tuple):
    review_dir = os.path.join(vault_dir, "Topic_Reviews", *category_tuple)
    if os.path.isdir(filesystem_path(review_dir)):
        for fname in sorted(os.listdir(filesystem_path(review_dir))):
            if fname.casefold().endswith(".md"):
                full_rev = os.path.realpath(os.path.join(review_dir, fname))
                return normalize_rel_path(
                    os.path.relpath(full_rev, os.path.realpath(vault_dir))
                )
    return None


def _get_entry_meta_and_text(vault_dir, tree, source_id, entry, need_full_content=False):
    """Retrieve pre-indexed metadata from entry, reading source file only if needed."""
    has_indexed = "summary" in entry and "search_text" in entry
    raw_content = None
    if not has_indexed or need_full_content:
        try:
            _fp, _rel, _units, _idx, unit = locate_source_unit(
                vault_dir, tree, source_id, entry
            )
            raw_content = SOURCE_ID_RE.sub("", unit.content).strip()
        except Exception:
            raw_content = ""

    if has_indexed:
        meta = {
            "kind": entry.get("kind"),
            "files": list(entry.get("files") or []),
            "commit": entry.get("commit"),
            "summary": entry.get("summary", ""),
            "search_text": entry.get("search_text", ""),
        }
    else:
        meta = extract_entry_metadata(raw_content or "")

    return meta, raw_content


def query_topic_context(
    vault_dir,
    metadata_path,
    rules_path,
    raw_topic_path,
    limit=5,
    mode="review",
    max_chars=DEFAULT_CONTEXT_MAX_CHARS,
    max_entry_chars=None,
):
    parts = split_category_path(raw_topic_path)
    tree = parse_rules_from_markdown(rules_path)
    resolved = tree.resolve_manual_path(parts) or tuple(parts)
    norm_prefix = tuple(normalize_name(p) for p in resolved)

    if max_entry_chars is None:
        if mode == "index":
            max_entry_chars = 240
        elif mode == "entries":
            max_entry_chars = DEFAULT_ENTRIES_MODE_CHARS
        else:
            max_entry_chars = DEFAULT_REVIEW_ENTRY_CHARS

    metadata = load_metadata(metadata_path) if os.path.isfile(metadata_path) else {}
    entries = metadata.get("entries", {}) if isinstance(metadata, dict) else {}

    candidates = []
    for source_id, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        matched_cats = [
            tuple(m.get("path", ()))
            for m in entry.get("matched_categories", [])
            if isinstance(m, dict) and m.get("path")
        ]
        hit = any(
            len(norm_prefix) <= len(cat)
            and tuple(normalize_name(c) for c in cat[: len(norm_prefix)]) == norm_prefix
            for cat in matched_cats
        )
        if not hit:
            continue
        source_rel = entry.get("source_path", "")
        entry_date = entry.get("date") or source_date_key(source_rel)
        candidates.append((entry_date, source_rel, entry.get("source_line", 0), source_id, entry, matched_cats))

    candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    total_entries = len(candidates)
    selected_candidates = candidates[: max(0, int(limit))]

    review_rel_path = _find_review_path_for_category(vault_dir, resolved)
    review_content = None
    review_truncated = False
    remaining_budget = max(500, int(max_chars)) if max_chars and max_chars > 0 else None

    if mode != "index" and review_rel_path:
        full_rev = os.path.join(vault_dir, *review_rel_path.split("/"))
        if os.path.isfile(filesystem_path(full_rev)):
            with open(filesystem_path(full_rev), "r", encoding="utf-8") as file_obj:
                raw_rev = file_obj.read()
            if remaining_budget is not None:
                rev_cap = remaining_budget if mode == "review" else max(1000, remaining_budget // 2)
                review_content, review_truncated = _truncate_text(raw_rev, rev_cap)
                remaining_budget = max(0, remaining_budget - len(review_content))
            else:
                review_content = raw_rev

    matched_entries = []
    budget_truncated = review_truncated
    need_body = mode in {"review", "entries"}

    for entry_date, source_rel, source_line, source_id, entry, matched_cats in selected_candidates:
        meta, raw_content = _get_entry_meta_and_text(
            vault_dir, tree, source_id, entry, need_full_content=need_body
        )
        item = {
            "source_id": source_id,
            "date": entry_date,
            "source_path": source_rel,
            "source_line": source_line,
            "title": entry.get("source_heading", ""),
            "kind": meta["kind"],
            "files": meta["files"],
            "commit": meta["commit"],
            "summary": meta["summary"],
            "categories": [list(cat) for cat in matched_cats],
            "generated_paths": entry.get("generated_paths", []),
        }
        if mode == "index":
            item["content"] = meta["summary"]
            item["truncated"] = False
        else:
            effective_cap = max_entry_chars
            if remaining_budget is not None:
                if remaining_budget <= 120:
                    item["content"] = meta["summary"]
                    item["truncated"] = True
                    budget_truncated = True
                    matched_entries.append(item)
                    continue
                effective_cap = min(effective_cap, remaining_budget) if effective_cap else remaining_budget
            bounded_text, was_trunc = _truncate_text(raw_content or meta["summary"], effective_cap, source_id=source_id)
            item["content"] = bounded_text
            item["truncated"] = was_trunc
            if was_trunc:
                budget_truncated = True
            if remaining_budget is not None:
                remaining_budget = max(0, remaining_budget - len(bounded_text))
        matched_entries.append(item)

    return {
        "topic_path": list(resolved),
        "mode": mode,
        "max_chars": max_chars,
        "budget_truncated": budget_truncated,
        "review_path": review_rel_path,
        "review_markdown": review_content,
        "total_entries": total_entries,
        "entries": matched_entries,
    }


def search_workspace_entries(
    vault_dir,
    metadata_path,
    rules_path,
    query=None,
    file_filter=None,
    kind_filter=None,
    commit_filter=None,
    topic_filter=None,
    limit=10,
    max_chars=DEFAULT_CONTEXT_MAX_CHARS,
    max_entry_chars=DEFAULT_SEARCH_ENTRY_CHARS,
):
    tree = parse_rules_from_markdown(rules_path)
    norm_topic_prefix = None
    if topic_filter:
        parts = split_category_path(topic_filter)
        resolved = tree.resolve_manual_path(parts) or tuple(parts)
        norm_topic_prefix = tuple(normalize_name(p) for p in resolved)

    query_terms = [t.casefold() for t in str(query or "").split() if t.strip()]
    norm_file_filter = (
        normalize_rel_path(str(file_filter or "").strip()).lstrip("./").casefold()
        if file_filter
        else None
    )
    norm_kind_filter = str(kind_filter or "").strip().casefold() if kind_filter else None
    norm_commit_filter = str(commit_filter or "").strip().casefold() if commit_filter else None

    metadata = load_metadata(metadata_path) if os.path.isfile(metadata_path) else {}
    entries = metadata.get("entries", {}) if isinstance(metadata, dict) else {}

    matched_raw = []
    related_reviews = []
    seen_reviews = set()

    for source_id, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        matched_cats = [
            tuple(m.get("path", ()))
            for m in entry.get("matched_categories", [])
            if isinstance(m, dict) and m.get("path")
        ]
        if norm_topic_prefix is not None:
            hit_topic = any(
                len(norm_topic_prefix) <= len(cat)
                and tuple(normalize_name(c) for c in cat[: len(norm_topic_prefix)]) == norm_topic_prefix
                for cat in matched_cats
            )
            if not hit_topic:
                continue

        meta, _unused = _get_entry_meta_and_text(
            vault_dir, tree, source_id, entry, need_full_content=False
        )

        if norm_kind_filter and (meta["kind"] or "").casefold() != norm_kind_filter:
            continue

        if norm_commit_filter:
            entry_commit = (meta["commit"] or "").casefold()
            if not entry_commit or not entry_commit.startswith(norm_commit_filter):
                continue

        if norm_file_filter:
            file_hit = any(norm_file_filter in f.casefold() for f in meta["files"]) or (
                norm_file_filter in normalize_rel_path(meta["search_text"]).casefold()
            )
            if not file_hit:
                continue

        if query_terms:
            haystack = " ".join(
                [
                    entry.get("source_heading", ""),
                    " ".join("/".join(cat) for cat in matched_cats),
                    " ".join(meta["files"]),
                    meta["kind"] or "",
                    meta["commit"] or "",
                    meta["search_text"],
                ]
            ).casefold()
            if not all(term in haystack for term in query_terms):
                continue

        for cat in matched_cats:
            rev = _find_review_path_for_category(vault_dir, cat)
            if rev and rev not in seen_reviews:
                seen_reviews.add(rev)
                related_reviews.append(rev)

        source_rel = entry.get("source_path", "")
        entry_date = entry.get("date") or source_date_key(source_rel)
        matched_raw.append(
            (entry_date, source_rel, entry.get("source_line", 0), source_id, entry, matched_cats, meta)
        )

    matched_raw.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    total_matches = len(matched_raw)

    results = []
    remaining_budget = max(500, int(max_chars)) if max_chars and max_chars > 0 else None
    budget_truncated = False

    for entry_date, source_rel, source_line, source_id, entry, matched_cats, meta in matched_raw[: max(0, int(limit))]:
        effective_cap = max_entry_chars
        if remaining_budget is not None:
            effective_cap = min(effective_cap, remaining_budget) if effective_cap else remaining_budget
        preview, was_trunc = _truncate_text(meta["search_text"] or meta["summary"], effective_cap, source_id=source_id)
        if was_trunc:
            budget_truncated = True
        if remaining_budget is not None:
            remaining_budget = max(0, remaining_budget - len(preview))
        results.append(
            {
                "source_id": source_id,
                "date": entry_date,
                "source_path": source_rel,
                "source_line": source_line,
                "title": entry.get("source_heading", ""),
                "kind": meta["kind"],
                "files": meta["files"],
                "commit": meta["commit"],
                "summary": meta["summary"],
                "categories": [list(cat) for cat in matched_cats],
                "generated_paths": entry.get("generated_paths", []),
                "content": preview,
                "truncated": was_trunc,
            }
        )

    return {
        "query": query or "",
        "file": file_filter or "",
        "kind": kind_filter or "",
        "commit": commit_filter or "",
        "topic": topic_filter or "",
        "total_matches": total_matches,
        "budget_truncated": budget_truncated,
        "related_reviews": sorted(related_reviews),
        "results": results,
    }


def _parse_semver_tuple(raw_version):
    cleaned = str(raw_version or "").strip().lstrip("vV")
    nums = re.findall(r"\d+", cleaned)
    return tuple(int(n) for n in nums[:4]) if nums else (0,)


def check_remote_version(repo=None, timeout=5):
    target_repo = (
        str(repo or "").strip()
        or os.environ.get("PAGENT_GITHUB_REPO", "").strip()
        or DEFAULT_GITHUB_REPO
    )
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", target_repo):
        raise ValueError(f"Invalid GitHub repository identifier: {target_repo}")
    url = f"https://api.github.com/repos/{target_repo}/releases/latest"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"pagent/{PAGENT_VERSION}",
        },
        method="GET",
    )
    upgrade_cmds = [
        "uv tool upgrade pagent",
        "pipx upgrade pagent",
        "git pull --ff-only",
    ]
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag_name = str(data.get("tag_name") or "").strip()
        latest_clean = tag_name.lstrip("vV") or PAGENT_VERSION
        update_available = _parse_semver_tuple(latest_clean) > _parse_semver_tuple(PAGENT_VERSION)
        return {
            "version": PAGENT_VERSION,
            "checked_remote": True,
            "repo": target_repo,
            "latest_version": latest_clean,
            "latest_tag": tag_name,
            "update_available": update_available,
            "release_url": data.get("html_url") or f"https://github.com/{target_repo}/releases",
            "upgrade_commands": upgrade_cmds,
        }
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {
            "version": PAGENT_VERSION,
            "checked_remote": False,
            "repo": target_repo,
            "latest_version": None,
            "update_available": False,
            "check_error": str(exc),
            "upgrade_commands": upgrade_cmds,
        }


def build_parser():
    parser = argparse.ArgumentParser(
        prog="pagent",
        description="Deterministic Markdown work-history compiler & viewer for AI agents and humans.",
    )
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"pagent {PAGENT_VERSION}",
    )
    parser.add_argument("--vault-dir", default=None, help="Override Workspace directory path.")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_version = sub.add_parser("version", help="Show installed version and optionally check for newer releases.")
    p_version.add_argument(
        "--check",
        action="store_true",
        help="Check GitHub Releases API for a newer version (read-only, never auto-installs).",
    )
    p_version.add_argument("--repo", default=None, help="Override GitHub owner/repo for --check.")
    p_version.add_argument("--json", action="store_true")

    p_init = sub.add_parser("init", help="Initialize Workspace folders and Classification_Rules.md.")
    p_init.add_argument("--here", action="store_true", help="Initialize ./Workspace in the current working directory.")
    p_init.add_argument("--json", action="store_true")

    p_update = sub.add_parser("update", help="Compile Daily_Logs into Subject/ and Topic_Reviews/ via Python classifier.")
    p_update.add_argument("--dry-run", action="store_true", help="Inspect classification result without writing files.")
    p_update.add_argument("--rebuild-ids", action="store_true", help="Rebuild Source ID markers after replacing logs.")
    p_update.add_argument("--json", action="store_true")

    p_rules = sub.add_parser("rules", help="List configured 5-level category paths and keywords.")
    p_rules.add_argument("--json", action="store_true")

    p_rule_add = sub.add_parser("rule-add", help="Add or update a category path in Classification_Rules.md.")
    p_rule_add.add_argument("--path", required=True, help="Category path (e.g. 'Projects/Alpha/Testing').")
    p_rule_add.add_argument("--keywords", default="", help="Comma-separated supplemental keywords.")
    p_rule_add.add_argument("--no-update", action="store_true", help="Do not run local classifier after updating rules.")
    p_rule_add.add_argument("--json", action="store_true")

    p_append = sub.add_parser("append", help="Append a structured work event Entry to Daily_Logs and compile.")
    p_append.add_argument("--path", required=True, help="Category path (e.g. 'Projects/Alpha/Testing').")
    p_append.add_argument("--title", default=None, help="Entry title (written as H6 '###### <title>').")
    p_append.add_argument("--content", default=None, help="Markdown body content.")
    p_append.add_argument("--stdin", action="store_true", help="Read Markdown body content from stdin.")
    p_append.add_argument("--files", action="append", default=[], help="Related code/file paths (comma-separated or repeated).")
    p_append.add_argument("--kind", default=None, help="Entry type (e.g. decision, bugfix, experiment, refactor, note).")
    p_append.add_argument("--commit", default=None, help="Git commit SHA associated with this entry.")
    p_append.add_argument("--git", action="store_true", help="Automatically record current Git HEAD short SHA.")
    p_append.add_argument("--date", default=None, help="Log date (YYYY-MM-DD). Defaults to today.")
    p_append.add_argument("--log-file", default=None, help="Specific filename under Daily_Logs/.")
    p_append.add_argument("--create-rule", action="store_true", help="Automatically add the path to Classification_Rules.md if missing.")
    p_append.add_argument("--no-merge", action="store_true", help="Always write full H1-H5 topic headings before the entry.")
    p_append.add_argument("--no-escape-headings", action="store_true", help="Do not convert headings inside body content.")
    p_append.add_argument("--no-update", action="store_true", help="Write log entry without running the classifier.")
    p_append.add_argument("--json", action="store_true")

    p_context = sub.add_parser("context", help="Retrieve bounded compiled review and recent entries for a topic.")
    p_context.add_argument("--topic", required=True, help="Topic path prefix (e.g. 'Projects/Alpha').")
    p_context.add_argument(
        "--mode",
        choices=["review", "index", "entries"],
        default="review",
        help="Context detail mode: 'index' (metadata only), 'review' (bounded review + summaries), 'entries' (bounded entries).",
    )
    p_context.add_argument("--limit", type=int, default=5, help="Max individual entries to return.")
    p_context.add_argument("--max-chars", type=int, default=DEFAULT_CONTEXT_MAX_CHARS, help="Max total characters to return (default: 12000).")
    p_context.add_argument("--max-entry-chars", type=int, default=None, help="Max characters per individual entry.")
    p_context.add_argument("--json", action="store_true")

    p_search = sub.add_parser("search", help="Search pre-indexed entries by keyword query, modified file path, kind, commit, or topic.")
    p_search.add_argument("--query", "-q", default=None, help="Keyword query across headings, categories, and body.")
    p_search.add_argument("--file", "-f", default=None, help="Filter by referenced/modified file path (e.g. 'review_renderer.py').")
    p_search.add_argument("--kind", "-k", default=None, help="Filter by entry kind (e.g. 'decision', 'bugfix').")
    p_search.add_argument("--commit", "-c", default=None, help="Filter by Git commit SHA prefix.")
    p_search.add_argument("--topic", "-t", default=None, help="Optional topic prefix filter.")
    p_search.add_argument("--limit", type=int, default=10, help="Max matching entries to return (default: 10).")
    p_search.add_argument("--max-chars", type=int, default=DEFAULT_CONTEXT_MAX_CHARS, help="Max total characters to return (default: 12000).")
    p_search.add_argument("--max-entry-chars", type=int, default=DEFAULT_SEARCH_ENTRY_CHARS, help="Max preview characters per entry (default: 600).")
    p_search.add_argument("--json", action="store_true")

    p_status = sub.add_parser("status", help="Show classification statistics and items needing review.")
    p_status.add_argument("--json", action="store_true")

    p_classify = sub.add_parser("classify", help="Assign or clear manual Category: for a Source ID.")
    p_classify.add_argument("--source-id", required=True, help="Target Source ID.")
    p_classify.add_argument("--path", action="append", default=[], help="Category path to assign (may be repeated).")
    p_classify.add_argument("--clear-manual", action="store_true", help="Remove manual Category: override.")
    p_classify.add_argument("--json", action="store_true")

    p_source = sub.add_parser("source", help="Inspect a specific Source ID entry.")
    p_source.add_argument("--id", required=True, help="Target Source ID.")
    p_source.add_argument("--max-chars", type=int, default=None, help="Optional character cap on returned content.")
    p_source.add_argument("--json", action="store_true")

    p_view = sub.add_parser("view", help="Open the read-only local web viewer (folder tree + Markdown renderer).")
    p_view.add_argument("--port", type=int, default=8000, help="Local port (default: 8000).")
    p_view.add_argument("--no-browser", action="store_true", help="Do not open browser automatically.")
    p_view.add_argument("--refresh", action="store_true", help="Run classifier update before opening viewer.")
    p_view.add_argument("--no-refresh", action="store_true", help=argparse.SUPPRESS)

    return parser


def _emit(payload, as_json, human_lines=()):
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for line in human_lines:
            print(line)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    parser = build_parser()
    args = parser.parse_args(argv)
    explicit_vault = args.vault_dir
    if args.subcommand == "init" and getattr(args, "here", False) and not explicit_vault:
        explicit_vault = os.path.join(os.getcwd(), WORKSPACE_DIRECTORY_NAME)

    vault_dir = resolve_active_workspace_dir(
        SCRIPT_DIR,
        explicit_vault_dir=explicit_vault,
        for_init=(args.subcommand == "init"),
    )
    rules_path = os.path.join(vault_dir, "Classification_Rules.md")
    state_dir = resolve_workspace_state_dir(SCRIPT_DIR, vault_dir)
    metadata_path = os.path.join(state_dir, "organizer_metadata.json")

    try:
        if args.subcommand == "version":
            if args.check:
                info = check_remote_version(repo=args.repo)
            else:
                info = {
                    "version": PAGENT_VERSION,
                    "checked_remote": False,
                    "upgrade_commands": [
                        "uv tool upgrade pagent",
                        "pipx upgrade pagent",
                        "git pull --ff-only",
                    ],
                }
            payload = {"status": "ok", **info}
            lines = [f"pagent {PAGENT_VERSION}"]
            if args.check:
                if info.get("checked_remote"):
                    if info.get("update_available"):
                        lines.append(
                            f"Update available: v{info['latest_version']} ({info['release_url']})"
                        )
                        lines.append(
                            f"Upgrade via: {'  |  '.join(info['upgrade_commands'])}"
                        )
                    else:
                        lines.append(f"Up to date (latest: v{info['latest_version']}).")
                else:
                    lines.append(f"Remote check skipped/unavailable: {info.get('check_error')}")
            _emit(payload, args.json, lines)
            return 0

        if args.subcommand == "init":
            info = ensure_workspace_initialized(vault_dir)
            payload = {"status": "ok", **info}
            _emit(
                payload,
                args.json,
                [
                    f"Workspace ready: {info['workspace_dir']}",
                    f"Created: {', '.join(info['created']) if info['created'] else '(already initialized)'}",
                ],
            )
            return 0

        if args.subcommand == "update":
            ensure_workspace_initialized(vault_dir)
            extra = []
            if args.dry_run:
                extra.append("--dry-run")
            if args.rebuild_ids:
                extra.append("--rebuild-source-ids")
            res = run_production_classifier(vault_dir, extra_args=extra)
            metadata = load_metadata(metadata_path) if os.path.isfile(metadata_path) else {}
            payload = {
                "status": "ok" if res["ok"] else "error",
                "returncode": res["returncode"],
                "dry_run": bool(args.dry_run),
                "rebuild_ids": bool(args.rebuild_ids),
                "stats": metadata.get("stats", {}) if isinstance(metadata, dict) else {},
                "output": res["output"],
            }
            _emit(payload, args.json, [res["output"]] if res["output"] else ["Update completed."])
            return 0 if res["ok"] else (res["returncode"] or 1)

        if args.subcommand == "rules":
            tree = parse_rules_from_markdown(rules_path)
            categories = [
                {
                    "path": list(path),
                    "depth": len(path),
                    "keywords": list(node.keywords),
                    "has_children": tree.has_children(path),
                }
                for path, node in sorted(
                    tree.nodes.items(),
                    key=lambda item: tuple(p.casefold() for p in item[0]),
                )
            ]
            payload = {
                "status": "ok" if not tree.errors else "error",
                "count": len(categories),
                "errors": tree.errors,
                "categories": categories,
            }
            lines = [
                f"{' > '.join(c['path'])} (L{c['depth']})"
                + (f" [keywords: {', '.join(c['keywords'])}]" if c["keywords"] else "")
                for c in categories
            ]
            _emit(payload, args.json, lines)
            return 0 if not tree.errors else 2

        if args.subcommand == "rule-add":
            ensure_workspace_initialized(vault_dir)
            with VaultProcessLock(vault_dir, "agent-rule-add", command=command_text(__file__, argv)):
                res = add_rule_to_markdown(rules_path, args.path, args.keywords)
                rebuild = {"ok": True}
                daily_dir = os.path.join(vault_dir, "Daily_Logs")
                has_md = os.path.isdir(daily_dir) and any(
                    f.casefold().endswith(".md") for f in os.listdir(daily_dir)
                )
                if not args.no_update and has_md:
                    rebuild = run_production_classifier(vault_dir, lock_held_by_parent=True)
            payload = {"status": "ok", **res, "rebuilt": rebuild.get("ok", True)}
            _emit(payload, args.json, [f"Rule ready: {' > '.join(res['path'])}"])
            return 0

        if args.subcommand == "append":
            ensure_workspace_initialized(vault_dir)
            body = sys.stdin.read() if args.stdin else args.content
            effective_commit = args.commit or (detect_git_head_commit() if args.git else None)
            with VaultProcessLock(vault_dir, "agent-append", command=command_text(__file__, argv)):
                res = append_log_entry(
                    vault_dir,
                    rules_path,
                    args.path,
                    body,
                    title=args.title,
                    date_str=args.date,
                    log_file=args.log_file,
                    auto_create_rule=args.create_rule,
                    escape_headings=not args.no_escape_headings,
                    files=args.files,
                    kind=args.kind,
                    commit=effective_commit,
                    merge_same_topic=not args.no_merge,
                )
                rebuild = {"ok": True}
                if not args.no_update:
                    rebuild = run_production_classifier(vault_dir, lock_held_by_parent=True)
            if not rebuild.get("ok", True):
                payload = {
                    "status": "error",
                    "message": "Entry appended, but classification update failed.",
                    "details": rebuild.get("output", ""),
                    **res,
                }
                _emit(payload, args.json, [payload["message"], payload["details"]])
                return 1
            payload = {"status": "ok", **res, "updated": not args.no_update}
            _emit(
                payload,
                args.json,
                [f"Appended to {res['log_path']} under {' > '.join(res['category_path'])}"],
            )
            return 0

        if args.subcommand == "context":
            ctx = query_topic_context(
                vault_dir,
                metadata_path,
                rules_path,
                args.topic,
                limit=args.limit,
                mode=args.mode,
                max_chars=args.max_chars,
                max_entry_chars=args.max_entry_chars,
            )
            payload = {"status": "ok", **ctx}
            lines = [
                f"Topic: {' > '.join(ctx['topic_path'])} ({ctx['total_entries']} entries, mode={ctx['mode']})",
                f"Review file: {ctx['review_path'] or '(none)'}",
            ]
            if ctx["review_markdown"]:
                lines.extend(["", ctx["review_markdown"].rstrip()])
            elif ctx["mode"] == "index":
                for item in ctx["entries"]:
                    lines.append(
                        f"- [{item['source_id']}] {item['date']} · {item['title']} "
                        f"({item.get('kind') or 'note'}): {item.get('summary') or ''}"
                    )
            _emit(payload, args.json, lines)
            return 0

        if args.subcommand == "search":
            res = search_workspace_entries(
                vault_dir,
                metadata_path,
                rules_path,
                query=args.query,
                file_filter=args.file,
                kind_filter=args.kind,
                commit_filter=args.commit,
                topic_filter=args.topic,
                limit=args.limit,
                max_chars=args.max_chars,
                max_entry_chars=args.max_entry_chars,
            )
            payload = {"status": "ok", **res}
            lines = [f"Matches: {res['total_matches']}"]
            if res["related_reviews"]:
                lines.append(f"Related reviews: {', '.join(res['related_reviews'])}")
            for item in res["results"]:
                cat_str = " > ".join(item["categories"][0]) if item["categories"] else "Unclassified"
                kind_tag = f" [{item['kind']}]" if item.get("kind") else ""
                files_tag = f" (files: {', '.join(item['files'])})" if item.get("files") else ""
                commit_tag = f" @{item['commit']}" if item.get("commit") else ""
                lines.append(
                    f"- [{item['source_id']}] {item['date']} · {cat_str} · {item['title']}{kind_tag}{files_tag}{commit_tag}"
                )
            _emit(payload, args.json, lines)
            return 0

        if args.subcommand == "status":
            metadata = load_metadata(metadata_path) if os.path.isfile(metadata_path) else {}
            items = (
                build_review_items(vault_dir, metadata_path, rules_path)
                if os.path.isfile(metadata_path)
                else []
            )
            payload = {
                "status": "ok",
                "stats": metadata.get("stats", {}),
                "needs_review_count": len(items),
                "needs_review": items,
            }
            lines = [
                f"Needs review: {len(items)}",
                *[
                    f"- [{item['source_id']}] {item['source_path']}:{item['source_line']} "
                    f"{item['source_heading']} ({'; '.join(item['review_reasons'])})"
                    for item in items
                ],
            ]
            _emit(payload, args.json, lines)
            return 0

        if args.subcommand == "classify":
            with VaultProcessLock(vault_dir, "agent-classify", command=command_text(__file__, argv)):
                metadata = load_metadata(metadata_path)
                entry = metadata.get("entries", {}).get(args.source_id)
                if not isinstance(entry, dict):
                    raise ValueError(f"Source ID not found in metadata: {args.source_id}")
                tree = parse_rules_from_markdown(rules_path)
                _fp, _rel, _units, _idx, unit = locate_source_unit(
                    vault_dir, tree, args.source_id, entry
                )
                fingerprint = source_unit_fingerprint(unit)
                if args.clear_manual:
                    edit_res = remove_manual_categories(
                        args.source_id, fingerprint, vault_dir, metadata_path, rules_path
                    )
                else:
                    if not args.path:
                        raise ValueError("Specify at least one --path or --clear-manual.")
                    raw_paths = [list(split_category_path(p)) for p in args.path]
                    edit_res = apply_manual_categories(
                        args.source_id, raw_paths, fingerprint, vault_dir, metadata_path, rules_path
                    )
                rebuild = run_production_classifier(vault_dir, lock_held_by_parent=True)
            if not rebuild.get("ok", True):
                raise RuntimeError(f"Classifier rebuild failed: {rebuild.get('output', '')}")
            payload = {"status": "ok", **edit_res}
            _emit(payload, args.json, [f"Updated classification for {args.source_id} in {edit_res['source_path']}"])
            return 0

        if args.subcommand == "source":
            metadata = load_metadata(metadata_path)
            entry = metadata.get("entries", {}).get(args.id)
            if not isinstance(entry, dict):
                raise ValueError(f"Source ID not found: {args.id}")
            tree = parse_rules_from_markdown(rules_path)
            _fp, _rel, _units, _idx, unit = locate_source_unit(
                vault_dir, tree, args.id, entry
            )
            meta = extract_entry_metadata(unit.content)
            content_out, was_trunc = _truncate_text(unit.content, args.max_chars)
            payload = {
                "status": "ok",
                "source_id": args.id,
                "kind": meta["kind"],
                "files": meta["files"],
                "commit": meta["commit"],
                "summary": meta["summary"],
                **entry,
                "content": content_out,
                "truncated": was_trunc,
            }
            _emit(
                payload,
                args.json,
                [f"Source ID: {args.id} ({entry.get('source_path')}:{entry.get('source_line')})", "", content_out],
            )
            return 0

        if args.subcommand == "view":
            import workspace_viewer

            view_argv = ["--port", str(args.port), "--vault-dir", vault_dir]
            if args.no_browser:
                view_argv.append("--no-browser")
            if args.refresh and not args.no_refresh:
                view_argv.append("--refresh")
            return workspace_viewer.main(view_argv)

    except ProcessLockUnavailable as exc:
        if getattr(args, "json", False):
            print(json.dumps({"status": "busy", "message": str(exc), "owner": exc.owner}, ensure_ascii=False))
        else:
            print_lock_error(exc)
        return LOCK_BUSY_EXIT_CODE
    except (ValueError, ReviewError, StateFileError, RuntimeError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
