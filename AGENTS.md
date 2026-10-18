# AGENTS.md — Agent2Topic (`a2t`) Work History & Topic Context Protocol

This repository uses **Agent2Topic** (`agent2topic` / `a2t` / `python scripts/agent_cli.py`) to maintain chronological daily logs (`Workspace/Daily_Logs/`) and deterministically compile topic documents (`Workspace/Subject/`), bounded chronological reviews (`Workspace/Topic_Reviews/`), and a pre-indexed metadata table (`organizer_metadata.json`).

**Important**: Classification, indexing, and review compilation are executed 100% by the local Python rule engine (`scripts/hierarchical_classifier.py`). Never spend LLM tokens manually copying or summarizing logs into `Subject/` or `Topic_Reviews/`.

---

## 1. Golden Rules for AI Agents

1. **Single Source of Truth**:
   - Only `Workspace/Daily_Logs/*.md` and `Workspace/Classification_Rules.md` are editable sources.
   - **Never directly edit** files under `Workspace/Subject/` or `Workspace/Topic_Reviews/`—they are deterministically compiled and overwritten on every update.
2. **Retrieve Bounded Context Before Starting a Task**:
   - Search the pre-built metadata index by **modified file path**, **keyword**, **entry kind**, or **Git commit** when you don't know the exact topic name:
     ```bash
     a2t search --file "review_renderer.py" --json
     a2t search --query "atomic_write" --kind "bugfix" --json
     ```
   - Or fetch strictly bounded topic context (`--mode index|review|entries`, `--max-chars 12000`, `--offset 0`):
     ```bash
     a2t rules --json
     a2t context --topic "Projects/Sample Project/Testing" --mode review --max-chars 12000 --json
     ```
   - Use `a2t source --id <ID>` only when you need the full, untruncated text of a specific entry.
3. **Record Work After Completing a Task**:
   - Use `append` with `--kind` (`decision`, `bugfix`, `experiment`, `refactor`, `note`), `--files`, and `--git` (or `--commit <SHA>`).
   - Each `append` creates an independent `###### <title>` work event (`Entry`) with its own permanent `Source ID` and metadata while sharing parent `#`–`#####` topic headings in today's log:
     ```bash
     a2t append \
       --path "Projects/Sample Project/Testing/Functional Test" \
       --title "Verify atomic write and lock recovery" \
       --kind "bugfix" \
       --files "scripts/atomic_io.py, scripts/review_renderer.py" \
       --git \
       --content "Confirmed atomic_write_text preserves existing files and permissions on replace." \
       --create-rule \
       --json
     ```

---

## 2. CLI Reference (`a2t` / `agent2topic` / `./a2t.bat` / `./a2t.sh` / `python scripts/agent_cli.py`)

| Subcommand | Example | Description |
| :--- | :--- | :--- |
| `init` | `a2t init [--here] --json` | Initialize `Workspace/` directories and default `Classification_Rules.md`. |
| `rules` | `a2t rules --json` | List all configured 1–5 level category paths and supplemental keywords. |
| `rule-add` | `a2t rule-add --path "Projects/Alpha/API" --keywords "rest, endpoint" --json` | Add a new topic path or keywords to the inherited table in `Classification_Rules.md`. |
| `append` | `a2t append --path "Projects/Alpha/API" --title "Auth fix" --kind bugfix --files "src/auth.py" --git --stdin --create-rule --json` | Append an independent `######` Entry to `Daily_Logs/YYYY-MM-DD.md` and run the Python classifier. |
| `search` | `a2t search --file "src/auth.py" --query "token" --kind bugfix --commit a1b2c3d --json` | Search pre-indexed entries and related reviews by file path, keyword, kind, commit, agent, session, or topic. |
| `context` | `a2t context --topic "Projects/Alpha" --mode review --max-chars 12000 --limit 5 --offset 0 --json` | Return strictly bounded `Topic_Reviews/` Markdown and entry summaries (`--mode index\|review\|entries`). |
| `update` | `a2t update [--dry-run] [--rebuild-ids] --json` | Run the deterministic Python classifier (`Daily_Logs/` → `Subject/` + `Topic_Reviews/`). |
| `status` | `a2t status --json` | Check classification statistics and any `needs_review` entries. |
| `classify` | `a2t classify --source-id <ID> --path "Projects/Alpha/API" --json` | Assign or clear a manual `Category:` override for a specific `Source ID`. |
| `source` | `a2t source --id <ID> [--max-chars N] --json` | Inspect the original log file, line number, metadata (`kind`, `files`, `commit`), and full content of a `Source ID`. |
| `view` | `a2t view [--refresh]` | Launch the strictly read-only local web viewer (`http://127.0.0.1:8000`) for human inspection. |
| `version` | `a2t version [--check] --json` | Show installed version and optionally check GitHub Releases for updates. |
