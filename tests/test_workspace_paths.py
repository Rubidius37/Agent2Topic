import os
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from workspace_paths import (  # noqa: E402
    find_workspace_from_cwd,
    resolve_active_workspace_dir,
    resolve_workspace_dir,
)


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

    def test_cwd_discovery_walks_up_to_find_external_project_workspace(self):
        with tempfile.TemporaryDirectory() as external_project:
            ws = os.path.join(external_project, "Workspace")
            subdir = os.path.join(external_project, "src", "components")
            os.makedirs(ws)
            os.makedirs(subdir)
            with open(os.path.join(ws, "Classification_Rules.md"), "w", encoding="utf-8") as f:
                f.write("rules")

            self.assertEqual(os.path.abspath(ws), find_workspace_from_cwd(subdir))
            self.assertEqual(
                os.path.abspath(ws),
                resolve_active_workspace_dir(SCRIPTS_DIR, cwd=subdir),
            )

    def test_for_init_creates_workspace_in_external_cwd(self):
        with tempfile.TemporaryDirectory() as external_project, patch.dict(
            os.environ, {"PAGENT_VAULT_DIR": ""}, clear=False
        ):
            resolved = resolve_active_workspace_dir(
                SCRIPTS_DIR, cwd=external_project, for_init=True
            )
            self.assertEqual(
                os.path.abspath(os.path.join(external_project, "Workspace")),
                resolved,
            )


if __name__ == "__main__":
    unittest.main()
