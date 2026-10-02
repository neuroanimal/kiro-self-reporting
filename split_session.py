#!/usr/bin/env python3
"""Split one mixed kiro-cli session into two topic-separated exports.

Read-only: the live DB is never written. We snapshot it, read conversation
`4fda0b63-…` (workspace /home/estagrz/repo/github/bip_krk), split its
`history` pairs at a given boundary index, and for each half write:
  * transcript.md  — readable turn-by-turn log
  * session.jsonl  — kiro-cli JSONL (Prompt / AssistantMessage events)
No row is inserted anywhere; new sessions are started by opening each folder.
"""
# pylint: disable=too-many-locals  # write_part tracks several render counters
import json
import os
import shutil
import sqlite3

HOME = os.path.expanduser("~")
DB_LIVE = f"{HOME}/.local/share/kiro-cli/data.sqlite3"
DB_COPY = "/tmp/kiro_split_copy.sqlite3"
CONV_ID = "4fda0b63-31f5-4531-9bec-3d96ad4f2a37"

# Topic boundary established by inspection: pair [96] opens with
# "Dobra, temat ogarnięty ... kolejny temat" (switch to the electricity topic).
BOUNDARY = 96
PARTS = [
    {"name": "Szukanie Pracy (BIP Kraków)",
     "folder": f"{HOME}/repo/github/bip_krk",
     "range": (0, BOUNDARY)},            # pairs [0 .. 95]
    {"name": "Prąd w Pracowni",
     "folder": f"{HOME}/repo/github/craftsman_workshop",
     "range": (BOUNDARY, 10**9)},        # pairs [96 .. end]
]


def load_history():
    """Snapshot the live DB and return the conversation's history list."""
    shutil.copy2(DB_LIVE, DB_COPY)
    uri = f"file:{DB_COPY}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    row = con.execute(
        "SELECT value FROM conversations_v2 WHERE conversation_id=?",
        (CONV_ID,)).fetchone()
    con.close()
    return json.loads(row[0]).get("history", [])


def pair_texts(item):
    """Extract (user_prompt, assistant_text, tool_names, is_real_turn).

    A history pair is either a real user turn (user.content == Prompt /
    CancelledToolUses with a prompt) or an internal tool step
    (user.content == ToolUseResults). The assistant side is Response (final
    text) or ToolUse (interim text + the tools it called).
    """
    user = ((item or {}).get("user") or {}).get("content") or {}
    prompt = ""
    is_real = False
    if isinstance(user, dict):
        if "Prompt" in user:
            prompt = user["Prompt"].get("prompt", "")
            is_real = True
        # ToolUseResults / CancelledToolUses are internal, not user turns.

    asst = item.get("assistant") or {}
    atext = ""
    tools = []
    if isinstance(asst, dict):
        body = asst.get("Response") or asst.get("ToolUse") or {}
        if isinstance(body, dict):
            atext = body.get("content", "") or ""
            for tu in body.get("tool_uses", []) or []:
                name = tu.get("name") or tu.get("orig_name")
                if name:
                    tools.append(name)
    return prompt, atext, tools, is_real


def write_part(part, pairs):
    """Write transcript.md (readable) and session.jsonl (faithful) for a half.

    transcript.md shows real user turns with the assistant's final/interim
    text and a collapsed list of tools used in between. session.jsonl emits
    one Prompt per real user turn and one AssistantMessage carrying the
    assistant text, so it can be read back as a kiro-cli-style session.
    """
    os.makedirs(part["folder"], exist_ok=True)
    md_path = os.path.join(part["folder"], "transcript.md")
    jl_path = os.path.join(part["folder"], "session.jsonl")

    first, last = part["range"][0], part["range"][0] + len(pairs) - 1
    real_turns = 0
    with open(md_path, "w", encoding="utf-8") as md, \
            open(jl_path, "w", encoding="utf-8") as jl:
        md.write(f"# Transkrypt — {part['name']}\n\n")
        md.write(f"Wyodrębniono z sesji `{CONV_ID}`, pary history "
                 f"{first}–{last}. Kroki narzędziowe zwinięte do listy; "
                 f"pełny ślad jest w `session.jsonl`.\n")
        pending_tools = []
        for item in pairs:
            prompt, atext, tools, is_real = pair_texts(item)
            pending_tools.extend(tools)
            if is_real:
                real_turns += 1
                md.write(f"\n---\n\n## Tura {real_turns} — użytkownik\n\n")
                md.write(prompt.strip() + "\n")
                jl.write(json.dumps({"kind": "Prompt", "data": {
                    "content": [{"kind": "text", "data": prompt}]}},
                    ensure_ascii=False) + "\n")
            if atext.strip():
                md.write("\n**Asystent:**\n\n" + atext.strip() + "\n")
                jl.write(json.dumps({"kind": "AssistantMessage", "data": {
                    "content": [{"kind": "text", "data": atext}]}},
                    ensure_ascii=False) + "\n")
            if pending_tools and (is_real or atext.strip()):
                uniq = ", ".join(sorted(set(pending_tools)))
                md.write(f"\n_użyte narzędzia: {uniq}_\n")
                pending_tools = []
    return md_path, jl_path, len(pairs), real_turns


def main():
    """Split the session and report what was written."""
    history = load_history()
    for part in PARTS:
        start, end = part["range"]
        pairs = history[start:end]
        md_path, jl_path, npairs, real = write_part(part, pairs)
        print(f"{part['name']}: {npairs} par history, {real} realnych tur usera")
        print(f"  {md_path}")
        print(f"  {jl_path}")
    if os.path.exists(DB_COPY):
        os.remove(DB_COPY)
    print("kopia bazy usunięta; żywa baza nietknięta (tylko odczyt)")


if __name__ == "__main__":
    main()
