"""Core review inspection, manual classification override, and session lifecycle helpers."""

from datetime import datetime
from http import HTTPStatus
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse

from atomic_io import atomic_write_json, atomic_write_text as _atomic_write_text, filesystem_path
from hierarchical_classifier import (
    ANY_UNIT_HEADING_RE,
    CATEGORY_LEVELS,
    HEADING_RE,
    METADATA_SCHEMA_VERSION,
    SOURCE_ID_RE,
    iter_active_category_lines,
    make_source_anchor,
    normalize_name,
    normalize_rel_path,
    parse_markdown_into_units,
    parse_rules_from_markdown,
    source_unit_fingerprint,
)
from process_lock import delegated_lock_environment
from workspace_paths import resolve_workspace_dir, resolve_workspace_state_dir


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VAULT_DIR = resolve_workspace_dir(SCRIPT_DIR)
METADATA_PATH = os.path.join(SCRIPT_DIR, "organizer_metadata.json")
RULES_PATH = os.path.join(VAULT_DIR, "Classification_Rules.md")
BACKUP_DIR = os.path.join(SCRIPT_DIR, "source_id_backups", "classification_review")
SOURCE_ID_VALUE_RE = re.compile(r"^[a-fA-F0-9]{8,64}$")
DASHBOARD_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
CATEGORY_LINE_RE = re.compile(r"^(Category|분류)\s*:", re.IGNORECASE)
DASHBOARD_CLOSE_GRACE_SECONDS = 3.0
TRAY_SETTINGS_PATH = os.path.join(SCRIPT_DIR, ".runtime", "tray_settings.json")


def resolve_ui_language(settings_path=TRAY_SETTINGS_PATH):
    requested = os.environ.get("LOG2TOPIC_LANGUAGE", "Auto")
    try:
        with open(settings_path, "r", encoding="utf-8") as file_obj:
            value = json.load(file_obj)
        if requested.casefold() == "auto" and isinstance(value, dict):
            requested = str(value.get("ui_language", requested))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass
    if requested.casefold() in {"ko", "ko-kr"}:
        return "ko"
    if requested.casefold() in {"en", "en-us"}:
        return "en"
    system_locale = locale.getlocale()[0] or ""
    return "ko" if system_locale.casefold().startswith("ko") else "en"


class ReviewError(Exception):
    def __init__(self, message, status=HTTPStatus.BAD_REQUEST, details=None):
        super().__init__(message)
        self.message = message
        self.status = int(status)
        self.details = details or {}


