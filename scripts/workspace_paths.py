"""Resolve the Markdown workspace independently from the application folder."""

import os


WORKSPACE_DIRECTORY_NAME = "Workspace"
RULES_FILENAME = "Classification_Rules.md"
VAULT_ENV_VAR = "AGENT2TOPIC_VAULT_DIR"
LEGACY_VAULT_ENV_VAR = "PAGENT_VAULT_DIR"


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


def find_workspace_from_cwd(start_dir):
    """Walk up from start_dir looking for Workspace/Classification_Rules.md or Classification_Rules.md."""
    current = os.path.abspath(start_dir)
    while True:
        candidate_ws = os.path.join(current, WORKSPACE_DIRECTORY_NAME)
        if os.path.isfile(os.path.join(candidate_ws, RULES_FILENAME)):
            return os.path.abspath(candidate_ws)
        if os.path.isfile(os.path.join(current, RULES_FILENAME)):
            return os.path.abspath(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def resolve_active_workspace_dir(script_dir, cwd=None, explicit_vault_dir=None, for_init=False):
    """Resolve the active workspace from explicit arg, env var, CWD ancestry, or bundled default."""
    if explicit_vault_dir:
        return os.path.abspath(explicit_vault_dir)
    env_vault = (
        os.environ.get(VAULT_ENV_VAR, "").strip()
        or os.environ.get(LEGACY_VAULT_ENV_VAR, "").strip()
    )
    if env_vault:
        return os.path.abspath(env_vault)

    effective_cwd = os.path.abspath(cwd or os.getcwd())
    discovered = find_workspace_from_cwd(effective_cwd)
    if discovered:
        return discovered

    app_root = get_application_root(script_dir)
    try:
        inside_app = os.path.commonpath([os.path.realpath(effective_cwd), os.path.realpath(app_root)]) == os.path.realpath(app_root)
    except ValueError:
        inside_app = False

    if for_init and not inside_app:
        if os.path.basename(effective_cwd).casefold() == WORKSPACE_DIRECTORY_NAME.casefold():
            return effective_cwd
        return os.path.join(effective_cwd, WORKSPACE_DIRECTORY_NAME)

    return resolve_workspace_dir(script_dir)


def resolve_workspace_state_dir(script_dir, workspace_dir):
    """Keep runtime state beside scripts for bundled workspaces and legacy vaults."""
    script_dir = os.path.abspath(script_dir)
    workspace_dir = os.path.abspath(workspace_dir)
    bundled_workspace = resolve_workspace_dir(script_dir)
    if os.path.normcase(os.path.realpath(workspace_dir)) == os.path.normcase(os.path.realpath(bundled_workspace)):
        return script_dir
    return os.path.join(workspace_dir, "scripts")
