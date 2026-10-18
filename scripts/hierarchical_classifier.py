import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote, urlsplit

from atomic_io import StateFileError, atomic_write_json, atomic_write_text, load_json_state, filesystem_path
from process_lock import (
    LOCK_BUSY_EXIT_CODE,
    ProcessLockUnavailable,
    VaultProcessLock,
    command_text,
    lock_is_delegated_by_parent,
    print_lock_error,
)
from document_utils import (
    AUTO_GENERATED_REVIEW, AUTO_GENERATED_SUBJECT, IMAGE_TOKEN_RE,
    _is_within_directory, _normalize_image_target, _resolve_source_image_path,
    cleanup_generated_root, escape_markdown_link_label, extract_entry_metadata,
    is_prefix, make_markdown_link, normalize_rel_path, parse_stem_date,
    sanitize_filename, source_date_key, strip_markdown_target_title,
)
from workspace_paths import resolve_workspace_dir, resolve_workspace_state_dir
from review_content import extract_review_blocks


CATEGORY_LEVELS = 5
METADATA_SCHEMA_VERSION = 5
DEFAULT_OUTPUT_DIR = "Subject_Hierarchical"
DEFAULT_REVIEW_DIR = "Topic_Reviews_Hierarchical"
DEFAULT_METADATA_FILE = "organizer_metadata_hierarchical.json"
PRODUCTION_OUTPUT_DIR = "Subject"
PRODUCTION_REVIEW_DIR = "Topic_Reviews"
PRODUCTION_METADATA_FILE = "organizer_metadata.json"
SOURCE_ID_MARKER_NAME = "research-notes-source-id"
HEADING_RE = re.compile(r"^(#{1,5})[ \t]+(.*?)[ \t]*$")
ENTRY_HEADING_RE = re.compile(r"^######[ \t]+(.*?)[ \t]*$")
ANY_UNIT_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")
CATEGORY_LINE_RE = re.compile(r"^(Category|분류)\s*:\s*(.*)$", re.IGNORECASE)
SOURCE_ID_RE = re.compile(
    r"<!--\s*(?:[a-zA-Z0-9][a-zA-Z0-9_-]*-)?source-id\s*:\s*"
    r"([a-fA-F0-9]{8,64})\s*-->",
    re.IGNORECASE,
)
def normalize_name(value):
    value = re.sub(r"[`*_~]", "", value or "")
    value = re.sub(r"[_-]+", " ", value)
    return re.sub(r"\s+", " ", value).strip().casefold()


def is_markdown_blockquote_line(line):
    return line.lstrip(" \t").startswith(">")


def source_lines_with_code_context(content, keepends=False):
    """Keep fenced code examples out of headings and permanent ID handling."""
    fence = None
    for line in content.splitlines(keepends=keepends):
        match = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})([^\r\n]*)", line)
        in_code = fence is not None
        if match:
            marker, suffix = match.groups()
            if fence is None and (marker[0] != "`" or "`" not in suffix):
                fence = marker
                in_code = True
            elif fence is not None and marker[0] == fence[0] and len(marker) >= len(fence) and not suffix.strip():
                fence = None
        yield line, in_code


def iter_active_source_id_matches(content):
    """Yield Source ID markers that belong to the source document itself.

    Source ID comments inside Markdown blockquotes are quoted material, not
    identifiers for the surrounding classification unit.
    """
    for line, in_code in source_lines_with_code_context(content):
        if in_code or is_markdown_blockquote_line(line):
            continue
        yield from SOURCE_ID_RE.finditer(line)


def replace_active_source_id_comments(content, replacement):
    lines = []
    for line, in_code in source_lines_with_code_context(content, keepends=True):
        if in_code or is_markdown_blockquote_line(line):
            lines.append(line)
        else:
            lines.append(SOURCE_ID_RE.sub(replacement, line))
    return "".join(lines)






def split_markdown_table_row(line):
    return [part.strip() for part in line.strip().strip("|").split("|")]


def is_table_divider(cells):
    return cells and all(re.fullmatch(r"[:\-\s]+", cell or "") for cell in cells)


def is_category_header(cells):
    joined = " ".join(cells[:CATEGORY_LEVELS]).casefold()
    return "level 1" in joined or "대분류" in joined or "대주제" in joined


def is_ascii_word(term):
    return bool(re.fullmatch(r"[a-z0-9]+", term))


def text_contains_term(term, text):
    if is_ascii_word(term):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text))
    return term in text


def keyword_matches_text(keyword, text, text_no_spaces):
    terms = keyword.casefold().split()
    if terms and all(text_contains_term(term, text) for term in terms):
        return True

    compact = keyword.casefold().replace(" ", "")
    if not compact:
        return False
    if len(terms) == 1 and is_ascii_word(compact) and len(compact) <= 3:
        return False
    return compact in text_no_spaces


@dataclass
class RuleNode:
    path: tuple
    keywords: list = field(default_factory=list)
    explicit: bool = False


class RuleTree:
    def __init__(self):
        self.nodes = {}
        self.children = defaultdict(dict)
        self.errors = []
        self.warnings = []

    def ensure_path(self, path, explicit=False):
        for size in range(1, len(path) + 1):
            partial = tuple(path[:size])
            node = self.nodes.setdefault(partial, RuleNode(path=partial))
            if size == len(path) and explicit:
                node.explicit = True
            parent = partial[:-1]
            normalized = normalize_name(partial[-1])
            existing = self.children[parent].get(normalized)
            if existing and existing != partial[-1]:
                self.errors.append(
                    f"Ambiguous sibling names under {' > '.join(parent) or '(root)'}: "
                    f"{existing!r} and {partial[-1]!r}"
                )
            self.children[parent][normalized] = partial[-1]
        return self.nodes[tuple(path)]

    def add_rule(self, path, keywords, line_number):
        node = self.ensure_path(path, explicit=True)
        existing = set(node.keywords)
        for keyword in keywords:
            normalized = keyword.strip().casefold()
            if normalized and normalized not in existing:
                node.keywords.append(normalized)
                existing.add(normalized)
        if node.explicit and not keywords:
            self.warnings.append(
                f"Line {line_number}: {' > '.join(path)} has no supplemental keywords. "
                "Heading matching will still work."
            )

    def match_child(self, parent_path, heading_text):
        canonical = self.children.get(tuple(parent_path), {}).get(normalize_name(heading_text))
        if not canonical:
            return None
        return tuple(parent_path) + (canonical,)

    def resolve_manual_path(self, values):
        current = ()
        for value in values:
            matched = self.match_child(current, value)
            if not matched:
                return None
            current = matched
        return current

    def has_children(self, path):
        return bool(self.children.get(tuple(path)))

    def iter_keyword_nodes(self, level1):
        for path, node in self.nodes.items():
            if path and path[0] == level1 and node.keywords:
                yield path, node


def parse_rules_from_markdown(rules_filepath):
    tree = RuleTree()
    with open(rules_filepath, "r", encoding="utf-8") as file_obj:
        lines = file_obj.readlines()

    inherited = [""] * CATEGORY_LEVELS
    for line_number, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = split_markdown_table_row(line)
        if len(cells) < CATEGORY_LEVELS:
            continue
        if is_table_divider(cells) or is_category_header(cells):
            continue

        path_cells = cells[:CATEGORY_LEVELS]
        for index, cell in enumerate(path_cells):
            if not cell:
                continue
            inherited[index] = cell
            for child_index in range(index + 1, CATEGORY_LEVELS):
                inherited[child_index] = ""

        populated = [index for index, value in enumerate(inherited) if value]
        if not populated:
            continue
        deepest = max(populated)
        if any(not inherited[index] for index in range(deepest + 1)):
            tree.errors.append(f"Line {line_number}: category level is missing before a child level.")
            continue

        path = tuple(inherited[: deepest + 1])
        keyword_cell = cells[CATEGORY_LEVELS] if len(cells) > CATEGORY_LEVELS else ""
        keywords = [value.strip() for value in keyword_cell.split(",") if value.strip()]
        tree.add_rule(path, keywords, line_number)

    if not tree.nodes:
        tree.errors.append("No category rows were found in Classification_Rules.md.")
    return tree


def is_page_title(heading_text, log_name, first_heading, tree):
    if not first_heading or tree.match_child((), heading_text):
        return False
    normalized_heading = normalize_name(heading_text)
    normalized_log = normalize_name(log_name)
    return (
        normalized_heading == normalized_log
        or "일지" in heading_text
        or bool(re.fullmatch(r"[\d.\-\s]+(?:일지)?", heading_text.strip()))
    )


