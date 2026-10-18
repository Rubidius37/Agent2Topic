"""Read-only local web viewer for browsing Workspace folder trees and rendered Markdown."""

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
import posixpath
import sys
import time
import urllib.parse
import webbrowser

from atomic_io import filesystem_path
from document_utils import normalize_rel_path, source_date_key
from hierarchical_classifier import count_daily_markdown_files, load_metadata
from review_ops import (
    DASHBOARD_CLOSE_GRACE_SECONDS,
    DASHBOARD_SESSION_ID_RE,
    DashboardLifecycle,
    ReviewError,
    is_local_origin,
    resolve_ui_language,
    run_production_classifier,
)
from workspace_paths import resolve_workspace_dir, resolve_workspace_state_dir


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_VAULT_DIR = resolve_workspace_dir(SCRIPT_DIR)
STATIC_DIR = os.path.join(SCRIPT_DIR, "viewer_ui")
MAX_REQUEST_BYTES = 16 * 1024

ALLOWED_ASSET_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
}


def resolve_safe_workspace_path(vault_dir, rel_path):
    cleaned = normalize_rel_path(str(rel_path or "")).lstrip("/")
    if not cleaned or cleaned.startswith(".."):
        raise ReviewError("Invalid workspace path.", HTTPStatus.BAD_REQUEST)
    parts = [p for p in cleaned.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise ReviewError("Path traversal is not allowed.", HTTPStatus.FORBIDDEN)
    vault_real = os.path.realpath(vault_dir)
    target_real = os.path.realpath(os.path.join(vault_real, *parts))
    try:
        inside = os.path.commonpath([target_real, vault_real]) == vault_real
    except ValueError:
        inside = False
    if not inside:
        raise ReviewError("Path is outside the workspace.", HTTPStatus.FORBIDDEN)
    normalized = normalize_rel_path(os.path.relpath(target_real, vault_real))
    return target_real, normalized


def _build_dir_tree(vault_real, current_dir, sort_descending=False):
    entries = []
    if not os.path.isdir(filesystem_path(current_dir)):
        return entries, 0

    try:
        names = os.listdir(filesystem_path(current_dir))
    except OSError:
        return entries, 0

    subdirs = []
    files = []
    for name in names:
        if name.startswith("."):
            continue
        full = os.path.join(current_dir, name)
        if os.path.isdir(filesystem_path(full)):
            subdirs.append((name, full))
        elif os.path.isfile(filesystem_path(full)) and name.casefold().endswith(".md"):
            files.append((name, full))

    subdirs.sort(key=lambda item: item[0].casefold())
    if sort_descending:
        files.sort(
            key=lambda item: (
                source_date_key(item[0]),
                item[0].casefold(),
            ),
            reverse=True,
        )
    else:
        files.sort(
            key=lambda item: (
                0 if item[0].startswith(("[종합 리뷰]", "[Summary Review]")) else 1,
                item[0].casefold(),
            )
        )

    total_files = 0
    for name, full in subdirs:
        children, count = _build_dir_tree(vault_real, full, sort_descending=sort_descending)
        if count > 0:
            rel = normalize_rel_path(os.path.relpath(full, vault_real))
            entries.append(
                {
                    "type": "folder",
                    "name": name,
                    "path": rel,
                    "count": count,
                    "children": children,
                }
            )
            total_files += count

    for name, full in files:
        rel = normalize_rel_path(os.path.relpath(full, vault_real))
        entries.append(
            {
                "type": "file",
                "name": name,
                "path": rel,
                "date": source_date_key(rel) if rel.startswith("Daily_Logs/") else None,
            }
        )
        total_files += 1

    return entries, total_files


def _first_file_in_nodes(nodes):
    for node in nodes:
        if node.get("type") == "file":
            return node.get("path")
        if node.get("type") == "folder":
            found = _first_file_in_nodes(node.get("children", ()))
            if found:
                return found
    return None


def build_workspace_tree(vault_dir, metadata_path=None):
    vault_real = os.path.realpath(vault_dir)
    if metadata_path is None:
        state_dir = resolve_workspace_state_dir(SCRIPT_DIR, vault_real)
        metadata_path = os.path.join(state_dir, "organizer_metadata.json")

    sections = []
    section_specs = (
        ("Topic_Reviews", "Topic Reviews", "주제별 리뷰", False),
        ("Subject", "Subject Documents", "주제별 상세 문서", False),
        ("Daily_Logs", "Daily Logs", "날짜별 원본 일지", True),
    )

    default_file = None
    for folder_name, label_en, label_ko, desc in section_specs:
        folder_path = os.path.join(vault_real, folder_name)
        children, count = _build_dir_tree(vault_real, folder_path, sort_descending=desc)
        if default_file is None and children:
            default_file = _first_file_in_nodes(children)
        sections.append(
            {
                "id": folder_name,
                "type": "section",
                "name": folder_name,
                "label_en": label_en,
                "label_ko": label_ko,
                "path": folder_name,
                "count": count,
                "children": children,
            }
        )

    rules_file = os.path.join(vault_real, "Classification_Rules.md")
    has_rules = os.path.isfile(filesystem_path(rules_file))
    if has_rules:
        if default_file is None:
            default_file = "Classification_Rules.md"
        sections.append(
            {
                "id": "Classification_Rules",
                "type": "section",
                "name": "Classification_Rules.md",
                "label_en": "Classification Rules",
                "label_ko": "분류 규칙",
                "path": "Classification_Rules.md",
                "count": 1,
                "children": [
                    {
                        "type": "file",
                        "name": "Classification_Rules.md",
                        "path": "Classification_Rules.md",
                        "date": None,
                    }
                ],
            }
        )

    metadata = load_metadata(metadata_path) if os.path.isfile(metadata_path) else {}
    stats = metadata.get("stats", {}) if isinstance(metadata, dict) else {}

    return {
        "status": "ok",
        "language": resolve_ui_language(),
        "default_file": default_file,
        "stats": stats,
        "sections": sections,
    }


def read_workspace_markdown(vault_dir, rel_path):
    target_real, normalized = resolve_safe_workspace_path(vault_dir, rel_path)
    if not normalized.casefold().endswith(".md"):
        raise ReviewError("Only Markdown (.md) files can be viewed.", HTTPStatus.BAD_REQUEST)
    if not os.path.isfile(filesystem_path(target_real)):
        raise ReviewError(f"File not found: {normalized}", HTTPStatus.NOT_FOUND)

    with open(filesystem_path(target_real), "r", encoding="utf-8") as file_obj:
        content = file_obj.read()

    stat = os.stat(filesystem_path(target_real))
    return {
        "status": "ok",
        "path": normalized,
        "name": posixpath.basename(normalized),
        "directory": posixpath.dirname(normalized),
        "size": stat.st_size,
        "mtime": int(stat.st_mtime),
        "content": content,
    }


class WorkspaceViewerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    vault_dir = DEFAULT_VAULT_DIR
    static_dir = STATIC_DIR

    def log_message(self, format_string, *args):
        print(f"[Viewer] {format_string % args}")

    def send_json(self, value, status=HTTPStatus.OK):
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def send_error_json(self, error):
        self.send_json(
            {"status": "error", "message": error.message, **error.details},
            error.status,
        )

    def send_static(self, filename, content_type):
        path = os.path.join(self.static_dir, filename)
        try:
            with open(path, "rb") as file_obj:
                payload = file_obj.read()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(payload)

    def send_workspace_asset(self, rel_path):
        target_real, normalized = resolve_safe_workspace_path(self.vault_dir, rel_path)
        ext = os.path.splitext(normalized)[1].casefold()
        content_type = ALLOWED_ASSET_EXTENSIONS.get(ext) or mimetypes.guess_type(normalized)[0]
        if ext not in ALLOWED_ASSET_EXTENSIONS:
            raise ReviewError("Unsupported workspace asset type.", HTTPStatus.FORBIDDEN)
        if not os.path.isfile(filesystem_path(target_real)):
            raise ReviewError(f"Asset not found: {normalized}", HTTPStatus.NOT_FOUND)
        with open(filesystem_path(target_real), "rb") as file_obj:
            payload = file_obj.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        if ext == ".svg":
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; img-src data:",
            )
        self.end_headers()
        self.wfile.write(payload)

    def read_json_payload(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ReviewError("Invalid request size.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise ReviewError("Only JSON requests are allowed.", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ReviewError("Invalid request body.")
        return payload

    def validate_session_id(self, session_id):
        if not isinstance(session_id, str) or not DASHBOARD_SESSION_ID_RE.fullmatch(session_id):
            raise ReviewError("Invalid session ID.")
        return session_id

    def serve_session_watch(self, session_id):
        connection_id = f"{session_id}:{id(self)}"
        self.server.dashboard_lifecycle.open(connection_id)
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            while True:
                self.wfile.write(b": viewer-connected\n\n")
                self.wfile.flush()
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.dashboard_lifecycle.close(connection_id)
            self.close_connection = True

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path in {"/", "/index.html"}:
                self.send_static("index.html", "text/html; charset=utf-8")
            elif path == "/viewer.css":
                self.send_static("viewer.css", "text/css; charset=utf-8")
            elif path == "/viewer.js":
                self.send_static("viewer.js", "text/javascript; charset=utf-8")
            elif path == "/api/tree":
                state_dir = resolve_workspace_state_dir(SCRIPT_DIR, self.vault_dir)
                meta_path = os.path.join(state_dir, "organizer_metadata.json")
                self.send_json(build_workspace_tree(self.vault_dir, meta_path))
            elif path == "/api/file":
                qs = urllib.parse.parse_qs(parsed.query)
                raw_target = qs.get("path", [""])[0]
                self.send_json(read_workspace_markdown(self.vault_dir, raw_target))
            elif path.startswith("/workspace/"):
                rel_asset = urllib.parse.unquote(path[len("/workspace/"):])
                self.send_workspace_asset(rel_asset)
            elif path == "/api/session/watch":
                if not is_local_origin(self.headers.get("Origin")):
                    raise ReviewError("Forbidden request origin.", HTTPStatus.FORBIDDEN)
                values = urllib.parse.parse_qs(parsed.query).get("session_id", [])
                session_id = self.validate_session_id(values[0] if len(values) == 1 else None)
                self.serve_session_watch(session_id)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except ReviewError as exc:
            self.send_error_json(exc)
        except Exception as exc:
            self.send_error_json(
                ReviewError(f"Server error: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
            )

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in {"/api/session/open", "/api/session/close"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not is_local_origin(self.headers.get("Origin")):
            self.send_error_json(ReviewError("Forbidden request origin.", HTTPStatus.FORBIDDEN))
            return
        try:
            payload = self.read_json_payload()
            session_id = self.validate_session_id(payload.get("session_id"))
            if path == "/api/session/open":
                self.server.dashboard_lifecycle.open(session_id)
                self.send_json({"status": "success"})
                return
            closed = self.server.dashboard_lifecycle.close(session_id)
            self.send_json({"status": "success", "closed": closed})
        except ReviewError as exc:
            self.send_error_json(exc)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error_json(ReviewError("Malformed JSON request."))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Open the read-only Workspace Markdown viewer.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--vault-dir", default=None)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="Run classifier update before starting viewer.")
    parser.add_argument("--no-refresh", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    args = parse_args(argv)
    if not 1 <= args.port <= 65535:
        print("Error: port must be between 1 and 65535.")
        return 2

    vault_dir = os.path.abspath(args.vault_dir) if args.vault_dir else DEFAULT_VAULT_DIR
    daily_dir = os.path.join(vault_dir, "Daily_Logs")
    if args.refresh and not args.no_refresh and count_daily_markdown_files(daily_dir) > 0:
        print("Refreshing local Subject/ and Topic_Reviews/ before opening viewer...")
        initial = run_production_classifier(vault_dir)
        if not initial["ok"]:
            print(initial["output"])

    class BoundViewerHandler(WorkspaceViewerHandler):
        pass

    BoundViewerHandler.vault_dir = vault_dir

    address = ("127.0.0.1", args.port)
    try:
        server = ThreadingHTTPServer(address, BoundViewerHandler)
    except OSError as exc:
        print(f"Error: Could not start viewer on http://127.0.0.1:{args.port}: {exc}")
        return 1

    lifecycle = DashboardLifecycle(close_delay=DASHBOARD_CLOSE_GRACE_SECONDS)
    lifecycle.attach(server)
    server.dashboard_lifecycle = lifecycle

    url = f"http://127.0.0.1:{args.port}"
    print(f"Workspace read-only viewer: {url}")
    print("Close all viewer browser tabs to stop automatically.")
    print("Press Ctrl+C to stop manually.")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception as exc:
            print(f"Warning: Could not open browser automatically: {exc}")
    try:
        with server:
            server.serve_forever()
    except KeyboardInterrupt:
        print("\nViewer stopped.")
    finally:
        lifecycle.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