class DashboardLifecycle:
    def __init__(self, close_delay=DASHBOARD_CLOSE_GRACE_SECONDS, busy_check=None):
        self.close_delay = close_delay
        self.busy_check = busy_check or (lambda: False)
        self.server = None
        self.sessions = set()
        self.lock = threading.RLock()
        self.shutdown_timer = None
        self.generation = 0

    def attach(self, server):
        self.server = server

    def open(self, session_id):
        with self.lock:
            self.sessions.add(session_id)
            self.generation += 1
            self._cancel_timer_locked()

    def close(self, session_id):
        with self.lock:
            if session_id not in self.sessions:
                return False
            self.sessions.remove(session_id)
            if not self.sessions:
                self._schedule_shutdown_locked()
            return True

    def stop(self):
        with self.lock:
            self.generation += 1
            self.sessions.clear()
            self._cancel_timer_locked()

    def _cancel_timer_locked(self):
        if self.shutdown_timer is not None:
            self.shutdown_timer.cancel()
            self.shutdown_timer = None

    def _schedule_shutdown_locked(self):
        self.generation += 1
        generation = self.generation
        self._cancel_timer_locked()
        timer = threading.Timer(self.close_delay, self._attempt_shutdown, (generation,))
        timer.daemon = True
        self.shutdown_timer = timer
        timer.start()

    def _attempt_shutdown(self, generation):
        with self.lock:
            if generation != self.generation or self.sessions:
                return
            self.shutdown_timer = None
            if self.busy_check():
                self._schedule_shutdown_locked()
                return
            server = self.server
        if server is not None:
            server.shutdown()


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as file_obj:
            value = json.load(file_obj)
    except FileNotFoundError as exc:
        raise ReviewError(
            "분류 metadata가 없습니다. 먼저 계층형 분류를 실행하세요.",
            HTTPStatus.SERVICE_UNAVAILABLE,
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewError(
            f"분류 metadata를 읽을 수 없습니다: {exc}",
            HTTPStatus.SERVICE_UNAVAILABLE,
        ) from exc
    if not isinstance(value, dict):
        raise ReviewError("분류 metadata 형식이 올바르지 않습니다.", HTTPStatus.SERVICE_UNAVAILABLE)
    return value


def load_review_context(vault_dir=VAULT_DIR, metadata_path=METADATA_PATH, rules_path=RULES_PATH):
    metadata = load_json(metadata_path)
    schema_version = metadata.get("schema_version", 0)
    if schema_version < METADATA_SCHEMA_VERSION:
        raise ReviewError(
            "분류 metadata가 이전 형식입니다. 계층형 분류를 다시 실행하세요.",
            HTTPStatus.CONFLICT,
        )
    tree = parse_rules_from_markdown(rules_path)
    if tree.errors:
        raise ReviewError(
            "Classification_Rules.md에 오류가 있습니다.",
            HTTPStatus.CONFLICT,
            {"errors": tree.errors},
        )
    return metadata, tree


def resolve_source_path(vault_dir, source_rel_path):
    normalized = normalize_rel_path(source_rel_path or "").lstrip("/")
    if not normalized.casefold().startswith("daily_logs/"):
        raise ReviewError("검토 대상 원본은 Daily_Logs 아래에 있어야 합니다.")
    full_path = os.path.realpath(os.path.join(vault_dir, *normalized.split("/")))
    daily_root = os.path.realpath(os.path.join(vault_dir, "Daily_Logs"))
    try:
        is_inside = os.path.commonpath([full_path, daily_root]) == daily_root
    except ValueError:
        is_inside = False
    if not is_inside:
        raise ReviewError("허용되지 않은 원본 경로입니다.")
    if not os.path.isfile(filesystem_path(full_path)):
        raise ReviewError(f"원본 파일을 찾을 수 없습니다: {normalized}", HTTPStatus.NOT_FOUND)
    return full_path, normalized


def locate_source_unit(vault_dir, tree, source_id, entry):
    filepath, source_rel_path = resolve_source_path(vault_dir, entry.get("source_path"))
    _log_name, units = parse_markdown_into_units(filepath, tree)

    marker_matches = []
    for index, unit in enumerate(units):
        markers = SOURCE_ID_RE.finditer(unit.content)
        if any(marker.group(1).casefold() == source_id.casefold() for marker in markers):
            marker_matches.append((index, unit))
    if len(marker_matches) == 1:
        index, unit = marker_matches[0]
        return filepath, source_rel_path, units, index, unit
    if len(marker_matches) > 1:
        raise ReviewError("동일한 Source ID marker가 원본에 중복되어 있습니다.", HTTPStatus.CONFLICT)

    expected_anchor = entry.get("source_anchor", "")
    anchor_matches = [
        (index, unit)
        for index, unit in enumerate(units)
        if make_source_anchor(source_rel_path, unit) == expected_anchor
    ]
    if len(anchor_matches) == 1:
        index, unit = anchor_matches[0]
        return filepath, source_rel_path, units, index, unit

    expected_heading_path = tuple(
        normalize_name(value) for value in entry.get("source_heading_path", [])
    )
    expected_heading = normalize_name(entry.get("source_heading", ""))
    fallback_matches = []
    for index, unit in enumerate(units):
        heading_path = tuple(normalize_name(value) for value in unit.heading_path)
        if heading_path == expected_heading_path and normalize_name(unit.title) == expected_heading:
            fallback_matches.append((index, unit))
    if len(fallback_matches) == 1:
        index, unit = fallback_matches[0]
        return filepath, source_rel_path, units, index, unit

    raise ReviewError(
        "Source ID에 해당하는 원본 구간을 하나로 확정할 수 없습니다. 분류를 다시 실행하세요.",
        HTTPStatus.CONFLICT,
    )


def classify_review_kind(entry):
    reasons = entry.get("review_reasons", [])
    if not entry.get("primary_category"):
        return "missing-level-1"
    if any(
        "Unknown manual category" in reason
        or "conflicts with heading category" in reason
        for reason in reasons
    ):
        return "invalid-manual"
    if any("Stopped at parent category" in reason for reason in reasons):
        return "parent-category"
    return "other"


def clean_source_preview(content):
    return SOURCE_ID_RE.sub("", content).strip()


def build_review_items(vault_dir=VAULT_DIR, metadata_path=METADATA_PATH, rules_path=RULES_PATH):
    metadata, tree = load_review_context(vault_dir, metadata_path, rules_path)
    entries = metadata.get("entries", {})
    if not isinstance(entries, dict):
        raise ReviewError("분류 metadata의 entries 형식이 올바르지 않습니다.", HTTPStatus.CONFLICT)

    items = []
    for source_id, entry in entries.items():
        if not isinstance(entry, dict) or not entry.get("needs_review"):
            continue
        try:
            _filepath, source_rel_path, _units, _index, unit = locate_source_unit(
                vault_dir, tree, source_id, entry
            )
            fingerprint = source_unit_fingerprint(unit)
            current_categories = [
                value.get("path", [])
                for value in entry.get("matched_categories", [])
                if isinstance(value, dict) and value.get("path")
            ]
            has_manual_category = bool(list(iter_active_category_lines(unit.content)))
            items.append(
                {
                    "source_id": source_id,
                    "source_path": source_rel_path,
                    "source_log": entry.get("source_log", ""),
                    "source_heading": entry.get("source_heading", unit.title),
                    "source_heading_path": entry.get("source_heading_path", []),
                    "source_line": unit.start_line,
                    "source_fingerprint": fingerprint,
                    "metadata_stale": fingerprint != entry.get("source_fingerprint"),
                    "primary_category": entry.get("primary_category", []),
                    "current_categories": current_categories,
                    "has_manual_category": has_manual_category,
                    "review_reasons": entry.get("review_reasons", []),
                    "review_kind": classify_review_kind(entry),
                    "content": clean_source_preview(unit.content),
                    "generated_paths": entry.get("generated_paths", []),
                }
            )
        except ReviewError as exc:
            items.append(
                {
                    "source_id": source_id,
                    "source_path": entry.get("source_path", ""),
                    "source_log": entry.get("source_log", ""),
                    "source_heading": entry.get("source_heading", ""),
                    "source_heading_path": entry.get("source_heading_path", []),
                    "source_line": entry.get("source_line", 0),
                    "source_fingerprint": entry.get("source_fingerprint", ""),
                    "metadata_stale": True,
                    "primary_category": entry.get("primary_category", []),
                    "current_categories": [],
                    "has_manual_category": False,
                    "review_reasons": entry.get("review_reasons", []) + [exc.message],
                    "review_kind": "source-error",
                    "content": "",
                    "generated_paths": entry.get("generated_paths", []),
                }
            )
    return sorted(
        items,
        key=lambda item: (
            item["source_path"].casefold(),
            item["source_line"],
            item["source_id"],
        ),
    )


def build_category_list(tree):
    categories = []
    for path, node in sorted(
        tree.nodes.items(),
        key=lambda value: tuple(part.casefold() for part in value[0]),
    ):
        categories.append(
            {
                "path": list(path),
                "label": path[-1],
                "depth": len(path),
                "selectable": True,
                "has_children": tree.has_children(path),
            }
        )
    return categories


def validate_category_paths(tree, raw_paths):
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ReviewError("하나 이상의 분류 경로를 선택하세요.")
    canonical_paths = []
    seen = set()
    for raw_path in raw_paths:
        if not isinstance(raw_path, list) or not 1 <= len(raw_path) <= CATEGORY_LEVELS:
            raise ReviewError("분류 경로 형식이 올바르지 않습니다.")
        if any(not isinstance(value, str) or not value.strip() for value in raw_path):
            raise ReviewError("분류 경로에 빈 단계가 있습니다.")
        resolved = tree.resolve_manual_path([value.strip() for value in raw_path])
        node = tree.nodes.get(resolved) if resolved else None
        if not resolved or not node:
            raise ReviewError(f"Classification_Rules.md에 없는 경로입니다: {' > '.join(raw_path)}")
        if resolved not in seen:
            canonical_paths.append(resolved)
            seen.add(resolved)
    return canonical_paths


def find_existing_category_lines(lines, start_index, end_index):
    unit_slice = "\n".join(lines[start_index:end_index])
    return [start_index + idx for idx, _match in iter_active_category_lines(unit_slice)]


def atomic_write_text(path, content):
    _atomic_write_text(path, content)


def _update_entry_manual_heading_path(metadata, entry, metadata_path, heading_path):
    if not isinstance(entry, dict) or not metadata_path or not os.path.exists(metadata_path):
        return
    if heading_path is None:
        if "manual_heading_path" not in entry:
            return
        entry.pop("manual_heading_path", None)
    else:
        if entry.get("manual_heading_path") == list(heading_path):
            return
        entry["manual_heading_path"] = list(heading_path)
    try:
        atomic_write_json(metadata_path, metadata)
    except OSError:
        pass


def apply_manual_categories(
    source_id,
    raw_category_paths,
    expected_fingerprint,
    vault_dir=VAULT_DIR,
    metadata_path=METADATA_PATH,
    rules_path=RULES_PATH,
    backup_dir=None,
):
    if backup_dir is None:
        backup_dir = os.path.join(
            resolve_workspace_state_dir(SCRIPT_DIR, vault_dir),
            "source_id_backups",
            "classification_review",
        )
    if not isinstance(source_id, str) or not SOURCE_ID_VALUE_RE.fullmatch(source_id):
        raise ReviewError("Source ID 형식이 올바르지 않습니다.")
    if not isinstance(expected_fingerprint, str) or not expected_fingerprint:
        raise ReviewError("원본 fingerprint가 없습니다. 화면을 새로 고치세요.")

    metadata, tree = load_review_context(vault_dir, metadata_path, rules_path)
    entry = metadata.get("entries", {}).get(source_id)
    if not isinstance(entry, dict):
        raise ReviewError("Source ID가 최신 metadata에 없습니다.", HTTPStatus.NOT_FOUND)
    canonical_paths = validate_category_paths(tree, raw_category_paths)
    filepath, source_rel_path, units, unit_index, unit = locate_source_unit(
        vault_dir, tree, source_id, entry
    )
    current_fingerprint = source_unit_fingerprint(unit)
    if current_fingerprint != expected_fingerprint:
        raise ReviewError(
            "원본 일지가 화면을 연 뒤 변경되었습니다. 새로 고친 후 다시 적용하세요.",
            HTTPStatus.CONFLICT,
        )

    with open(filesystem_path(filepath), "r", encoding="utf-8", newline="") as file_obj:
        original = file_obj.read()
    newline = "\r\n" if "\r\n" in original else "\n"
    had_final_newline = original.endswith(("\n", "\r"))
    lines = original.splitlines()
    start_index = unit.start_line - 1
    end_index = units[unit_index + 1].start_line - 1 if unit_index + 1 < len(units) else len(lines)
    category_line = "Category: " + ", ".join("/".join(path) for path in canonical_paths)
    existing_indices = find_existing_category_lines(lines, start_index, end_index)

    frontmatter_end = None
    if start_index == 0 and lines and lines[0].strip() == "---":
        for index in range(1, end_index):
            if lines[index].strip() == "---":
                frontmatter_end = index
                break
        if frontmatter_end is not None and not existing_indices:
            for index in range(1, frontmatter_end):
                if CATEGORY_LINE_RE.match(lines[index].strip()):
                    existing_indices = [index]
                    break

    if existing_indices:
        lines[existing_indices[0]] = category_line
        for dup_index in reversed(existing_indices[1:]):
            del lines[dup_index]
    else:
        insert_index = start_index
        if frontmatter_end is not None:
            insert_index = 1
        elif 0 <= start_index < len(lines) and ANY_UNIT_HEADING_RE.match(lines[start_index]):
            insert_index += 1
        while insert_index < end_index and SOURCE_ID_RE.fullmatch(lines[insert_index].strip()):
            insert_index += 1
        lines.insert(insert_index, category_line)

    _update_entry_manual_heading_path(metadata, entry, metadata_path, unit.heading_path)
    updated = newline.join(lines)
    if had_final_newline:
        updated += newline
    if updated == original:
        return {"changed": False, "backup_path": None, "source_path": source_rel_path}

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = os.path.join(backup_dir, timestamp, *source_rel_path.split("/"))
    os.makedirs(filesystem_path(os.path.dirname(backup_path)), exist_ok=True)
    shutil.copy2(filesystem_path(filepath), filesystem_path(backup_path))
    atomic_write_text(filepath, updated)
    return {
        "changed": True,
        "backup_path": normalize_rel_path(os.path.relpath(os.path.realpath(backup_path), os.path.realpath(vault_dir))),
        "source_path": source_rel_path,
    }


def remove_manual_categories(
    source_id,
    expected_fingerprint,
    vault_dir=VAULT_DIR,
    metadata_path=METADATA_PATH,
    rules_path=RULES_PATH,
    backup_dir=None,
):
    if backup_dir is None:
        backup_dir = os.path.join(
            resolve_workspace_state_dir(SCRIPT_DIR, vault_dir),
            "source_id_backups",
            "classification_review",
        )
    if not isinstance(source_id, str) or not SOURCE_ID_VALUE_RE.fullmatch(source_id):
        raise ReviewError("Source ID 형식이 올바르지 않습니다.")
    if not isinstance(expected_fingerprint, str) or not expected_fingerprint:
        raise ReviewError("원본 fingerprint가 없습니다. 화면을 새로 고치세요.")

    metadata, tree = load_review_context(vault_dir, metadata_path, rules_path)
    entry = metadata.get("entries", {}).get(source_id)
    if not isinstance(entry, dict):
        raise ReviewError("Source ID가 최신 metadata에 없습니다.", HTTPStatus.NOT_FOUND)
    filepath, source_rel_path, units, unit_index, unit = locate_source_unit(
        vault_dir, tree, source_id, entry
    )
    current_fingerprint = source_unit_fingerprint(unit)
    if current_fingerprint != expected_fingerprint:
        raise ReviewError(
            "원본 일지가 화면을 연 뒤 변경되었습니다. 새로 고친 후 다시 적용하세요.",
            HTTPStatus.CONFLICT,
        )

    with open(filesystem_path(filepath), "r", encoding="utf-8", newline="") as file_obj:
        original = file_obj.read()
    newline = "\r\n" if "\r\n" in original else "\n"
    had_final_newline = original.endswith(("\n", "\r"))
    lines = original.splitlines()
    start_index = unit.start_line - 1
    end_index = units[unit_index + 1].start_line - 1 if unit_index + 1 < len(units) else len(lines)
    existing_indices = find_existing_category_lines(lines, start_index, end_index)
    for index in reversed(existing_indices):
        del lines[index]

    _update_entry_manual_heading_path(metadata, entry, metadata_path, None)
    updated = newline.join(lines)
    if had_final_newline:
        updated += newline
    if updated == original:
        return {"changed": False, "backup_path": None, "source_path": source_rel_path}

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = os.path.join(backup_dir, timestamp, *source_rel_path.split("/"))
    os.makedirs(filesystem_path(os.path.dirname(backup_path)), exist_ok=True)
    shutil.copy2(filesystem_path(filepath), filesystem_path(backup_path))
    atomic_write_text(filepath, updated)
    return {
        "changed": True,
        "backup_path": normalize_rel_path(os.path.relpath(os.path.realpath(backup_path), os.path.realpath(vault_dir))),
        "source_path": source_rel_path,
    }


def run_production_classifier(vault_dir=VAULT_DIR, lock_held_by_parent=False, extra_args=()):
    command = [
        sys.executable,
        os.path.join(SCRIPT_DIR, "hierarchical_classifier.py"),
        "--vault-dir",
        os.path.abspath(vault_dir),
        "--production",
        "--output-dir",
        "Subject",
        "--review-dir",
        "Topic_Reviews",
        "--metadata-file",
        "organizer_metadata.json",
        *extra_args,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=vault_dir,
            env=delegated_lock_environment() if lock_held_by_parent else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "returncode": None, "output": str(exc)}
    output = "\n".join(value.strip() for value in (result.stdout, result.stderr) if value.strip())
    return {"ok": result.returncode == 0, "returncode": result.returncode, "output": output[-8000:]}


def is_local_origin(origin):
    if not origin:
        return True
    parsed = urllib.parse.urlparse(origin)
    return parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def is_allowed_local_host(host_header):
    if not host_header:
        return False
    raw = str(host_header).strip()
    if not raw or "/" in raw or "@" in raw or " " in raw:
        return False
    parsed = urllib.parse.urlparse(f"http://{raw}")
    return parsed.hostname in {"127.0.0.1", "localhost", "::1"}