@dataclass
class SourceUnit:
    title: str
    lines: list
    category_path: tuple
    heading_path: tuple
    start_line: int
    occurrence: int = 1

    @property
    def content(self):
        return "\n".join(self.lines).strip()


@dataclass
class SourceMarkerPlan:
    filepath: str
    source_rel_path: str
    original_content: str
    markers: list
    heading_count: int
    document_root_count: int
    normalized_marker_count: int = 0
    relocations: list = field(default_factory=list)
    consolidations: list = field(default_factory=list)


def parse_markdown_into_units(filepath, tree, content=None):
    if content is None:
        with open(filepath, "r", encoding="utf-8") as file_obj:
            content = file_obj.read()
    lines = content.splitlines()

    log_name = os.path.splitext(os.path.basename(filepath))[0].strip()
    category_context = ()
    heading_context = [""] * CATEGORY_LEVELS
    units = []
    current = None
    first_heading = True

    def finish_current():
        nonlocal current
        if current and current.content:
            units.append(current)
        current = None

    for line_number, (line, in_code) in enumerate(source_lines_with_code_context(content), 1):
        entry_match = None if in_code or is_markdown_blockquote_line(line) else ENTRY_HEADING_RE.match(line)
        if entry_match:
            entry_title = entry_match.group(1).strip()
            if entry_title:
                finish_current()
                current = SourceUnit(
                    title=entry_title,
                    lines=[line],
                    category_path=category_context,
                    heading_path=tuple(value for value in heading_context if value),
                    start_line=line_number,
                )
                first_heading = False
                continue

        match = None if in_code else HEADING_RE.match(line)
        if not match:
            if current is None:
                current = SourceUnit(log_name, [], category_context, tuple(), line_number)
            current.lines.append(line)
            continue

        hashes, heading_text = match.groups()
        level = len(hashes)
        heading_text = heading_text.strip()
        if not heading_text:
            if current is None:
                current = SourceUnit(log_name, [], category_context, tuple(), line_number)
            current.lines.append(line)
            first_heading = False
            continue

        if level == 1 and is_page_title(heading_text, log_name, first_heading, tree):
            first_heading = False
            continue
        first_heading = False

        heading_context[level - 1] = heading_text
        for index in range(level, CATEGORY_LEVELS):
            heading_context[index] = ""

        previous_context = category_context
        if level == 1:
            matched_path = tree.match_child((), heading_text)
            category_context = matched_path or ()
        else:
            parent_path = previous_context[: level - 1]
            matched_path = None
            if len(parent_path) == level - 1:
                matched_path = tree.match_child(parent_path, heading_text)
            category_context = matched_path or parent_path

        # H1/H2 remain author-facing content boundaries, matching the legacy
        # organizer's useful granularity. H3-H5 split only when they change the
        # recognized category path; ordinary nested headings stay in the unit.
        starts_new_unit = current is None or level <= 2 or category_context != previous_context
        if starts_new_unit:
            finish_current()
            current = SourceUnit(
                title=heading_text,
                lines=[line],
                category_path=category_context,
                heading_path=tuple(value for value in heading_context if value),
                start_line=line_number,
            )
        else:
            current.lines.append(line)

    finish_current()

    occurrence_counts = defaultdict(int)
    for unit in units:
        signature = tuple(normalize_name(value) for value in unit.heading_path) or (
            normalize_name(unit.title),
        )
        occurrence_counts[signature] += 1
        unit.occurrence = occurrence_counts[signature]
    return log_name, units


def clean_match_text(value):
    value = re.sub(r"!\[\[.*?\]\]", " ", value)
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", value)
    value = re.sub(r"<!--.*?-->", " ", value, flags=re.DOTALL)
    return value.casefold()


def node_matches(node, text, text_no_spaces):
    return any(keyword_matches_text(keyword, text, text_no_spaces) for keyword in node.keywords)




def qualify_keyword_path(path, primary_path, tree, text, text_no_spaces):
    node = tree.nodes[path]
    if not node_matches(node, text, text_no_spaces):
        return False

    for level in range(2, len(path)):
        ancestor = path[:level]
        if is_prefix(ancestor, primary_path):
            continue
        ancestor_node = tree.nodes.get(ancestor)
        if ancestor_node and ancestor_node.keywords:
            if not node_matches(ancestor_node, text, text_no_spaces):
                return False
    return True


def remove_ancestor_candidates(paths):
    unique = []
    for path in sorted(set(paths), key=lambda value: (-len(value), value)):
        if any(is_prefix(path, existing) for existing in unique):
            continue
        unique.append(path)
    return sorted(unique)


def iter_active_category_lines(content):
    """Yield (line_index, match) for active Category: lines outside code and quotes."""
    lines = content.splitlines()
    frontmatter_end = find_frontmatter_end(lines)
    checked_after_header = 0
    recent_structural_line = True
    for index, (raw_line, in_code) in enumerate(source_lines_with_code_context(content)):
        if in_code or is_markdown_blockquote_line(raw_line):
            continue
        stripped = raw_line.strip()
        if not stripped:
            continue
        if frontmatter_end is not None and index <= frontmatter_end:
            match = CATEGORY_LINE_RE.match(stripped)
            if match:
                yield index, match
            continue
        if ANY_UNIT_HEADING_RE.match(raw_line) or SOURCE_ID_RE.fullmatch(stripped):
            recent_structural_line = True
            continue
        checked_after_header += 1
        match = CATEGORY_LINE_RE.match(stripped)
        if match and (checked_after_header <= 4 or recent_structural_line):
            yield index, match
            recent_structural_line = True
            continue
        recent_structural_line = False


def extract_manual_categories(content, tree):
    lines = content.splitlines()
    matched_indices = []
    resolved = []
    invalid = []
    for index, match in iter_active_category_lines(content):
        matched_indices.append(index)
        raw_paths = [value.strip() for value in match.group(2).split(",") if value.strip()]
        for raw_path in raw_paths:
            values = [value.strip() for value in re.split(r"[/>]", raw_path) if value.strip()]
            path = tree.resolve_manual_path(values)
            if path:
                if path not in resolved:
                    resolved.append(path)
            else:
                if raw_path not in invalid:
                    invalid.append(raw_path)
    if not matched_indices:
        return [], [], content
    for index in reversed(matched_indices):
        del lines[index]
    return resolved, invalid, "\n".join(lines).strip()


