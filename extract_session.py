#!/usr/bin/env python3
"""Turn one conversation from an export zip (export_kiro_sessions.py) into plain text files.

The YAML files in the export are complete but hard to read: long strings are folded,
newlines are escaped. This script writes the conversation back as ordinary text, one
file per prompt and per answer, with real line breaks.

    python3 extract_session.py <export.zip> --list
        list the conversations in the zip (number, file, date, turns, first prompt)
    python3 extract_session.py <export.zip> --select 3 [--select d49c9fde ...]
        extract conversation number 3 (from --list) or the one whose file name
        contains the given text (e.g. a conversation id prefix); repeatable
    options:
        --out DIR       output root (default: C:\\Users\\estagrz\\Downloads\\_kiro_\\_output_)
        --with-tools    also write the tool calls and their outputs (tools.txt per exchange)

Output, per selected conversation:
    <out>/<yaml name without .yaml>/
        README.txt                 conversation id, working directory, dates, counts
        summary_NN.txt             the /compact summaries (what came before the kept history)
        transcript.txt             the chat as kiro-cli shows it
        20261104_093059123_01/prompt.txt     what the user typed (folder = start time + number)
        20261104_093059123_01/response.txt   every assistant text of that exchange, in order
        20261104_093059123_01/tools.txt      (with --with-tools) commands and outputs
        ..._00/                    tool output left from before the first kept prompt, if any
    <out>/<yaml name without .yaml>.zip   the folder above, zipped

An "exchange" starts with a user prompt and lasts until the next prompt; the tool
calls and tool results in between belong to it.
"""
# pylint: disable=line-too-long,too-many-locals  # CLI one-liners; main/extract track several counters
import argparse
import os
import sys
import zipfile

import yaml

DEFAULT_OUT = "/mnt/c/Users/estagrz/Downloads/_kiro_/_output_"


def load_index(zf):
    """Return the export's index.yaml entries, or a minimal list built from the file names."""
    if "index.yaml" in zf.namelist():
        return yaml.safe_load(zf.read("index.yaml")) or []
    return [{"file": n} for n in sorted(zf.namelist()) if n.endswith(".yaml")]


def pick(index, selectors):
    """Resolve each selector (1-based number or substring of the file name) to an index entry."""
    chosen = []
    for sel in selectors:
        if sel.isdigit() and 1 <= int(sel) <= len(index):
            chosen.append(index[int(sel) - 1])
            continue
        hits = [e for e in index if sel in e["file"]]
        if len(hits) != 1:
            sys.exit(f"selector {sel!r} matches {len(hits)} conversations; use --list and a number")
        chosen.append(hits[0])
    return chosen


def split_exchanges(turns):
    """Group turns into exchanges: dict(time, prompt, texts, tools), in stored order.

    An exchange starts at a real user prompt. Tool output that kiro-cli replayed as a prompt
    (after a compaction) is not a prompt: it goes to the tools of the current exchange, or,
    before the first real prompt, to exchange 0 ("before the first prompt of the kept history").
    """
    exchanges = []
    for turn in turns:
        user, assistant = turn.get("user") or {}, turn.get("assistant") or {}
        if "prompt" in user or not exchanges:
            exchanges.append({"time": turn.get("time") or "", "prompt": user.get("prompt"),
                              "texts": [], "tools": []})
        cur = exchanges[-1]
        if not cur["time"]:
            cur["time"] = turn.get("time") or ""
        if "tool_output_as_prompt" in user:
            cur["tools"].append(f"--- tool output (replayed as a prompt by kiro-cli):\n{user['tool_output_as_prompt']}")
        for res in user.get("tool_results") or []:
            cur["tools"].append(f"--- result ({res.get('status')}):\n{res.get('output', '')}")
        if "cancelled_tool_uses" in user:
            cur["tools"].append("--- cancelled tool uses")
        if assistant.get("response"):
            cur["texts"].append(assistant["response"])
        if assistant.get("text"):
            cur["texts"].append(assistant["text"])
        for call in assistant.get("tool_calls") or []:
            args = call.get("args") or {}
            body = "\n".join(f"{k}: {v}" for k, v in args.items())
            cur["tools"].append(f"--- call {call.get('tool')}:\n{body}")
    return exchanges


