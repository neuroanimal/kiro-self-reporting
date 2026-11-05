#!/usr/bin/env python3
"""Export kiro-cli conversations to readable YAML files and a zip, plus a short index.

kiro-cli keeps every conversation as one JSON blob in the table ``conversations_v2`` of
``~/.local/share/kiro-cli/data.sqlite3`` (key = working directory, value = conversation).
This script never opens the live database: like make_report.py it copies it to
/tmp/kiro_export_copy.sqlite3 with a plain file copy, reads the copy with
mode=ro&immutable=1 and deletes it afterwards. It writes, per conversation, a YAML file with:
  - metadata (id, working directory, created/updated, number of turns, how it ended);
  - ``transcript``: the readable chat as kiro-cli shows it (prompts start with "> ");
  - ``turns``: every turn - user prompt or tool results, assistant text and tool calls
    (tool outputs are cut to --max-output characters to keep the files readable).

Usage:
    python3 export_kiro_sessions.py --out ~/kiro_sessions_export/out [--since 2026-10-21]
                                    [--cwd-filter cfg/tools] [--max-output 4000]
"""
# pylint: disable=line-too-long,too-many-locals  # CLI one-liners; main/extract track several counters
import argparse
import datetime
import json
import os
import shutil
import sqlite3
import zipfile

import yaml

DB = os.path.expanduser("~/.local/share/kiro-cli/data.sqlite3")
DB_COPY = "/tmp/kiro_export_copy.sqlite3"


def to_dt(value):
    """Convert a kiro-cli timestamp (ms or s since epoch) to a local ISO string."""
    secs = value / 1000 if value > 1e11 else value
    return datetime.datetime.fromtimestamp(secs).strftime("%Y-%m-%d %H:%M")


def cut(text, limit):
    """Shorten a long string, keeping head and tail, so tool outputs stay readable."""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    if limit <= 0 or len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} chars cut] ...\n{text[-half:]}"


def user_part(content, limit):
    """Render the user side of a turn: a prompt, tool results or cancelled tool uses."""
    if "Prompt" in content:
        return {"prompt": content["Prompt"].get("prompt", "")}
    if "ToolUseResults" in content:
        res = []
        for r in content["ToolUseResults"].get("tool_use_results", []):
            parts = []
            for c in r.get("content", []):
                parts.append(cut(c.get("Text") if isinstance(c, dict) and "Text" in c
                                 else c.get("Json") if isinstance(c, dict) else c, limit))
            res.append({"tool_use_id": r.get("tool_use_id"), "status": r.get("status"),
                        "output": "\n".join(str(p) for p in parts)})
        return {"tool_results": res}
    if "CancelledToolUses" in content:
        return {"cancelled_tool_uses": cut(content["CancelledToolUses"], limit)}
    return {"other": cut(content, limit)}


def assistant_part(assistant, limit):
    """Render the assistant side of a turn: response text and/or tool calls with arguments."""
    if not isinstance(assistant, dict):
        return {"other": cut(assistant, limit)}
    if "Response" in assistant:
        return {"response": assistant["Response"].get("content", "")}
    if "ToolUse" in assistant:
        tu = assistant["ToolUse"]
        calls = [{"tool": t.get("name"), "args": {k: cut(v, limit) for k, v in (t.get("args") or {}).items()}}
                 for t in tu.get("tool_uses", [])]
        out = {"tool_calls": calls}
        if tu.get("content"):
            out["text"] = tu["content"]
        return out
    return {"other": cut(assistant, limit)}


def main():
    """Read conversations, write one YAML per conversation, an index and a zip."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.expanduser("~/kiro_sessions_export/out"))
    ap.add_argument("--since", default="", help="only conversations updated on/after YYYY-MM-DD")
    ap.add_argument("--cwd-filter", default="", help="substring of the working directory")
    ap.add_argument("--max-output", type=int, default=4000, help="max chars per tool output/arg (0 = all)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    shutil.copy2(DB, DB_COPY)  # snapshot: a running kiro-cli writes to the live DB
    try:
        con = sqlite3.connect(f"file:{DB_COPY}?mode=ro&immutable=1", uri=True)
        rows = con.execute("select key, conversation_id, value, created_at, updated_at "
                           "from conversations_v2 order by updated_at").fetchall()
        con.close()
    finally:
        os.remove(DB_COPY)
    index, files = [], []
    for key, cid, value, created, updated in rows:
        if a.since and to_dt(updated)[:10] < a.since:
            continue
        if a.cwd_filter and a.cwd_filter not in key:
            continue
        d = json.loads(value)
        hist = d.get("history") or []
        turns = []
        for e in hist:
            turns.append({"user": user_part(e.get("user", {}).get("content", {}), a.max_output),
                          "assistant": assistant_part(e.get("assistant"), a.max_output)})
        last = turns[-1]["assistant"] if turns else {}
        ended = "response" if "response" in last else ("tool_call (interrupted?)" if "tool_calls" in last else "unknown")
        meta = {"conversation_id": cid, "cwd": key, "created": to_dt(created), "updated": to_dt(updated),
                "turns": len(turns), "ended_with": ended}
        doc = dict(meta, transcript=d.get("transcript") or [], turns=turns)
        name = f"{to_dt(updated)[:10]}_{cid[:8]}_{os.path.basename(key.rstrip('/')) or 'home'}.yaml"
        path = os.path.join(a.out, name)
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(doc, fh, allow_unicode=True, sort_keys=False, width=120)
        files.append(path)
        prompts = [t["user"]["prompt"] for t in turns if "prompt" in t["user"]]
        index.append(dict(meta, file=name, first_prompt=cut(prompts[0], 300) if prompts else "",
                          last_prompt=cut(prompts[-1], 600) if prompts else ""))
    with open(os.path.join(a.out, "index.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump(index, fh, allow_unicode=True, sort_keys=False, width=120)
    zpath = a.out.rstrip("/") + ".zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files + [os.path.join(a.out, "index.yaml")]:
            z.write(p, os.path.basename(p))
    print(f"{len(files)} conversations -> {a.out}/ ; zip {zpath} ({os.path.getsize(zpath)//1024} KB)")


if __name__ == "__main__":
    main()
