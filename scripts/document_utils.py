"""Shared document paths, links, images, dates, and generated-file cleanup."""

import os
import re
from datetime import date
from urllib.parse import quote, unquote, urlsplit

from atomic_io import filesystem_path


AUTO_GENERATED_SUBJECT = "<!-- AUTO-GENERATED: Log2Topic hierarchical subject. -->"


AUTO_GENERATED_REVIEW = "<!-- AUTO-GENERATED: Log2Topic hierarchical review. -->"


def resolve_document_language(settings_path=None):
    """Return 'en' when English is explicitly configured, otherwise 'ko'."""
    requested = os.environ.get("LOG2TOPIC_LANGUAGE", "Auto")
    if settings_path is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        settings_path = os.path.join(script_dir, ".runtime", "tray_settings.json")
    if requested.casefold() == "auto" and settings_path and os.path.isfile(settings_path):
        try:
            import json

            with open(settings_path, "r", encoding="utf-8") as file_obj:
                data = json.load(file_obj)
            if isinstance(data, dict):
                requested = str(data.get("ui_language", requested))
        except Exception:
            pass
    if requested.casefold() in {"en", "en-us"}:
        return "en"
    return "ko"


def normalize_rel_path(path):
    return path.replace("\\", "/")


def sanitize_filename(name):
    name = re.sub(r'[\\/*?:"<>|]', "_", name or "")
    return name.strip().rstrip(".") or "Untitled"


def is_prefix(prefix, path):
    return len(prefix) <= len(path) and tuple(path[: len(prefix)]) == tuple(prefix)


def escape_markdown_link_label(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def make_markdown_link(from_rel_path, target_rel_path, label=None):
    source_dir = os.path.dirname(normalize_rel_path(from_rel_path)) or "."
    target = normalize_rel_path(target_rel_path)
    relative_target = normalize_rel_path(os.path.relpath(target, source_dir))
    encoded_target = quote(relative_target, safe="/-._~")
    display_text = label or os.path.splitext(os.path.basename(target))[0]
    return f"[{escape_markdown_link_label(display_text)}]({encoded_target})"


IMAGE_TOKEN_RE = re.compile(
    r"!\[\[([^\]]+)\]\]|!\[([^\]\n]*)\]\((<[^>\n]+>(?:\s+[\"'][^\"'\n]*[\"'])?|(?:[^()\n]|\([^()\n]*\))+)\)"
)

BLOCKED_SEARCH_DIRECTORIES = {
    ".git",
    ".obsidian",
    ".agents",
    ".codex",
    ".gemini",
    "scripts",
    "subject",
    "topic_reviews",
    "_generated",
}


def strip_markdown_target_title(raw_target):
    target = str(raw_target or "").strip()
    if target.startswith("<"):
        end = target.find(">")
        if end != -1:
            return target[1:end].strip()
    if not target.casefold().startswith("data:"):
        match = re.match(r'^(.*?)\s+(?:"[^"]*"|\'[^\']*\')\s*$', target)
        if match and match.group(1).strip():
            target = match.group(1).strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    return target


def _normalize_image_target(raw_target):
    target = strip_markdown_target_title(raw_target)
    target = unquote(target)
    target = target.split("#", 1)[0].strip()
    target = target.split("|", 1)[0].strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    return target


def _is_within_directory(path, directory):
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(directory)]) == os.path.realpath(directory)
    except ValueError:
        return False


def _resolve_source_image_path(raw_target, source_filepath, vault_dir):
    target = _normalize_image_target(raw_target)
    if not target or urlsplit(target).scheme:
        return None

    vault_root = os.path.realpath(os.path.abspath(vault_dir))
    source_dir = os.path.dirname(os.path.abspath(source_filepath))
    candidates = []
    if os.path.isabs(target):
        candidates.append(target)
    else:
        candidates.extend(
            (
                os.path.join(source_dir, target),
                os.path.join(vault_root, target),
                os.path.join(vault_root, "attachments", os.path.basename(target)),
            )
        )

    for candidate in candidates:
        resolved = os.path.realpath(os.path.abspath(candidate))
        if _is_within_directory(resolved, vault_root) and os.path.isfile(resolved):
            return resolved

    basename = os.path.basename(target)
    for root, dirs, files in os.walk(vault_root):
        dirs[:] = sorted(
            d for d in dirs if d.casefold() not in BLOCKED_SEARCH_DIRECTORIES
        )
        for filename in sorted(files):
            if filename.casefold() == basename.casefold():
                resolved = os.path.realpath(os.path.join(root, filename))
                if _is_within_directory(resolved, vault_root):
                    return resolved
    return None


def extract_frontmatter_date(content):
    """Extract an explicit ISO date from YAML frontmatter or preamble."""
    if not content:
        return None, None
    lines = str(content).splitlines()
    in_frontmatter = bool(lines and lines[0].strip() == "---")
    limit = min(len(lines), 30 if in_frontmatter else 6)
    for idx in range(1 if in_frontmatter else 0, limit):
        stripped = lines[idx].strip()
        if in_frontmatter and stripped == "---":
            break
        match = re.match(
            r"^(?:\*\*(?:Date|날짜)\*\*|date|created)\s*:\s*['\"]?([^\s'\"#]+)",
            stripped,
            re.IGNORECASE,
        )
        if match:
            iso_date, prefix = parse_stem_date(match.group(1))
            if iso_date:
                return iso_date, prefix
    return None, None