def classify_unit(unit, tree, level1_hint=None, previous_entry=None):
    manual_paths, invalid_manual, clean_content = extract_manual_categories(unit.content, tree)
    clean_content = replace_active_source_id_comments(clean_content, "").strip()
    manual_issues = [f"Unknown manual category: {value}" for value in invalid_manual]
    primary = tuple(unit.category_path)

    if manual_paths:
        effective_paths = []
        for path in manual_paths:
            if primary and is_prefix(path, primary) and len(primary) > len(path):
                target = primary
            else:
                target = path
            if target not in effective_paths:
                effective_paths.append(target)
        if len(effective_paths) > 1:
            effective_paths = remove_ancestor_candidates(effective_paths)

        if primary:
            heading_compatible = any(
                is_prefix(primary, path) or is_prefix(path, primary)
                for path in effective_paths
            )
            if not heading_compatible:
                prev_manual_heading = (
                    previous_entry.get("manual_heading_path")
                    if isinstance(previous_entry, dict)
                    else None
                )
                heading_changed_since_manual = (
                    isinstance(prev_manual_heading, list)
                    and [normalize_name(v) for v in unit.heading_path]
                    != [normalize_name(v) for v in prev_manual_heading]
                )
                same_root_divergence = any(
                    path[0] == primary[0] for path in effective_paths
                )
                leaf_heading_contradiction = (
                    previous_entry is None
                    and len(effective_paths) == 1
                    and len(primary) >= 2
                    and not tree.has_children(primary)
                )
                if (
                    same_root_divergence
                    or heading_changed_since_manual
                    or leaf_heading_contradiction
                ):
                    manual_label = ", ".join(" > ".join(p) for p in effective_paths)
                    heading_label = " > ".join(primary)
                    manual_issues.append(
                        f"Manual category ({manual_label}) conflicts with heading category ({heading_label})"
                    )

        return {
            "paths": sorted(set(effective_paths)),
            "primary_path": effective_paths[0],
            "basis_by_path": {path: "manual" for path in effective_paths},
            "needs_review": bool(manual_issues),
            "issues": manual_issues,
            "content": clean_content,
        }
    inherited_legacy_level1 = False
    if not primary and level1_hint:
        matched_root = tree.match_child((), level1_hint)
        if matched_root:
            primary = matched_root
            inherited_legacy_level1 = True
    if not primary:
        missing_path = ("Unclassified", "Missing Level 1")
        return {
            "paths": [missing_path],
            "primary_path": (),
            "basis_by_path": {missing_path: "missing-level-1"},
            "needs_review": True,
            "issues": manual_issues + ["No recognized Level 1 Heading is active."],
            "content": clean_content,
        }

    # Supplemental keywords should describe the section's opening context, not
    # every topic mentioned later in a long meeting note or test transcript.
    corpus = " ".join(unit.heading_path) + "\n" + clean_content[:800]
    text = clean_match_text(corpus)
    text_no_spaces = re.sub(r"\s+", "", text)
    candidates = []
    for path, _node in tree.iter_keyword_nodes(primary[0]):
        if path == primary or is_prefix(path, primary):
            continue
        if qualify_keyword_path(path, primary, tree, text, text_no_spaces):
            candidates.append(path)
    candidates = remove_ancestor_candidates(candidates)

    descendants = [path for path in candidates if is_prefix(primary, path)]
    divergent = [path for path in candidates if not is_prefix(primary, path)]
    paths = remove_ancestor_candidates((descendants or [primary]) + divergent)
    basis_by_path = {}
    for path in paths:
        if path == primary:
            if inherited_legacy_level1:
                basis_by_path[path] = "legacy-level1"
            else:
                basis_by_path[path] = (
                    "heading-fallback" if tree.has_children(primary) else "heading-exact"
                )
        else:
            basis_by_path[path] = (
                "keyword-refinement" if is_prefix(primary, path) else "keyword-supplement"
            )

    needs_review = bool(manual_issues)
    issues = list(manual_issues)
    if paths == [primary] and tree.has_children(primary):
        needs_review = True
        issues.append(f"Stopped at parent category: {' > '.join(primary)}")

    return {
        "paths": paths,
        "primary_path": primary,
        "basis_by_path": basis_by_path,
        "needs_review": needs_review,
        "issues": issues,
        "content": clean_content,
        "legacy_level1_hint": inherited_legacy_level1,
    }


def is_meaningless_content(content):
    body = re.sub(r"^#{1,6}\s+.*$", "", content, flags=re.MULTILINE)
    body = SOURCE_ID_RE.sub("", body)
    body = re.sub(r"[-*_\s]", "", body)
    return not body


def is_document_preamble(unit):
    if unit.heading_path:
        return False
    meaningful = []
    for raw_line in unit.content.splitlines():
        line = raw_line.strip()
        if not line or line in {"---", "***", "___"}:
            continue
        if re.match(r"^\*\*(Date|날짜)\*\*\s*:", line, re.IGNORECASE):
            continue
        if re.match(r"^(date|created|modified)\s*:", line, re.IGNORECASE):
            continue
        meaningful.append(line)
    return not meaningful


def extract_date_prefix(log_name):
    _iso_date, prefix = parse_stem_date(log_name)
    if prefix:
        return prefix
    match = re.match(
        r"^(\d{4}[-._]\d{2}[-._]\d{2}|\d{8}|\d{2}\.\d{2}\.\d{2}|\d{6})",
        log_name,
    )
    return match.group(1) if match else None














LOCAL_DOC_LINK_RE = re.compile(
    r"(?<!!)\[([^\]\n]+)\]\((<[^>\n]+>(?:\s+[\"'][^\"'\n]*[\"'])?|(?:[^()\n]|\([^()\n]*\))+)\)"
)


def rewrite_image_links_for_generated_document(content, source_filepath, generated_rel_path, vault_dir):
    from urllib.parse import unquote

    generated_filepath = os.path.join(vault_dir, generated_rel_path.replace("/", os.sep))
    generated_dir = os.path.dirname(os.path.realpath(generated_filepath))
    vault_root = os.path.realpath(os.path.abspath(vault_dir))
    source_dir = os.path.dirname(os.path.realpath(os.path.abspath(source_filepath)))

    def replace_image(match):
        raw_target = match.group(1) or match.group(3)
        target = _normalize_image_target(raw_target)
        if not target or urlsplit(target).scheme:
            return match.group(0)

        image_path = _resolve_source_image_path(raw_target, source_filepath, vault_dir)
        if not image_path:
            raise RuntimeError(
                f"Local image was not found while generating {generated_rel_path}: {raw_target}"
            )
        relative_target = normalize_rel_path(os.path.relpath(image_path, generated_dir))
        encoded_target = quote(relative_target, safe="/-._~")
        alt = escape_markdown_link_label(match.group(2) or "")
        return f"![{alt}]({encoded_target})"

    def replace_doc_link(match):
        raw_target = unquote(strip_markdown_target_title(match.group(2)))
        path_part = raw_target.split("#", 1)[0].strip()
        if not path_part or urlsplit(path_part).scheme or os.path.isabs(path_part):
            return match.group(0)
        for base in (source_dir, vault_root):
            candidate = os.path.realpath(os.path.join(base, path_part))
            if _is_within_directory(candidate, vault_root) and os.path.isfile(candidate):
                relative = normalize_rel_path(os.path.relpath(candidate, generated_dir))
                url = quote(relative, safe="/-._~")
                if "#" in raw_target:
                    url += "#" + quote(raw_target.split("#", 1)[1], safe="-._~")
                return f"[{escape_markdown_link_label(match.group(1))}]({url})"
        return match.group(0)

    rewritten_lines = []
    for line, in_code in source_lines_with_code_context(content, keepends=True):
        if in_code:
            rewritten_lines.append(line)
            continue
        rewritten = IMAGE_TOKEN_RE.sub(replace_image, line)
        rewritten = LOCAL_DOC_LINK_RE.sub(replace_doc_link, rewritten)
        rewritten_lines.append(rewritten)
    return "".join(rewritten_lines)


def load_metadata(path):
    metadata = load_json_state(
        path,
        missing_default={},
        expected_type=dict,
        label="classification metadata",
    )
    if "entries" in metadata and not isinstance(metadata["entries"], dict):
        raise StateFileError(
            f"Invalid classification metadata {path}: 'entries' must be an object."
        )
    return metadata


def build_id_indexes(metadata):
    by_anchor = {}
    by_heading = defaultdict(list)
    entries = metadata.get("entries", {}) if isinstance(metadata, dict) else {}
    for source_id, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        anchor = entry.get("source_anchor")
        if anchor:
            by_anchor[anchor] = source_id
        source_path = entry.get("source_path", "")
        heading = normalize_name(entry.get("source_heading", ""))
        if source_path and heading:
            by_heading[(source_path, heading)].append(source_id)
    return by_anchor, by_heading


def build_legacy_level1_hints(metadata, tree):
    hints = defaultdict(set)
    entries = metadata.get("entries", {}) if isinstance(metadata, dict) else {}
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        source_path = normalize_rel_path(entry.get("source_path", ""))
        heading = normalize_name(entry.get("source_heading", ""))
        if not source_path or not heading:
            continue
        for generated_path in entry.get("generated_paths", []):
            parts = [part for part in normalize_rel_path(generated_path).split("/") if part]
            if len(parts) < 3 or parts[0].casefold() != "subject":
                continue
            matched_root = tree.match_child((), parts[1])
            if matched_root:
                hints[(source_path, heading)].add(matched_root[0])
    return hints


def resolve_legacy_level1_hint(source_rel_path, unit, hints):
    candidates = set()
    for heading in reversed(unit.heading_path):
        candidates.update(hints.get((source_rel_path, normalize_name(heading)), set()))
        if len(candidates) == 1:
            return next(iter(candidates))
        if len(candidates) > 1:
            return None
    candidates.update(hints.get((source_rel_path, normalize_name(unit.title)), set()))
    return next(iter(candidates)) if len(candidates) == 1 else None


def make_source_anchor(source_rel_path, unit):
    heading_key = "/".join(normalize_name(value) for value in unit.heading_path)
    return f"{normalize_rel_path(source_rel_path)}::{heading_key}::{unit.occurrence}"