def folder_name(exchange, num, width):
    """YYYYMMDD_HHmmssSSS_NN from the exchange's start time; NNNN_no-time when none is stored."""
    stamp = exchange["time"]
    if len(stamp) >= 23:
        return f"{stamp[0:4]}{stamp[5:7]}{stamp[8:10]}_{stamp[11:13]}{stamp[14:16]}{stamp[17:19]}{stamp[20:23]}_{num:0{width}d}"
    return f"{num:0{width + 2}d}_no-time"


def write_text(path, text):
    """Write text with real newlines, ending with one newline."""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text.rstrip("\n") + "\n")


def extract(zf, entry, out_root, with_tools):
    """Write one conversation as folders of text files and zip the result; return the zip path."""
    doc = yaml.safe_load(zf.read(entry["file"]))
    stem = os.path.splitext(entry["file"])[0]
    base = os.path.join(out_root, stem)
    os.makedirs(base, exist_ok=True)
    exchanges = split_exchanges(doc.get("turns") or [])
    written = [os.path.join(base, "README.txt")]
    write_text(written[0], "\n".join([
        f"conversation_id: {doc.get('conversation_id')}",
        f"working directory: {doc.get('cwd')}",
        f"created: {doc.get('created')}   updated: {doc.get('updated')}",
        f"turns: {doc.get('turns')}   exchanges: {len(exchanges)}   ended with: {doc.get('ended_with')}",
        "",
        "Each folder YYYYMMDD_HHmmssSSS_NN is one exchange (start time of its first turn, then its",
        "number): prompt.txt, response.txt" + (", tools.txt" if with_tools else "") + ". Folder _00, if present,",
        "holds what was left of an exchange before the first prompt of the kept history (after /compact).",
        "summary_NN.txt are the /compact summaries; transcript.txt is the chat as kiro-cli shows it.",
        "Text marked 'content truncated due to length' was already cut by kiro-cli when it was stored."]))
    for idx, summary in enumerate(doc.get("summaries") or [], 1):
        path = os.path.join(base, f"summary_{idx:02d}.txt")
        write_text(path, summary if isinstance(summary, str) else yaml.safe_dump(summary, allow_unicode=True))
        written.append(path)
    if doc.get("transcript"):
        path = os.path.join(base, "transcript.txt")
        write_text(path, "\n\n".join(str(t) for t in doc["transcript"]))
        written.append(path)
    first_is_context = bool(exchanges) and exchanges[0]["prompt"] is None
    width = max(2, len(str(len(exchanges))))
    for idx, exch in enumerate(exchanges):
        num = idx if first_is_context else idx + 1
        folder = os.path.join(base, folder_name(exch, num, width))
        os.makedirs(folder, exist_ok=True)
        files = [("response.txt", "\n\n".join(exch["texts"]))]
        if exch["prompt"] is not None:
            files.insert(0, ("prompt.txt", exch["prompt"]))
        if with_tools and exch["tools"]:
            files.append(("tools.txt", "\n\n".join(exch["tools"])))
        for name, text in files:
            if name == "response.txt" and not text:
                continue
            path = os.path.join(folder, name)
            write_text(path, text)
            written.append(path)
    zpath = base + ".zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as out:
        for path in written:
            out.write(path, os.path.relpath(path, out_root))
    return zpath, len(exchanges)


def main():
    """Parse arguments, then list the zip or extract the selected conversations."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("zip", help="export zip written by export_kiro_sessions.py")
    ap.add_argument("--list", action="store_true", help="list the conversations and exit")
    ap.add_argument("--select", action="append", default=[], help="number from --list or part of the file name")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output root directory")
    ap.add_argument("--with-tools", action="store_true", help="also write tool calls and outputs")
    a = ap.parse_args()
    with zipfile.ZipFile(a.zip) as zf:
        index = load_index(zf)
        if a.list or not a.select:
            for num, e in enumerate(index, 1):
                first = (e.get("first_prompt") or "").replace("\n", " ")[:90]
                print(f"{num:3d}  {e['file']:60s} {e.get('updated', ''):16s} "
                      f"{str(e.get('turns', '')):>5s}  {first}")
            return
        os.makedirs(a.out, exist_ok=True)
        for entry in pick(index, a.select):
            zpath, count = extract(zf, entry, a.out, a.with_tools)
            print(f"{entry['file']}: {count} exchanges -> {os.path.splitext(zpath)[0]}/ and {zpath}")


if __name__ == "__main__":
    main()