def parse_stem_date(stem):
    """Return (iso_date, matched_prefix) or (None, None) from a filename stem."""
    text = str(stem or "").strip()
    for pattern, short_year in (
        (r"^(\d{4})[-._](\d{2})[-._](\d{2})(?!\d)", False),
        (r"^(\d{4})(\d{2})(\d{2})(?!\d)", False),
        (r"^(\d{2})\.(\d{2})\.(\d{2})(?!\d)", True),
        (r"^(\d{2})-(\d{2})-(\d{2})(?!\d)", True),
        (r"^(\d{2})(\d{2})(\d{2})(?!\d)", True),
    ):
        match = re.match(pattern, text)
        if match:
            year = int(match.group(1)) + (2000 if short_year else 0)
            try:
                return date(year, int(match.group(2)), int(match.group(3))).isoformat(), match.group(0)
            except ValueError:
                return None, None
    return None, None


def source_date_key(value):
    filename = os.path.basename(str(value or ""))
    if not filename.casefold().endswith(".md"):
        return "0000-00-00"
    iso_date, _prefix = parse_stem_date(filename[:-3])
    return iso_date or "0000-00-00"


def cleanup_generated_root(vault_dir, root_name, desired_paths, marker):
    root_path = os.path.join(vault_dir, root_name)
    fs_root_path = filesystem_path(root_path)
    if not os.path.isdir(fs_root_path):
        return 0
    desired = {os.path.normcase(os.path.normpath(path)) for path in desired_paths}
    deleted = 0
    for dirpath, _dirnames, filenames in os.walk(root_path):
        for filename in filenames:
            if not filename.casefold().endswith(".md"):
                continue
            full_path = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(full_path, vault_dir)
            if os.path.normcase(os.path.normpath(rel_path)) in desired:
                continue
            fs_full_path = filesystem_path(full_path)
            try:
                with open(fs_full_path, "r", encoding="utf-8") as file_obj:
                    first_line = file_obj.readline().strip()
                marker_kind = (
                    "hierarchical subject"
                    if marker == AUTO_GENERATED_SUBJECT
                    else "hierarchical review"
                )
                is_managed_marker = (
                    first_line.startswith("<!-- AUTO-GENERATED: ")
                    and first_line.endswith(f" {marker_kind}. -->")
                )
                if first_line == marker or is_managed_marker:
                    os.remove(fs_full_path)
                    deleted += 1
            except OSError:
                pass
    for dirpath, _dirnames, _filenames in os.walk(root_path, topdown=False):
        if dirpath == root_path:
            continue
        fs_dirpath = filesystem_path(dirpath)
        try:
            if not os.listdir(fs_dirpath):
                os.rmdir(fs_dirpath)
        except OSError:
            pass
    return deleted


KIND_META_RE = re.compile(r"^\*\*Kind\*\*\s*:\s*(.+?)\s*$", re.IGNORECASE)
FILES_META_RE = re.compile(r"^\*\*Files\*\*\s*:\s*(.+?)\s*$", re.IGNORECASE)
COMMIT_META_RE = re.compile(r"^\*\*Commit\*\*\s*:\s*(.+?)\s*$", re.IGNORECASE)
INLINE_FILE_PATH_RE = re.compile(r"`([A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,10})`")


def extract_entry_metadata(content):
    """Extract Kind, Files, Commit, Summary, and Search Text from a SourceUnit body."""
    kind = None
    commit = None
    files = []
    seen_files = set()
    body_lines = []

    def add_file(candidate):
        norm = normalize_rel_path(str(candidate or "").strip().strip("`\"'")).lstrip("./")
        if norm and not norm.startswith(("http://", "https://")) and norm.casefold() not in seen_files:
            seen_files.add(norm.casefold())
            files.append(norm)

    fence = None
    for raw_line in str(content or "").splitlines():
        fence_match = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})([^\r\n]*)", raw_line)
        if fence_match:
            marker, suffix = fence_match.groups()
            if fence is None and (marker[0] != "`" or "`" not in suffix):
                fence = marker
                body_lines.append(raw_line)
                continue
            if fence is not None and marker[0] == fence[0] and len(marker) >= len(fence) and not suffix.strip():
                fence = None
                body_lines.append(raw_line)
                continue
        if fence is not None:
            body_lines.append(raw_line)
            continue

        stripped = raw_line.strip()
        if not stripped:
            continue
        if re.match(r"^#{1,6}[ \t]+", stripped) or re.match(r"^<!--.*-->$", stripped):
            continue
        if re.match(r"^(?:Category|분류)\s*:", stripped, re.IGNORECASE):
            continue
        m_kind = KIND_META_RE.match(stripped)
        if m_kind:
            if kind is None:
                kind = m_kind.group(1).strip().strip("`").lower()
            continue
        m_files = FILES_META_RE.match(stripped)
        if m_files:
            for item in m_files.group(1).split(","):
                add_file(item)
            continue
        m_commit = COMMIT_META_RE.match(stripped)
        if m_commit:
            if commit is None:
                commit = m_commit.group(1).strip().strip("`")
            continue

        for match in INLINE_FILE_PATH_RE.finditer(raw_line):
            add_file(match.group(1))
        body_lines.append(stripped)

    clean_text = "\n".join(body_lines).strip()
    single_line = re.sub(r"\s+", " ", clean_text).strip()
    summary = single_line[:240] + ("…" if len(single_line) > 240 else "")
    return {
        "kind": kind,
        "files": files,
        "commit": commit,
        "summary": summary,
        "search_text": clean_text[:4000],
    }
