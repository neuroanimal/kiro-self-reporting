# kiro-self-reporting

Generates a single interactive HTML report of all historical AI chat sessions
on this machine, across every tool that stored them in a different place and
format.

## Run

```bash
python3 make_report.py
```

Output: `_report.ai.html` in this directory. Open it in a browser.

## Sources collected

| Source | Location | Format |
|---|---|---|
| kiro-cli (current) | `~/.local/share/kiro-cli/data.sqlite3` → table `conversations_v2` | SQLite / JSON |
| kiro-cli (legacy) | `~/.kiro/sessions/cli/*.jsonl` | JSONL |
| Amazon Q | `~/.aws/amazonq/history/*.json` | loki-js DB dump (`tabs`) |
| cursor | `~/.cursor/projects/*/agent-transcripts/*.txt` | plain text |

## SQLite safety

The live DB is **never opened directly**. `make_report.py` first copies it to
`/tmp/kiro_data_copy.sqlite3` with a plain file copy (no SQLite connection, so
no lock and no write to the original), reads the copy with
`mode=ro&immutable=1`, and deletes the copy afterwards. This matters because a
running kiro-cli session writes its own conversation to the live DB
concurrently.

## Report columns

`# | Date (UTC) | Task Status | Talk Status | Tool | Model LLM |
Workspace path | Msgs | Title | About | Ended-with | Source`

- **Task Status** / **Talk Status** are heuristics read from the end of each
  conversation (completion markers vs. "to be continued" / `/compact` /
  cut-off mid tool-use). Treat them as indicative, not authoritative.
- **Model LLM** is only recorded by kiro-cli SQLite; Amazon Q and cursor do not
  store it, shown as "— (nie zapisany)".

## Interactive features (client-side, no server)

- Global search box across all columns.
- Per-column text filter inputs.
- Click any column header to sort (toggles asc/desc); Date and Msgs sort
  numerically via `data-sort`.
- "Wyczyść filtry" button and a visible/total counter.
