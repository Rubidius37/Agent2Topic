# AGENTS.md — Work History & Topic Context Protocol

This repository uses a deterministic Python Markdown compiler (`pagent`) to maintain chronological daily logs (`Workspace/Daily_Logs/`) and automatically compile topic-specific documents (`Workspace/Subject/`) and bounded chronological reviews (`Workspace/Topic_Reviews/`).

**Important**: Classification and review compilation are executed 100% by the local Python rule engine (`scripts/hierarchical_classifier.py`). Never spend LLM tokens manually copying or summarizing logs into `Subject/` or `Topic_Reviews/`.

---

## 1. Golden Rules for AI Agents

1. **Single Source of Truth**:
   - Only `Workspace/Daily_Logs/*.md` and `Workspace/Classification_Rules.md` are editable sources.
   - **Never directly edit** files under `Workspace/Subject/` or `Workspace/Topic_Reviews/`—they are deterministically compiled and overwritten on every update.
2. **Retrieve Context Before Starting a Task**:
   - Check available topic paths first, then fetch the compiled review for the relevant topic:
     ```bash
     python scripts/agent_cli.py rules --json
     python scripts/agent_cli.py context --topic "Projects/Sample Project/Testing" --limit 5 --json
     ```
3. **Record Work After Completing a Task**:
   - Use `append` to log decisions, root causes, experiments, or verification results.
   - `append` automatically sanitizes `#`–`#####` headings inside your body text to `######` so they never corrupt 5-level classification boundaries, writes to today's log (`Workspace/Daily_Logs/YYYY-MM-DD.md`), and triggers the Python classifier:
     ```bash
     python scripts/agent_cli.py append \
       --path "Projects/Sample Project/Testing/Functional Test" \
       --title "Verify atomic write and lock recovery" \
       --content "Confirmed atomic_write_text preserves existing files on replace failure." \
       --create-rule \
       --json
     ```

---

## 2. CLI Reference (`pagent.bat` / `./pagent.sh` / `python scripts/agent_cli.py`)

| Subcommand | Example | Description |
| :--- | :--- | :--- |
| `init` | `python scripts/agent_cli.py init --json` | Initialize `Workspace/` directories and default `Classification_Rules.md`. |
| `rules` | `python scripts/agent_cli.py rules --json` | List all configured 1–5 level category paths and supplemental keywords. |
| `rule-add` | `python scripts/agent_cli.py rule-add --path "Projects/Alpha/API" --keywords "rest, endpoint" --json` | Add a new topic path or keywords to the inherited table in `Classification_Rules.md`. |
| `append` | `python scripts/agent_cli.py append --path "Projects/Alpha/API" --title "Auth fix" --stdin --create-rule --json` | Append an entry to `Daily_Logs/YYYY-MM-DD.md` and run the Python classifier. |
| `context` | `python scripts/agent_cli.py context --topic "Projects/Alpha" --limit 5 --json` | Return the bounded `Topic_Reviews/` Markdown and the latest N entries for a topic. |
| `update` | `python scripts/agent_cli.py update --json` | Run the deterministic Python classifier (`Daily_Logs/` → `Subject/` + `Topic_Reviews/`). |
| `status` | `python scripts/agent_cli.py status --json` | Check classification statistics and any `needs_review` entries. |
| `classify` | `python scripts/agent_cli.py classify --source-id <ID> --path "Projects/Alpha/API" --json` | Assign or clear a manual `Category:` override for a specific `Source ID`. |
| `source` | `python scripts/agent_cli.py source --id <ID> --json` | Inspect the original log file, line number, and full content of a `Source ID`. |
| `view` | `python scripts/agent_cli.py view` | Launch the read-only local web viewer (`http://127.0.0.1:8000`) for human inspection. |
