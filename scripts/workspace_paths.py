"""Resolve the Markdown workspace independently from the application folder."""

import os


WORKSPACE_DIRECTORY_NAME = "Workspace"
RULES_FILENAME = "Classification_Rules.md"


def get_application_root(script_dir):
    """Return the distribution root that contains the scripts directory."""
    return os.path.abspath(os.path.join(script_dir, ".."))


def resolve_workspace_dir(script_dir):
    """Use a bundled Workspace when present, otherwise preserve legacy layout."""
    application_root = get_application_root(script_dir)
    workspace_dir = os.path.join(application_root, WORKSPACE_DIRECTORY_NAME)
    workspace_rules = os.path.join(workspace_dir, RULES_FILENAME)
    if os.path.isfile(workspace_rules):
        return os.path.abspath(workspace_dir)
    return application_root


def resolve_workspace_state_dir(script_dir, workspace_dir):
    """Keep runtime state beside scripts for bundled workspaces and legacy vaults."""
    script_dir = os.path.abspath(script_dir)
    workspace_dir = os.path.abspath(workspace_dir)
    bundled_workspace = resolve_workspace_dir(script_dir)
    if os.path.normcase(workspace_dir) == os.path.normcase(bundled_workspace):
        return script_dir
    return os.path.join(workspace_dir, "scripts")