def source_unit_fingerprint(unit):
    return hashlib.sha256(unit.content.encode("utf-8")).hexdigest()


def resolve_source_id(
    source_rel_path,
    unit,
    content,
    new_indexes,
    legacy_indexes,
    used_ids,
    allow_legacy_heading=True,
):
    marker = next(iter_active_source_id_matches(content), None)
    if marker:
        source_id = marker.group(1).lower()
    else:
        anchor = make_source_anchor(source_rel_path, unit)
        source_id = new_indexes[0].get(anchor)
        if not source_id and allow_legacy_heading:
            legacy_candidates = legacy_indexes[1].get(
                (source_rel_path, normalize_name(unit.title)), []
            )
            if len(legacy_candidates) == 1 and legacy_candidates[0] not in used_ids:
                source_id = legacy_candidates[0]
        if not source_id:
            source_id = hashlib.sha1(anchor.encode("utf-8")).hexdigest()[:16]

    if source_id in used_ids and marker:
        raise RuntimeError(
            f"Duplicate permanent Source ID '{source_id}' in "
            f"{source_rel_path}:{unit.start_line}."
        )
    if source_id in used_ids:
        anchor = make_source_anchor(source_rel_path, unit)
        source_id = hashlib.sha1(f"{anchor}::{source_id}".encode("utf-8")).hexdigest()[:16]
    used_ids.add(source_id)
    return source_id


def find_frontmatter_end(lines):
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return index
    return None


def source_id_comment(source_id):
    return f"<!-- {SOURCE_ID_MARKER_NAME}: {source_id.lower()} -->"


def unit_source_id_markers(unit):
    markers = []
    for offset, (line, in_code) in enumerate(source_lines_with_code_context("\n".join(unit.lines))):
        if in_code or is_markdown_blockquote_line(line):
            continue
        for match in SOURCE_ID_RE.finditer(line):
            markers.append((unit.start_line + offset, match.group(1).lower()))
    return markers


def find_source_id_relocations(units, tree):
    """Move an empty parent's ID into its newly recognized child unit.

    A heading correction can turn an H3-H5 heading from ordinary body structure
    into a classified child. If the preceding parent then contains only its
    heading and one Source ID marker, that ID belongs to the child content that
    was previously part of the parent unit.
    """
    relocations = []
    for parent, child in zip(units, units[1:]):
        parent_markers = unit_source_id_markers(parent)
        if len(parent_markers) != 1 or unit_source_id_markers(child):
            continue
        parent_classification = classify_unit(parent, tree)
        child_classification = classify_unit(child, tree)
        if not is_meaningless_content(parent_classification["content"]):
            continue
        if is_meaningless_content(child_classification["content"]):
            continue
        is_entry_child = (
            child.category_path == parent.category_path
            and bool(child.lines)
            and ENTRY_HEADING_RE.match(child.lines[0]) is not None
        )
        if (
            not parent.category_path
            or not is_prefix(parent.category_path, child.category_path)
            or (len(child.category_path) <= len(parent.category_path) and not is_entry_child)
        ):
            continue
        source_line, source_id = parent_markers[0]
        relocations.append((source_line, child.start_line, source_id))
    return relocations


def normalize_source_id_comments(content):
    changed = 0

    def replace(match):
        nonlocal changed
        replacement = source_id_comment(match.group(1))
        if match.group(0) != replacement:
            changed += 1
        return replacement

    return replace_active_source_id_comments(content, replace), changed


def _find_Empty_parent_category_line(lines, source_line, target_line):
    found_line = None
    found_match = None
    for line_num in range(source_line + 1, target_line):
        raw = lines[line_num - 1]
        stripped = raw.strip()
        if not stripped:
            continue
        match = CATEGORY_LINE_RE.match(stripped)
        if match and found_line is None:
            found_line = line_num
            found_match = match
            continue
        return None, None
    return found_line, found_match


def _find_Immediate_category_line(lines, after_line, max_line):
    for line_num in range(after_line + 1, min(len(lines), max_line) + 1):
        stripped = lines[line_num - 1].strip()
        if not stripped or SOURCE_ID_RE.fullmatch(stripped):
            continue
        match = CATEGORY_LINE_RE.match(stripped)
        if match:
            return line_num, match
        break
    return None, None


def render_source_id_markers(original_content, markers, relocations=(), consolidations=()):
    newline = "\r\n" if "\r\n" in original_content else "\n"
    had_final_newline = original_content.endswith(("\n", "\r"))
    normalized_content, normalized_count = normalize_source_id_comments(original_content)
    lines = normalized_content.splitlines()
    removed_line_numbers = set()
    relocated_ids = {
        source_id.lower() for _source_line, _target_line, source_id in relocations
    }
    removal_lines = {source_line for source_line, _, _ in relocations}
    category_removal_lines = set()
    category_insertions = {}

    for source_line, target_line, _source_id in relocations:
        cat_line, cat_match = _find_Empty_parent_category_line(lines, source_line, target_line)
        if cat_line is None:
            continue
        category_removal_lines.add(cat_line)
        child_cat_line, _ = _find_Immediate_category_line(lines, target_line, len(lines))
        if child_cat_line is not None:
            continue
        raw_items = [item.strip() for item in cat_match.group(2).split(",") if item.strip()]
        target_heading = ANY_UNIT_HEADING_RE.match(lines[target_line - 1]) if 0 <= target_line - 1 < len(lines) else None
        target_name = normalize_name(target_heading.group(2)) if target_heading else ""
        if len(raw_items) == 1:
            parts = [p.strip() for p in re.split(r"[/>]", raw_items[0]) if p.strip()]
            if parts and normalize_name(parts[-1]) == target_name:
                continue
        if raw_items:
            category_insertions[target_line] = f"Category: {', '.join(raw_items)}"

    consolidation_targets = {start for start, _, _ in consolidations}
    for start, _keep, found in consolidations:
        removal_lines.update(line for line, _source_id in found)
        relocated_ids.update(source_id for _line, source_id in found)
        candidate_anchors = sorted({start, *(line for line, _ in found)})
        max_anchor = candidate_anchors[-1] + 4
        cat_entries = []
        seen_cat_lines = set()
        for anchor in candidate_anchors:
            cat_line, cat_match = _find_Immediate_category_line(lines, anchor, max_anchor)
            if cat_line is not None and cat_line not in seen_cat_lines:
                seen_cat_lines.add(cat_line)
                cat_entries.append((cat_line, cat_match))
        if len(cat_entries) > 1 or (
            len(cat_entries) == 1 and cat_entries[0][0] > candidate_anchors[0] + 2
        ):
            merged_items = []
            for cat_line, cat_match in cat_entries:
                category_removal_lines.add(cat_line)
                for item in cat_match.group(2).split(","):
                    cleaned = item.strip()
                    if cleaned and cleaned not in merged_items:
                        merged_items.append(cleaned)
            if merged_items:
                category_insertions[start] = f"Category: {', '.join(merged_items)}"

    for source_line in sorted(removal_lines | category_removal_lines, reverse=True):
        source_index = source_line - 1
        if source_index < 0 or source_index >= len(lines):
            raise RuntimeError(f"Invalid Source ID relocation line {source_line}.")
        if source_line in category_removal_lines and source_line not in removal_lines:
            del lines[source_index]
            removed_line_numbers.add(source_line)
            continue
        lines[source_index] = SOURCE_ID_RE.sub("", lines[source_index])
        if not lines[source_index].strip():
            del lines[source_index]
            removed_line_numbers.add(source_line)
    existing_ids = {
        match.group(1).lower()
        for match in iter_active_source_id_matches(original_content)
    }
    existing_ids.difference_update(relocated_ids)
    frontmatter_end = find_frontmatter_end(lines)
    pending = []

    for line_number, source_id in markers:
        source_id = source_id.lower()
        if source_id in existing_ids:
            continue
        start_index = line_number - 1 - sum(
            removed_line < line_number for removed_line in removed_line_numbers
        )
        if start_index < 0 or start_index > len(lines):
            raise RuntimeError(
                f"Invalid Source ID insertion line {line_number}."
            )
        is_heading = (
            start_index < len(lines)
            and ANY_UNIT_HEADING_RE.match(lines[start_index]) is not None
        )
        if is_heading:
            insert_index = start_index + 1
            placement = "heading"
        else:
            insert_index = start_index
            if frontmatter_end is not None and insert_index <= frontmatter_end:
                insert_index = frontmatter_end + 1
            placement = "document-root"
        while (
            line_number not in consolidation_targets
            and insert_index < len(lines) and not lines[insert_index].strip()
        ):
            insert_index += 1
        pending.append((insert_index, source_id, placement, category_insertions.get(line_number)))
        existing_ids.add(source_id)

    for insert_index, source_id, _placement, category_line in sorted(pending, reverse=True):
        if category_line:
            lines.insert(insert_index, category_line)
        lines.insert(insert_index, source_id_comment(source_id))

    updated = newline.join(lines)
    if had_final_newline:
        updated += newline
    heading_count = sum(placement == "heading" for _, _, placement, _ in pending)
    document_root_count = len(pending) - heading_count
    return updated, len(pending), heading_count, document_root_count, normalized_count


