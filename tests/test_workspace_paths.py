import os
import sys
import tempfile
import unittest


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from workspace_paths import resolve_workspace_dir  # noqa: E402


class WorkspacePathTests(unittest.TestCase):
    def test_bundled_workspace_is_used_when_it_contains_rules(self):
        with tempfile.TemporaryDirectory() as root:
            script_dir = os.path.join(root, "scripts")
            workspace_dir = os.path.join(root, "Workspace")
            os.makedirs(script_dir)
            os.makedirs(workspace_dir)
            with open(
                os.path.join(workspace_dir, "Classification_Rules.md"),
                "w",
                encoding="utf-8",
            ) as file_obj:
                file_obj.write("rules")

            self.assertEqual(os.path.abspath(workspace_dir), resolve_workspace_dir(script_dir))

    def test_legacy_root_layout_remains_supported_without_workspace_rules(self):
        with tempfile.TemporaryDirectory() as root:
            script_dir = os.path.join(root, "scripts")
            os.makedirs(script_dir)
            with open(
                os.path.join(root, "Classification_Rules.md"),
                "w",
                encoding="utf-8",
            ) as file_obj:
                file_obj.write("rules")

            self.assertEqual(os.path.abspath(root), resolve_workspace_dir(script_dir))
