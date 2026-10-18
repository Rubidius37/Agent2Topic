from http import HTTPStatus
from http.server import ThreadingHTTPServer
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from review_ops import DashboardLifecycle, ReviewError  # noqa: E402
import workspace_viewer  # noqa: E402


PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)


class WorkspaceViewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = self.temp.name
        os.makedirs(os.path.join(self.vault, "Daily_Logs"))
        os.makedirs(os.path.join(self.vault, "Subject", "Projects", "Alpha"))
        os.makedirs(os.path.join(self.vault, "Topic_Reviews", "Projects", "Alpha"))
        os.makedirs(os.path.join(self.vault, "attachments"))
        os.makedirs(os.path.join(self.vault, "scripts"))

        with open(os.path.join(self.vault, "Classification_Rules.md"), "w", encoding="utf-8") as f:
            f.write("# Rules\n")
        with open(os.path.join(self.vault, "Daily_Logs", "2026-10-18.md"), "w", encoding="utf-8") as f:
            f.write("# Projects\n## Alpha\nDaily log body.\n")
        with open(
            os.path.join(self.vault, "Subject", "Projects", "Alpha", "Alpha.md"),
            "w",
            encoding="utf-8",
        ) as f:
            f.write("# Alpha Subject\nDetailed subject entry.\n")
        with open(
            os.path.join(self.vault, "Topic_Reviews", "Projects", "Alpha", "[리뷰] Alpha.md"),
            "w",
            encoding="utf-8",
        ) as f:
            f.write("# Alpha 리뷰\nReview summary.\n")
        with open(os.path.join(self.vault, "attachments", "diagram.png"), "wb") as f:
            f.write(PNG_1X1)

    def tearDown(self):
        self.temp.cleanup()

    def test_build_workspace_tree_and_read_markdown(self):
        tree = workspace_viewer.build_workspace_tree(self.vault)
        self.assertEqual(tree["status"], "ok")
        self.assertEqual(tree["default_file"], "Topic_Reviews/Projects/Alpha/[리뷰] Alpha.md")
        section_counts = {sec["id"]: sec["count"] for sec in tree["sections"]}
        self.assertEqual(section_counts["Topic_Reviews"], 1)
        self.assertEqual(section_counts["Subject"], 1)
        self.assertEqual(section_counts["Daily_Logs"], 1)
        self.assertEqual(section_counts["Classification_Rules"], 1)

        doc = workspace_viewer.read_workspace_markdown(
            self.vault, "Topic_Reviews/Projects/Alpha/[리뷰] Alpha.md"
        )
        self.assertIn("# Alpha 리뷰", doc["content"])

    def test_path_traversal_and_non_markdown_are_rejected(self):
        with self.assertRaises(ReviewError) as ctx:
            workspace_viewer.read_workspace_markdown(self.vault, "../outside.md")
        self.assertIn(ctx.exception.status, (HTTPStatus.BAD_REQUEST, HTTPStatus.FORBIDDEN))

        with self.assertRaises(ReviewError) as ctx2:
            workspace_viewer.read_workspace_markdown(self.vault, "attachments/diagram.png")
        self.assertEqual(ctx2.exception.status, HTTPStatus.BAD_REQUEST)

    def test_http_endpoints_and_session_auto_shutdown(self):
        class BoundHandler(workspace_viewer.WorkspaceViewerHandler):
            pass

        BoundHandler.vault_dir = self.vault
        server = ThreadingHTTPServer(("127.0.0.1", 0), BoundHandler)
        lifecycle = DashboardLifecycle(close_delay=0.15)
        lifecycle.attach(server)
        server.dashboard_lifecycle = lifecycle

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"

        try:
            # 1. GET /api/tree
            with urllib.request.urlopen(f"{base_url}/api/tree", timeout=5) as resp:
                tree_data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(tree_data["status"], "ok")

            # 2. GET /api/file
            with urllib.request.urlopen(
                f"{base_url}/api/file?path=Daily_Logs/2026-10-18.md", timeout=5
            ) as resp:
                file_data = json.loads(resp.read().decode("utf-8"))
                self.assertIn("Daily log body.", file_data["content"])

            # 3. GET /workspace/attachments/diagram.png
            with urllib.request.urlopen(
                f"{base_url}/workspace/attachments/diagram.png", timeout=5
            ) as resp:
                self.assertEqual(resp.headers.get("Content-Type"), "image/png")
                self.assertEqual(resp.read(), PNG_1X1)

            # 4. Path traversal via HTTP returns 400/403
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(f"{base_url}/api/file?path=../../secret.md", timeout=5)
            self.assertIn(err.exception.code, (400, 403))

            # 4b. DNS Rebinding (Host: evil.example.com) and Cross-Site GET are blocked with 403
            req_rebind = urllib.request.Request(
                f"{base_url}/api/tree",
                headers={"Host": "evil.example.com:8000"},
                method="GET",
            )
            with self.assertRaises(urllib.error.HTTPError) as rebind_err:
                urllib.request.urlopen(req_rebind, timeout=5)
            self.assertEqual(rebind_err.exception.code, 403)

            req_cross = urllib.request.Request(
                f"{base_url}/api/tree",
                headers={"Sec-Fetch-Site": "cross-site"},
                method="GET",
            )
            with self.assertRaises(urllib.error.HTTPError) as cross_err:
                urllib.request.urlopen(req_cross, timeout=5)
            self.assertEqual(cross_err.exception.code, 403)

            # 5. Open and close session -> server shuts down automatically
            req_open = urllib.request.Request(
                f"{base_url}/api/session/open",
                data=json.dumps({"session_id": "testsession_12345"}).encode("utf-8"),
                headers={"Content-Type": "application/json", "Origin": "http://127.0.0.1"},
                method="POST",
            )
            with urllib.request.urlopen(req_open, timeout=5) as resp:
                self.assertEqual(resp.status, 200)

            req_close = urllib.request.Request(
                f"{base_url}/api/session/close",
                data=json.dumps({"session_id": "testsession_12345"}).encode("utf-8"),
                headers={"Content-Type": "application/json", "Origin": "http://127.0.0.1"},
                method="POST",
            )
            with urllib.request.urlopen(req_close, timeout=5) as resp:
                self.assertEqual(resp.status, 200)

            thread.join(timeout=3.0)
            self.assertFalse(thread.is_alive(), "Server should auto-shutdown after last tab closes.")
        finally:
            lifecycle.stop()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