def atomic_replace_text(filepath, content, stat_source=None):
    filepath = filesystem_path(filepath)
    if stat_source:
        stat_source = filesystem_path(stat_source)
    directory = os.path.dirname(filepath)
    descriptor, temp_path = tempfile.mkstemp(
        prefix=".research-notes-source-id-",
        suffix=".tmp",
        dir=directory,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file_obj:
            file_obj.write(content)
        if stat_source and os.path.exists(stat_source):
            shutil.copystat(stat_source, temp_path)
        os.replace(temp_path, filepath)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def build_source_marker_plans(
    daily_logs_dir,
    vault_dir,
    tree,
    new_indexes,
    legacy_indexes,
    allow_legacy_heading=True,
):
    plans = []
    used_ids = set()
    marker_owners = {}
    existing_count = 0

    for root, dirnames, filenames in os.walk(daily_logs_dir):
        dirnames.sort()
        for filename in sorted(filenames):
            if not filename.casefold().endswith(".md"):
                continue
            filepath = os.path.join(root, filename)
            with open(filepath, "r", encoding="utf-8", newline="") as file_obj:
                original_content = file_obj.read()
            source_rel_path = normalize_rel_path(os.path.relpath(filepath, vault_dir))
            _log_name, units = parse_markdown_into_units(filepath, tree)
            with open(filepath, "r", encoding="utf-8", newline="") as file_obj:
                if file_obj.read() != original_content:
                    raise RuntimeError(
                        f"Source file changed while it was being scanned: {source_rel_path}"
                    )

            relocations = find_source_id_relocations(units, tree)
            # Check every original ID before discarding any of them. An ID shared
            # by different units/files must never disappear through consolidation.
            for unit in units:
                owner = (source_rel_path, unit.start_line)
                for _line, source_id in unit_source_id_markers(unit):
                    if source_id in marker_owners and marker_owners[source_id] != owner:
                        raise RuntimeError(
                            f"Duplicate permanent Source ID '{source_id}' in "
                            f"{source_rel_path}:{unit.start_line}"
                        )
                    marker_owners[source_id] = owner
            relocated_targets = {
                target_line: source_id
                for _source_line, target_line, source_id in relocations
            }
            markers = []
            consolidations = []
            heading_count = 0
            document_root_count = 0
            for unit in units:
                if is_document_preamble(unit):
                    continue
                classification = classify_unit(unit, tree)
                if is_meaningless_content(classification["content"]):
                    continue
                found_markers = unit_source_id_markers(unit)
                if len(found_markers) > 1:
                    next_line = unit.start_line + 1
                    for offset, line in enumerate(unit.lines[1:], 1):
                        if line.strip():
                            next_line = unit.start_line + offset
                            break
                    keep = found_markers[0][1]
                    if unit.lines and ANY_UNIT_HEADING_RE.match(unit.lines[0]):
                        keep = next((source_id for line, source_id in found_markers
                                     if line == next_line), keep)
                    consolidations.append((unit.start_line, keep, found_markers))
                    identity_content = source_id_comment(keep) + "\n" + replace_active_source_id_comments(unit.content, "")
                else:
                    identity_content = unit.content
                relocated_id = relocated_targets.get(unit.start_line)
                if relocated_id:
                    resolve_source_id(source_rel_path, unit, source_id_comment(relocated_id),
                                      new_indexes, legacy_indexes, used_ids,
                                      allow_legacy_heading=allow_legacy_heading)
                    markers.append((unit.start_line, relocated_id))
                    existing_count += 1
                    first_line = unit.content.splitlines()[0] if unit.content else ""
                    if ANY_UNIT_HEADING_RE.match(first_line):
                        heading_count += 1
                    else:
                        document_root_count += 1
                    continue
                source_id = resolve_source_id(
                    source_rel_path,
                    unit,
                    identity_content,
                    new_indexes,
                    legacy_indexes,
                    used_ids,
                    allow_legacy_heading=allow_legacy_heading,
                )
                if found_markers and len(found_markers) == 1:
                    existing_count += 1
                    continue
                if found_markers:
                    existing_count += 1
                markers.append((unit.start_line, source_id))
                first_line = unit.content.splitlines()[0] if unit.content else ""
                if ANY_UNIT_HEADING_RE.match(first_line):
                    heading_count += 1
                else:
                    document_root_count += 1

            _normalized_content, normalized_marker_count = normalize_source_id_comments(
                original_content
            )
            if markers or normalized_marker_count:
                plans.append(
                    SourceMarkerPlan(
                        filepath=filepath,
                        source_rel_path=source_rel_path,
                        original_content=original_content,
                        markers=markers,
                        heading_count=heading_count,
                        document_root_count=document_root_count,
                        normalized_marker_count=normalized_marker_count,
                        relocations=relocations,
                        consolidations=consolidations,
                    )
                )
    return plans, existing_count


def resolve_backup_retention_limit(cli_limit=None, script_dir=None):
    """Return max backup batches to keep (0 = unlimited/off by default)."""
    if cli_limit is not None:
        return max(0, int(cli_limit))
    if script_dir is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    settings_path = os.path.join(script_dir, ".runtime", "tray_settings.json")
    if not os.path.isfile(settings_path):
        return 0
    try:
        with open(settings_path, "r", encoding="utf-8") as file_obj:
            data = json.load(file_obj)
        if isinstance(data, dict) and bool(data.get("prune_backups", False)):
            count = int(data.get("backup_retention_count", 100))
            return max(1, min(10000, count))
    except Exception:
        pass
    return 0


def prune_backup_batches(category_backup_dir, max_batches):
    """Remove oldest timestamped backup batch folders exceeding max_batches."""
    if not max_batches or max_batches <= 0:
        return 0
    fs_dir = filesystem_path(category_backup_dir)
    if not os.path.isdir(fs_dir):
        return 0
    try:
        entries = sorted(
            name
            for name in os.listdir(fs_dir)
            if os.path.isdir(filesystem_path(os.path.join(category_backup_dir, name)))
        )
    except OSError:
        return 0
    if len(entries) <= max_batches:
        return 0
    removed = 0
    for old_name in entries[: len(entries) - max_batches]:
        old_path = filesystem_path(os.path.join(category_backup_dir, old_name))
        try:
            shutil.rmtree(old_path)
            removed += 1
        except OSError:
            pass
    return removed


def persist_source_marker_plans(plans, backup_root, vault_dir, dry_run=False, max_backup_batches=None):
    total = sum(len(plan.markers) for plan in plans)
    heading_count = sum(plan.heading_count for plan in plans)
    document_root_count = sum(plan.document_root_count for plan in plans)
    normalized_count = sum(plan.normalized_marker_count for plan in plans)
    if dry_run or not plans:
        return total, heading_count, document_root_count, normalized_count, None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    batch_backup_root = os.path.join(backup_root, "automatic", timestamp)
    prepared = []
    committed = []
    stage = "prepare"
    current_source = "(none)"
    try:
        for plan in plans:
            current_source = plan.source_rel_path
            stage = "render Source ID markers"
            (
                updated,
                inserted,
                rendered_headings,
                rendered_roots,
                rendered_normalized,
            ) = render_source_id_markers(
                plan.original_content, plan.markers, plan.relocations, plan.consolidations
            )
            if (
                inserted != len(plan.markers)
                or rendered_headings != plan.heading_count
                or rendered_roots != plan.document_root_count
                or rendered_normalized != plan.normalized_marker_count
            ):
                raise RuntimeError(
                    f"Source ID insertion plan changed unexpectedly: {plan.source_rel_path}"
                )
            backup_path = os.path.join(
                batch_backup_root,
                os.path.relpath(plan.filepath, vault_dir),
            )
            stage = "back up original"
            os.makedirs(filesystem_path(os.path.dirname(backup_path)), exist_ok=True)
            shutil.copy2(filesystem_path(plan.filepath), filesystem_path(backup_path))

            stage = "prepare replacement"
            descriptor, temp_path = tempfile.mkstemp(
                prefix=".research-notes-source-id-",
                suffix=".tmp",
                dir=filesystem_path(os.path.dirname(plan.filepath)),
            )
            prepared.append((plan, temp_path, backup_path))
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file_obj:
                file_obj.write(updated)
            shutil.copystat(filesystem_path(plan.filepath), temp_path)

        for plan, temp_path, backup_path in prepared:
            current_source = plan.source_rel_path
            stage = "check original for concurrent changes"
            with open(filesystem_path(plan.filepath), "r", encoding="utf-8", newline="") as file_obj:
                current_content = file_obj.read()
            if current_content != plan.original_content:
                raise RuntimeError(
                    f"Source file changed before Source IDs were saved: "
                    f"{plan.source_rel_path}"
                )
            stage = "replace original"
            os.replace(temp_path, filesystem_path(plan.filepath))
            committed.append((plan, backup_path))

    except Exception as exc:
        rollback_failures = []
        for plan, backup_path in reversed(committed):
            try:
                atomic_replace_text(
                    plan.filepath,
                    plan.original_content,
                    stat_source=backup_path,
                )
            except Exception as rollback_exc:
                rollback_failures.append(
                    f"{plan.source_rel_path}: {rollback_exc}"
                )
        for _plan, temp_path, _backup_path in prepared:
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except OSError:
                pass
        rollback_note = (
            f" Rollback failures: {'; '.join(rollback_failures)}"
            if rollback_failures
            else ""
        )
        raise RuntimeError(
            f"Failed to persist Source ID markers. File: {current_source}; "
            f"stage: {stage}; {type(exc).__name__}: {exc}.{rollback_note}"
        ) from exc

    limit = resolve_backup_retention_limit(max_backup_batches)
    if limit > 0:
        prune_backup_batches(os.path.join(backup_root, "automatic"), limit)

    return total, heading_count, document_root_count, normalized_count, batch_backup_root


def rebuild_source_id_markers(daily_logs_dir, vault_dir, backup_root, max_backup_batches=None):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    batch_backup_root = os.path.join(backup_root, "rebuild", timestamp)
    changed = 0
    try:
        for root, dirnames, filenames in os.walk(daily_logs_dir):
            dirnames.sort()
            for filename in sorted(filenames):
                if not filename.casefold().endswith(".md"):
                    continue
                filepath = os.path.join(root, filename)
                with open(filepath, "r", encoding="utf-8", newline="") as file_obj:
                    original_content = file_obj.read()
                rebuilt_content = replace_active_source_id_comments(original_content, "")
                if rebuilt_content == original_content:
                    continue
                relative_path = os.path.relpath(filepath, vault_dir)
                backup_path = os.path.join(batch_backup_root, relative_path)
                os.makedirs(filesystem_path(os.path.dirname(backup_path)), exist_ok=True)
                shutil.copy2(filesystem_path(filepath), filesystem_path(backup_path))
                atomic_replace_text(filepath, rebuilt_content, stat_source=backup_path)
                changed += 1
    except Exception:
        if os.path.isdir(filesystem_path(batch_backup_root)):
            for root, _dirnames, filenames in os.walk(filesystem_path(batch_backup_root)):
                for filename in filenames:
                    backup_path = os.path.join(root, filename)
                    relative_path = os.path.relpath(backup_path, filesystem_path(batch_backup_root))
                    with open(backup_path, "r", encoding="utf-8", newline="") as file_obj:
                        backup_content = file_obj.read()
                    atomic_replace_text(
                        os.path.join(vault_dir, relative_path),
                        backup_content,
                        stat_source=backup_path,
                    )
        raise
    limit = resolve_backup_retention_limit(max_backup_batches)
    if limit > 0:
        prune_backup_batches(os.path.join(backup_root, "rebuild"), limit)
    return changed, batch_backup_root


def make_target_filename(log_name, title, source_id, claimed_paths, category_path):
    date_prefix = extract_date_prefix(log_name)
    safe_title = sanitize_filename(title)
    if date_prefix and safe_title.startswith(date_prefix):
        filename = f"{safe_title}.md"
    elif date_prefix:
        filename = f"{date_prefix} - {safe_title}.md"
    else:
        filename = f"{sanitize_filename(log_name)} - {safe_title}.md"

    key = (tuple(category_path), filename.casefold())
    if key in claimed_paths and claimed_paths[key] != source_id:
        stem, extension = os.path.splitext(filename)
        filename = f"{stem} [{source_id[:8]}]{extension}"
        key = (tuple(category_path), filename.casefold())
    claimed_paths[key] = source_id
    return filename


def build_subject_content(
    log_name,
    source_rel_path,
    unit,
    source_id,
    classification,
    path,
    generated_rel_path,
    vault_dir,
):
    source_link = make_markdown_link(generated_rel_path, source_rel_path, log_name)
    heading_path = " > ".join(unit.heading_path) or unit.title
    primary_path = " > ".join(classification["primary_path"]) or "(none)"
    content = rewrite_image_links_for_generated_document(
        classification["content"],
        os.path.join(vault_dir, source_rel_path.replace("/", os.sep)),
        generated_rel_path,
        vault_dir,
    )
    lines = [
        AUTO_GENERATED_SUBJECT + "\n",
        f"**Date/Log**: {source_link}\n",
        f"**Source ID**: `{source_id}`\n",
        f"**Source Path**: `{source_rel_path}`\n",
        f"**Source Heading**: {unit.title}\n",
        f"**Source Heading Path**: {heading_path}\n",
        f"**Primary Category**: {primary_path}\n",
        f"**Category**: {' > '.join(path)}\n",
        f"**Classification Basis**: {classification['basis_by_path'][path]}\n",
        f"**Needs Review**: {'true' if classification['needs_review'] else 'false'}\n\n",
        "---\n\n",
        content.rstrip() + "\n",
    ]
    return "".join(lines)




def write_reviews(vault_dir, review_dir_name, generated_records, dry_run=False):
    from review_renderer import write_reviews as render_reviews
    return render_reviews(vault_dir, review_dir_name, generated_records, dry_run=dry_run)




def write_report(report_path, stats, issues, dry_run):
    lines = [
        "# Hierarchical Classification Report\n\n",
        f"- Mode: {'dry-run' if dry_run else 'write'}\n",
        f"- Generated: {datetime.now().isoformat(timespec='seconds')}\n",
        f"- Source files: {stats['source_files']}\n",
        f"- Source units: {stats['source_units']}\n",
        f"- Generated subject files: {stats['generated_files']}\n",
        f"- Keyword refinements: {stats['keyword_refinements']}\n",
        f"- Keyword supplements: {stats['keyword_supplements']}\n",
        f"- Legacy Level 1 hints: {stats['legacy_level1_hints']}\n",
        f"- Needs review: {stats['needs_review']}\n",
        f"- Missing Level 1: {stats['missing_level1']}\n\n",
        f"- Existing Source ID markers: {stats['source_id_markers_existing']}\n",
        f"- Source ID markers inserted: {stats['source_id_markers']}\n",
        f"- Source ID markers normalized: {stats['source_id_markers_normalized']}\n",
        f"- Source units consolidated: {stats.get('source_id_units_consolidated', 0)}\n",
        f"- Extra Source ID markers removed: {stats.get('source_id_markers_removed', 0)}\n",
        f"- Document-root markers inserted: {stats['source_id_document_root_markers']}\n\n",
        "## Review Items\n\n",
    ]
    if issues:
        for issue in issues:
            lines.append(
                f"- `{issue['source_path']}:{issue['line']}` {issue['title']}: "
                f"{'; '.join(issue['issues'])}\n"
            )
    else:
        lines.append("- None\n")

    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as file_obj:
        file_obj.write("".join(lines))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Generate hierarchical Subject and review trees. The default writes to "
            "parallel preview folders; --production explicitly enables production roots."
        )
    )
    parser.add_argument("--vault-dir", default=None)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--review-dir", default=DEFAULT_REVIEW_DIR)
    parser.add_argument("--metadata-file", default=DEFAULT_METADATA_FILE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--validate-rules", action="store_true")
    parser.add_argument("--no-reviews", action="store_true")
    parser.add_argument(
        "--production",
        action="store_true",
        help="Allow the exact Subject/Topic_Reviews/organizer_metadata.json production targets.",
    )
    source_id_group = parser.add_mutually_exclusive_group()
    source_id_group.add_argument(
        "--initialize-source-ids",
        action="store_true",
        help=(
            "Persist missing Source ID comments in preview mode too. Production "
            "mode already persists them automatically."
        ),
    )
    source_id_group.add_argument(
        "--no-persist-source-ids",
        action="store_true",
        help=(
            "Emergency read-only mode: do not persist missing Source ID comments. "
            "This weakens identity stability for new source units."
        ),
    )
    parser.add_argument(
        "--rebuild-source-ids",
        action="store_true",
        help=(
            "Back up and remove existing Source ID comments before production "
            "classification, for replacing a source-log corpus."
        ),
    )
    parser.add_argument(
        "--max-backup-batches",
        type=int,
        default=None,
        help="Optional maximum number of backup batches to retain (0 = unlimited).",
    )
    return parser.parse_args(argv)


def validate_output_targets(args):
    output_name = normalize_rel_path(args.output_dir).strip("/")
    review_name = normalize_rel_path(args.review_dir).strip("/")
    metadata_name = os.path.basename(args.metadata_file)

    if args.production:
        expected = (
            PRODUCTION_OUTPUT_DIR,
            PRODUCTION_REVIEW_DIR,
            PRODUCTION_METADATA_FILE,
        )
        actual = (output_name, review_name, metadata_name)
        if actual != expected:
            return (
                "Production mode requires --output-dir Subject, "
                "--review-dir Topic_Reviews, and --metadata-file organizer_metadata.json."
            )
        return ""

    forbidden_roots = {
        PRODUCTION_OUTPUT_DIR.casefold(),
        PRODUCTION_REVIEW_DIR.casefold(),
        "daily_logs",
        "attachments",
    }
    if output_name.casefold() in forbidden_roots or review_name.casefold() in forbidden_roots:
        return "Preview mode must not use production or source roots."
    return ""


def should_persist_source_ids(args):
    return (
        args.production or args.initialize_source_ids
    ) and not args.no_persist_source_ids


def count_daily_markdown_files(daily_logs_dir):
    walk_errors = []

    def record_walk_error(error):
        walk_errors.append(error)

    count = 0
    for _root, _dirnames, filenames in os.walk(
        daily_logs_dir,
        onerror=record_walk_error,
    ):
        count += sum(
            1 for filename in filenames if filename.casefold().endswith(".md")
        )

    if walk_errors:
        details = "; ".join(str(error) for error in walk_errors)
        raise OSError(f"Could not fully scan Daily_Logs: {details}")
    return count


def _main_unlocked(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    args = parse_args(argv)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    vault_dir = os.path.abspath(args.vault_dir) if args.vault_dir else resolve_workspace_dir(script_dir)
    state_dir = resolve_workspace_state_dir(script_dir, vault_dir)
    rules_path = os.path.join(vault_dir, "Classification_Rules.md")
    daily_logs_dir = os.path.join(vault_dir, "Daily_Logs")
    metadata_path = os.path.join(state_dir, args.metadata_file)
    report_path = os.path.join(state_dir, "reports", "hierarchical_classification_report.md")
    source_id_backup_root = os.path.join(state_dir, "source_id_backups")

    target_error = validate_output_targets(args)
    if target_error:
        print(f"Error: {target_error}")
        return 2

    tree = parse_rules_from_markdown(rules_path)
    print(f"Loaded {len(tree.nodes)} category nodes from the inherited table.")
    if tree.errors:
        for error in tree.errors:
            print(f"Rule error: {error}")
        return 2
    if args.validate_rules:
        print(f"Rule validation passed with {len(tree.warnings)} optional-keyword notices.")
        return 0

    if not os.path.isdir(daily_logs_dir):
        print(f"Error: Daily logs directory does not exist: {daily_logs_dir}")
        return 2

    try:
        source_file_count = count_daily_markdown_files(daily_logs_dir)
    except OSError as exc:
        print(f"Error: {exc}")
        print("Existing Subject, Topic_Reviews, and metadata were left unchanged.")
        return 2
    if source_file_count == 0:
        print(f"Error: No Markdown files were found under {daily_logs_dir}")
        print("Existing Subject, Topic_Reviews, and metadata were left unchanged.")
        return 2

    try:
        previous_metadata = load_metadata(metadata_path)
        legacy_metadata = load_metadata(os.path.join(state_dir, "organizer_metadata.json"))
    except StateFileError as exc:
        print(f"Error: {exc}")
        print("Existing Subject, Topic_Reviews, and metadata were left unchanged.")
        return 2
    new_indexes = build_id_indexes(previous_metadata)
    legacy_indexes = build_id_indexes(legacy_metadata)
    legacy_level1_hints = build_legacy_level1_hints(legacy_metadata, tree)
    try:
        previous_schema_version = int(previous_metadata.get("schema_version", 0))
    except (TypeError, ValueError):
        previous_schema_version = 0
    allow_legacy_heading_fallback = previous_schema_version < METADATA_SCHEMA_VERSION
    persist_source_ids = should_persist_source_ids(args) or (
        args.dry_run and not args.no_persist_source_ids
    )

    source_id_marker_count = 0
    source_id_heading_marker_count = 0
    source_id_document_root_marker_count = 0
    source_id_normalized_marker_count = 0
    source_id_existing_marker_count = 0
    source_id_backup_batch = None
    marker_plans = []
    preview_sources = {}
    rebuild_backup_batch = None
    if args.rebuild_source_ids:
        if not args.production:
            print("Error: --rebuild-source-ids requires --production.")
            return 2
        try:
            rebuilt_count, rebuild_backup_batch = rebuild_source_id_markers(
                daily_logs_dir,
                vault_dir,
                source_id_backup_root,
                max_backup_batches=args.max_backup_batches,
            )
            print(f"Rebuilt Source ID markers in {rebuilt_count} source files.")
        except Exception as exc:
            print(f"Error: Source ID rebuild failed: {exc}")
            return 2
    if persist_source_ids:
        try:
            marker_plans, source_id_existing_marker_count = build_source_marker_plans(
                daily_logs_dir,
                vault_dir,
                tree,
                new_indexes,
                legacy_indexes,
                allow_legacy_heading=allow_legacy_heading_fallback,
            )
            if args.dry_run:
                for plan in marker_plans:
                    preview_sources[plan.filepath] = render_source_id_markers(
                        plan.original_content, plan.markers, plan.relocations,
                        plan.consolidations,
                    )[0]
                    for start, keep, found in plan.consolidations:
                        remaining = [source_id for _, source_id in found]
                        remaining.remove(keep)
                        print(f"Would consolidate {plan.source_rel_path}:{start}: "
                              f"keep {keep}; remove {', '.join(remaining)}")
            (
                source_id_marker_count,
                source_id_heading_marker_count,
                source_id_document_root_marker_count,
                source_id_normalized_marker_count,
                source_id_backup_batch,
            ) = persist_source_marker_plans(
                marker_plans,
                source_id_backup_root,
                vault_dir,
                dry_run=args.dry_run,
                max_backup_batches=args.max_backup_batches,
            )
        except Exception as exc:
            print(f"Error: Source ID persistence failed: {exc}")
            return 2

    used_ids = set()
    claimed_paths = {}
    entries = {}
    generated_records = []
    desired_subjects = {}
    issues = []
    stats = {
        "source_files": 0,
        "source_units": 0,
        "generated_files": 0,
        "keyword_refinements": 0,
        "keyword_supplements": 0,
        "legacy_level1_hints": 0,
        "needs_review": 0,
        "missing_level1": 0,
        "source_id_markers_existing": source_id_existing_marker_count,
        "source_id_markers": source_id_marker_count,
        "source_id_heading_markers": source_id_heading_marker_count,
        "source_id_document_root_markers": source_id_document_root_marker_count,
        "source_id_markers_normalized": source_id_normalized_marker_count,
        "source_id_units_consolidated": sum(len(plan.consolidations) for plan in marker_plans),
        "source_id_markers_removed": sum(len(found) - 1 for plan in marker_plans
                                         for _, _, found in plan.consolidations),
    }

    for root, dirnames, filenames in os.walk(daily_logs_dir):
        dirnames.sort()
        for filename in sorted(filenames):
            if not filename.casefold().endswith(".md"):
                continue
            filepath = os.path.join(root, filename)
            source_rel_path = normalize_rel_path(os.path.relpath(filepath, vault_dir))
            log_name, units = parse_markdown_into_units(
                filepath, tree, content=preview_sources.get(filepath)
            )
            stats["source_files"] += 1

            for unit in units:
                if is_document_preamble(unit):
                    continue
                level1_hint = resolve_legacy_level1_hint(
                    source_rel_path, unit, legacy_level1_hints
                )
                classification = classify_unit(unit, tree, level1_hint=level1_hint)
                if is_meaningless_content(classification["content"]):
                    continue
                stats["source_units"] += 1
                if classification.get("legacy_level1_hint"):
                    stats["legacy_level1_hints"] += 1
                source_id = resolve_source_id(
                    source_rel_path,
                    unit,
                    unit.content,
                    new_indexes,
                    legacy_indexes,
                    used_ids,
                    allow_legacy_heading=allow_legacy_heading_fallback,
                )
                source_anchor = make_source_anchor(source_rel_path, unit)
                previous_entry = (
                    previous_metadata.get("entries", {}).get(source_id)
                    if isinstance(previous_metadata, dict)
                    else None
                )
                if isinstance(previous_entry, dict):
                    classification = classify_unit(
                        unit,
                        tree,
                        level1_hint=level1_hint,
                        previous_entry=previous_entry,
                    )

                if classification["needs_review"]:
                    stats["needs_review"] += 1
                    issues.append(
                        {
                            "source_path": source_rel_path,
                            "line": unit.start_line,
                            "title": unit.title,
                            "issues": classification["issues"],
                        }
                    )
                if not classification["primary_path"]:
                    stats["missing_level1"] += 1

                generated_paths = []
                review_blocks = extract_review_blocks(
                    "\n".join(unit.lines), source_rel_path, source_id,
                    start_line=unit.start_line, root_title=unit.title,
                )
                for path in classification["paths"]:
                    if classification["basis_by_path"][path] == "keyword-refinement":
                        stats["keyword_refinements"] += 1
                    elif classification["basis_by_path"][path] == "keyword-supplement":
                        stats["keyword_supplements"] += 1
                    filename_out = make_target_filename(
                        log_name, unit.title, source_id, claimed_paths, path
                    )
                    rel_path = normalize_rel_path(
                        os.path.join(args.output_dir, *path, filename_out)
                    )
                    content = build_subject_content(
                        log_name,
                        source_rel_path,
                        unit,
                        source_id,
                        classification,
                        path,
                        rel_path,
                        vault_dir,
                    )
                    desired_subjects[rel_path] = content
                    generated_paths.append(rel_path)
                    generated_records.append(
                        {
                            "source_id": source_id,
                            "source_path": source_rel_path,
                            "source_log": log_name,
                            "source_line": unit.start_line,
                            "title": unit.title,
                            "category_path": list(path),
                            "generated_path": rel_path,
                            "review_blocks": review_blocks,
                        }
                    )

                has_manual_basis = any(
                    basis == "manual"
                    for basis in classification["basis_by_path"].values()
                )
                manual_heading_path = None
                if has_manual_basis:
                    prev_manual_heading = (
                        previous_entry.get("manual_heading_path")
                        if isinstance(previous_entry, dict)
                        else None
                    )
                    if (
                        isinstance(prev_manual_heading, list)
                        and any(
                            "conflicts with heading category" in issue
                            for issue in classification["issues"]
                        )
                    ):
                        manual_heading_path = prev_manual_heading
                    else:
                        manual_heading_path = list(unit.heading_path)

                entry_meta = extract_entry_metadata(classification["content"])
                entry_record = {
                    "source_path": source_rel_path,
                    "source_log": log_name,
                    "date": source_date_key(source_rel_path),
                    "source_heading": unit.title,
                    "source_heading_path": list(unit.heading_path),
                    "source_line": unit.start_line,
                    "source_anchor": source_anchor,
                    "source_fingerprint": source_unit_fingerprint(unit),
                    "primary_category": list(classification["primary_path"]),
                    "needs_review": classification["needs_review"],
                    "review_reasons": list(classification["issues"]),
                    "matched_categories": [
                        {
                            "path": list(path),
                            "basis": classification["basis_by_path"][path],
                        }
                        for path in classification["paths"]
                    ],
                    "generated_paths": generated_paths,
                    "kind": entry_meta["kind"],
                    "files": entry_meta["files"],
                    "commit": entry_meta["commit"],
                    "summary": entry_meta["summary"],
                    "search_text": entry_meta["search_text"],
                }
                if manual_heading_path is not None:
                    entry_record["manual_heading_path"] = manual_heading_path
                entries[source_id] = entry_record

    stats["generated_files"] = len(desired_subjects)
    if not args.dry_run:
        for rel_path, content in desired_subjects.items():
            full_path = os.path.join(vault_dir, rel_path)
            atomic_write_text(full_path, content)
        deleted = cleanup_generated_root(
            vault_dir, args.output_dir, set(desired_subjects), AUTO_GENERATED_SUBJECT
        )
    else:
        deleted = 0

    review_paths = []
    if not args.no_reviews:
        review_paths = write_reviews(
            vault_dir, args.review_dir, generated_records, dry_run=args.dry_run
        )

    metadata_payload = {
        "schema_version": METADATA_SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "output_dir": args.output_dir,
        "review_dir": args.review_dir,
        "entries": entries,
        "review_paths": review_paths,
        "stats": stats,
    }
    if not args.dry_run:
        atomic_write_json(metadata_path, metadata_payload)
        write_report(report_path, stats, issues, dry_run=False)

    print(f"Source files: {stats['source_files']}")
    print(f"Source units: {stats['source_units']}")
    print(f"Generated subject files: {stats['generated_files']}")
    print(f"Keyword refinements: {stats['keyword_refinements']}")
    print(f"Keyword supplements: {stats['keyword_supplements']}")
    print(f"Legacy Level 1 hints: {stats['legacy_level1_hints']}")
    print(f"Needs review: {stats['needs_review']}")
    print(f"Missing Level 1: {stats['missing_level1']}")
    if persist_source_ids:
        cleanup_action = "would be consolidated" if args.dry_run else "consolidated"
        print(f"Source units {cleanup_action}: {stats['source_id_units_consolidated']}")
        removal_action = "would be removed" if args.dry_run else "removed"
        print(f"Extra Source ID markers {removal_action}: {stats['source_id_markers_removed']}")
        action = "would be inserted" if args.dry_run else "inserted"
        print(f"Existing Source ID markers: {stats['source_id_markers_existing']}")
        print(f"Source ID markers {action}: {stats['source_id_markers']}")
        normalize_action = "would be normalized" if args.dry_run else "normalized"
        print(
            f"Source ID markers {normalize_action}: "
            f"{stats['source_id_markers_normalized']}"
        )
        print(
            f"Document-root Source ID markers {action}: "
            f"{stats['source_id_document_root_markers']}"
        )
        if source_id_backup_batch:
            print(
                "Source ID backups: "
                f"{normalize_rel_path(os.path.relpath(source_id_backup_batch, vault_dir))}"
            )
        if rebuild_backup_batch:
            print(
                "Rebuild backups: "
                f"{normalize_rel_path(os.path.relpath(rebuild_backup_batch, vault_dir))}"
            )
    print(f"Review files: {len(review_paths)}")
    if deleted:
        print(f"Removed stale generated files: {deleted}")
    if args.dry_run:
        print("Dry-run complete. No files were written.")
    else:
        mode = "Production" if args.production else "Preview"
        print(f"{mode} output written to {args.output_dir}/")
    return 0


def main(argv=None):
    args = parse_args(argv)
    if args.validate_rules or lock_is_delegated_by_parent():
        return _main_unlocked(argv)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    vault_dir = os.path.abspath(args.vault_dir) if args.vault_dir else resolve_workspace_dir(script_dir)
    if args.production:
        operation = "classification-production"
    elif args.dry_run:
        operation = "classification-dry-run"
    else:
        operation = "classification-preview"

    try:
        with VaultProcessLock(
            vault_dir,
            operation,
            command=command_text(__file__, argv),
        ):
            return _main_unlocked(argv)
    except ProcessLockUnavailable as exc:
        print_lock_error(exc)
        return LOCK_BUSY_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
