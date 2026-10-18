# AGENTS.md — Work History & Topic Context Protocol

This repository uses a deterministic Python Markdown compiler (`pagent`) to maintain chronological daily logs (`Workspace/Daily_Logs/`) and automatically compile topic-specific documents (`Workspace/Subject/`) and bounded chronological reviews (`Workspace/Topic_Reviews/`).

**Important**: Classification, indexing, and review compilation are executed 100% by the local Python rule engine (`scripts/hierarchical_classifier.py`). Never spend LLM tokens manually copying or summarizing logs into `Subject/` or `Topic_Reviews/`.

---

## 1. Golden Rules for AI Agents

1. **Single Source of Truth**:
   - Only `Workspace/Daily_Logs/*.md` and `Workspace/Classification_Rules.md` are editable sources.
   - **Never directly edit** files under `Workspace/Subject/` or `Workspace/Topic_Reviews/`—they are deterministically compiled and overwritten on every update.
2. **Retrieve Bounded Context Before Starting a Task**:
   - Search the pre-built metadata index by **modified file path**, **keyword**, **entry kind**, or **Git commit** when you don't know the exact topic name:
     ```bash
     python scripts/agent_cli.py search --file "review_renderer.py" --json
     python scripts/agent_cli.py search --query "atomic_write" --kind "bugfix" --json
     ```
   - Or fetch bounded topic context (`--mode index|review|entries`, `--max-chars 12000`):
     ```bash
     python scripts/agent_cli.py rules --json
     python scripts/agent_cli.py context --topic "Projects/Sample Project/Testing" --mode review --max-chars 12000 --json
     ```
   - Use `source --id <ID>` only when you need the full, untruncated text of a specific entry.
3. **Record Work After Completing a Task**:
   - Use `append` with `--kind` (`decision`, `bugfix`, `experiment`, `refactor`, `note`), `--files`, and `--git` (or `--commit <SHA>`).
   - Each `append` creates an independent `###### <title>` work event (`Entry`) with its own permanent `Source ID` and metadata while sharing parent `#`–`#####` topic headings in today's log:
     ```bash
     python scripts/agent_cli.py append \
       --path "Projects/Sample Project/Testing/Functional Test" \
       --title "Verify atomic write and lock recovery" \
       --kind "bugfix" \
       --files "scripts/atomic_io.py, scripts/review_renderer.py" \
       --git \
       --content "Confirmed atomic_write_text preserves existing files on replace failure." \
       --create-rule \
       --json
     ```

---

## 2. CLI Reference (`pagent` / `pagent.bat` / `./pagent.sh` / `python scripts/agent_cli.py`)

| Subcommand | Example | Description |
| :--- | :--- | :--- |
| `init` | `python scripts/agent_cli.py init [--here] --json` | Initialize `Workspace/` directories and default `Classification_Rules.md`. |
| `rules` | `python scripts/agent_cli.py rules --json` | List all configured 1–5 level category paths and supplemental keywords. |
| `rule-add` | `python scripts/agent_cli.py rule-add --path "Projects/Alpha/API" --keywords "rest, endpoint" --json` | Add a new topic path or keywords to the inherited table in `Classification_Rules.md`. |
| `append` | `python scripts/agent_cli.py append --path "Projects/Alpha/API" --title "Auth fix" --kind bugfix --files "src/auth.py" --git --stdin --create-rule --json` | Append an independent `######` Entry to `Daily_Logs/YYYY-MM-DD.md` and run the Python classifier. |
| `search` | `python scripts/agent_cli.py search --file "src/auth.py" --query "token" --kind bugfix --commit a1b2c3d --json` | Search pre-indexed entries and related reviews by file path, keyword, kind, commit, or topic. |
| `context` | `python scripts/agent_cli.py context --topic "Projects/Alpha" --mode review --max-chars 12000 --limit 5 --json` | Return bounded `Topic_Reviews/` Markdown and entry summaries (`--mode index\|review\|entries`). |
| `update` | `python scripts/agent_cli.py update [--dry-run] [--rebuild-ids] --json` | Run the deterministic Python classifier (`Daily_Logs/` → `Subject/` + `Topic_Reviews/`). |
| `status` | `python scripts/agent_cli.py status --json` | Check classification statistics and any `needs_review` entries. |
| `classify` | `python scripts/agent_cli.py classify --source-id <ID> --path "Projects/Alpha/API" --json` | Assign or clear a manual `Category:` override for a specific `Source ID`. |
| `source` | `python scripts/agent_cli.py source --id <ID> [--max-chars N] --json` | Inspect the original log file, line number, metadata (`kind`, `files`, `commit`), and full content of a `Source ID`. |
| `view` | `python scripts/agent_cli.py view [--refresh]` | Launch the strictly read-only local web viewer (`http://127.0.0.1:8000`) for human inspection. |
