#!/usr/bin/env python3
"""Build a combined HTML report of all historical AI chat sessions.

Sources, each with its own parser:
  * SQLite  — ~/.local/share/kiro-cli/data.sqlite3 (read from a /tmp copy,
              immutable mode, never touching the live DB) -> conversations_v2
  * JSON    — ~/.aws/amazonq/history/*.json (loki-js DB dump, 'tabs')
  * JSONL   — ~/.kiro/sessions/cli/*.jsonl (old kiro-cli session)
  * TXT     — ~/.cursor/.../agent-transcripts/*.txt (cursor transcripts)

Columns requested by the user:
  Date | Task Status | Talk Status | Tool | Model | Title | About | Ended-with
"""
# pylint: disable=line-too-long  # long CSS/HTML lines inside the report f-string
# pylint: disable=too-many-locals  # parsers legitimately juggle many fields
import glob
import html
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
# Live DB is never opened directly; we snapshot it to this copy and read that.
DB_LIVE = f"{HOME}/.local/share/kiro-cli/data.sqlite3"
DB_COPY = "/tmp/kiro_data_copy.sqlite3"
OUT_DIR = f"{HOME}/repo/github/kiro-self-reporting"

# Markers that signal the assistant wrapped up the task (PL + EN).
DONE_MARKERS = [
    "gotowe", "działa", "zrobione", "zrobiłem", "domknięt", "rozwiązan",
    "naprawione", "naprawiłem", "fixed", "done", "podsumowanie", "podsumowując",
    "all issues fixed", "all four issues", "ukończone", "zakończone",
    "commit msg", "commit message", "pylint 10", "10.00/10", "udało się",
    "sukces", "success", "zamykam",
]
# Markers that signal the work was left unfinished / to be continued.
UNFINISHED_MARKERS = [
    "working...", "working…", "prompt do", "po restarcie", "kontynu",
    "reszta później", "reszta póżniej", "zacznę od nowa", "later", "to be continued",
    "muszę zrobić restart", "przerw",
]


def ts(value):
    """Epoch (s/ms) or ISO -> YYYY-MM-DD HH:MM; '' on failure."""
    if value is None or value == "":
        return ""
    try:
        if isinstance(value, str) and "T" in value:
            return value.replace("T", " ")[:16]
        num = float(value)
        if num > 1e12:
            num /= 1000.0
        return datetime.fromtimestamp(num, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return str(value)[:16]


def clean(text, limit=None):
    """Collapse whitespace, unescape entities, optionally truncate."""
    if not text:
        return ""
    text = html.unescape(str(text))
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        return text[:limit] + "…"
    return text


def classify(last_assistant, last_transcript, has_summary, ended_compact):
    """Return (task_status, talk_status) from end-of-conversation signals."""
    low = (last_assistant or "").lower()
    # Talk status --------------------------------------------------------
    if ended_compact:
        talk = "zakończona (/compact)"
    elif any(m in low for m in UNFINISHED_MARKERS):
        talk = "ucięta / do kontynuacji"
    elif last_transcript and last_transcript.rstrip().endswith("]"):
        # ends on a "[Tool uses: ...]" line = cut off mid-action
        talk = "ucięta (w trakcie akcji)"
    elif any(m in low for m in ("zamykam", "dobranoc", "pozdrowienia",
                                "dziękuję", "do kontynuacji")):
        talk = "zakończona"
    else:
        talk = "zakończona (naturalnie)"
    if has_summary and "compact" not in talk:
        talk += " · był /compact"
    # Task status --------------------------------------------------------
    if any(m in low for m in UNFINISHED_MARKERS):
        task = "niedokończone"
    elif any(m in low for m in DONE_MARKERS):
        task = "ukończone"
    else:
        task = "nieokreślone"
    return task, talk


def parse_sqlite():
    """conversations_v2 from the immutable copy."""
    out = []
    uri = f"file:{DB_COPY}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    # conversation_id/created_at are selected for clarity but unused here.
    for key, _cid, value, _ca, ua in con.execute(
            "SELECT key,conversation_id,value,created_at,updated_at "
            "FROM conversations_v2"):
        try:
            doc = json.loads(value)
        except json.JSONDecodeError:
            continue
        tr = doc.get("transcript") or []
        hist = doc.get("history") or []
        model = (doc.get("model_info") or {}).get("model_name") or "?"
        has_summary = bool(doc.get("latest_summary"))
        # First user prompt (title in picker).
        first = ""
        for item in hist:
            prompt = (((item or {}).get("user") or {}).get("content") or {})
            if isinstance(prompt, dict) and "Prompt" in prompt:
                first = prompt["Prompt"].get("prompt", "")
                break
        if not first:
            first = next((t for t in tr if t.startswith(">")),
                         tr[0] if tr else "")
        last_tr = tr[-1] if tr else ""
        # Last assistant message = last transcript line not starting with '>'.
        last_asst = next((t for t in reversed(tr)
                          if not t.startswith(">")), "")
        ended_compact = last_tr.strip() in ("> /compact", "/compact")
        task, talk = classify(last_asst, last_tr, has_summary, ended_compact)
        out.append({
            "date": ts(ua), "sort": ua or 0,
            "tool": "kiro-cli (SQLite v2)", "model": model,
            "workspace": key,  # conversations_v2.key is the workspace path
            "task": task, "talk": talk,
            "title": first.lstrip("> ").strip(),
            "about": first.lstrip("> ").strip(),
            "ended": last_asst or last_tr,
            "msgs": len(tr), "src": f"data.sqlite3 · {key}",
        })
    con.close()
    return out


def parse_amazonq(path):
    """loki-js dump; conversations live under the 'tabs' collection."""
    out = []
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return out
    for coll in doc.get("collections", []):
        if coll.get("name") != "tabs":
            continue
        for tab in coll.get("data", []):
            if not isinstance(tab, dict):
                continue
            msgs = []
            for conv in tab.get("conversations", []):
                msgs.extend(conv.get("messages", []))
            if not msgs and not tab.get("title"):
                continue
            first = next((m.get("body", "") for m in msgs
                          if m.get("type") == "prompt"), "")
            # Workspace path, if the IDE recorded one in editorState.
            workspace = ""
            for msg in msgs:
                ectx = (msg.get("userInputMessageContext") or {})
                folders = ((ectx.get("editorState") or {})
                           .get("workspaceFolders") or [])
                if folders:
                    workspace = folders[0]
                    break
            last_asst = ""
            for msg in msgs:
                if msg.get("type") != "prompt" and msg.get("body"):
                    last_asst = msg["body"]
            # /compact in Amazon Q shows as a "## Conversation Summary" prompt.
            has_summary = first.strip().startswith("## Conversation Summary")
            task, talk = classify(last_asst, last_asst, has_summary, False)
            out.append({
                "date": ts(tab.get("updatedAt")), "sort": tab.get("updatedAt") or 0,
                "tool": "Amazon Q (IDE/CLI)", "model": "— (nie zapisany)",
                "workspace": workspace,
                "task": task, "talk": talk,
                "title": tab.get("title") or first,
                "about": first, "ended": last_asst,
                "msgs": len(msgs),
                "src": f"{os.path.basename(path)} · {tab.get('historyId','')[:8]}",
            })
    return out


def parse_kiro_jsonl(path):
    """Old kiro-cli .jsonl session."""
    first = last_asst = None
    nmsg = 0
    first_ts = last_ts = None
    with open(path, encoding="utf-8") as handle:
        lines = handle.readlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        data = rec.get("data", {})
        parts = [c.get("data", "") for c in data.get("content", [])
                 if isinstance(c, dict) and c.get("kind") == "text"]
        text = " ".join(p for p in parts if p)
        if rec.get("kind") == "Prompt":
            nmsg += 1
            if first is None:
                first = text
            mts = data.get("meta", {}).get("timestamp")
            first_ts = first_ts or mts
            last_ts = mts or last_ts
        elif rec.get("kind") == "AssistantMessage":
            nmsg += 1
            if text:
                last_asst = text
    task, talk = classify(last_asst, last_asst, False, False)
    return [{
        "date": ts(last_ts), "sort": (last_ts or 0) * 1000,
        "tool": "kiro-cli (JSONL legacy)", "model": "— (nie zapisany)",
        "workspace": "— (nie zapisany)",
        "task": task, "talk": talk,
        "title": first, "about": first, "ended": last_asst,
        "msgs": nmsg, "src": os.path.basename(path),
    }]


def parse_cursor(path):
    """cursor plain-text transcript."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        content = handle.read()
    mtime = os.path.getmtime(path)
    first = ""
    match = re.search(r"<user_query>(.*?)</user_query>", content, re.S)
    if match:
        first = match.group(1)
    task, talk = classify(content[-500:], content[-500:], False, False)
    # cursor project dir name is derived from the workspace path.
    proj = re.search(r"/projects/([^/]+)/", path)
    workspace = proj.group(1).replace("home-estagrz-", "/home/estagrz/") \
        .replace("-", "/") if proj else ""
    return [{
        "date": ts(mtime), "sort": mtime * 1000,
        "tool": "cursor (agent-transcript)", "model": "— (nie zapisany)",
        "workspace": workspace,
        "task": task, "talk": talk,
        "title": first, "about": first, "ended": content[-500:],
        "msgs": content.count("\nassistant:") + content.count("\nuser:"),
        "src": os.path.basename(path),
    }]


def collect(have_db):
    """Gather every record from every source."""
    recs = parse_sqlite() if have_db else []
    for path in sorted(glob.glob(f"{HOME}/.aws/amazonq/history/*.json")):
        if "backup" in path:
            continue
        recs += parse_amazonq(path)
    for path in sorted(glob.glob(f"{HOME}/.kiro/sessions/cli/*.jsonl")):
        recs += parse_kiro_jsonl(path)
    for path in sorted(glob.glob(
            f"{HOME}/.cursor/projects/*/agent-transcripts/*.txt")):
        recs += parse_cursor(path)
    recs.sort(key=lambda r: r["sort"], reverse=True)  # newest first
    return recs


def render_html(recs):
    """Produce the final self-contained interactive HTML string."""
    def esc(text, limit=None):
        return html.escape(clean(text, limit))

    tool_counts = {}
    for rec in recs:
        tool_counts[rec["tool"]] = tool_counts.get(rec["tool"], 0) + 1
    summary = " · ".join(f"{k}: {v}" for k, v in sorted(tool_counts.items()))

    rows = []
    for i, rec in enumerate(recs, 1):
        task_cls = {"ukończone": "ok", "niedokończone": "no"}.get(
            rec["task"], "na")
        talk_cls = "ok" if "zakończona" in rec["talk"] else "no"
        # data-sort on the date cell carries the epoch for correct ordering.
        rows.append(f"""<tr>
<td class="num">{i}</td>
<td class="date" data-sort="{rec['sort']}">{esc(rec['date'])}</td>
<td class="st {task_cls}">{esc(rec['task'])}</td>
<td class="st {talk_cls}">{esc(rec['talk'])}</td>
<td>{esc(rec['tool'])}</td>
<td class="model">{esc(rec['model'])}</td>
<td class="ws">{esc(rec.get('workspace',''))}</td>
<td class="msgs" data-sort="{rec['msgs']}">{rec['msgs']}</td>
<td class="title">{esc(rec['title'], 160)}</td>
<td class="about">{esc(rec['about'], 260)}</td>
<td class="ended">{esc(rec['ended'], 320)}</td>
<td class="src">{esc(rec['src'])}</td>
</tr>""")

    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Column headers; the per-column filter row is generated to match.
    headers = ["#", "Data (UTC)", "Task Status", "Talk Status", "Narzędzie",
               "Model LLM", "Workspace path", "Msgs", "Tytuł (1. prompt)",
               "Czego dotyczyła", "Na czym skończono", "Źródło"]
    head_html = "".join(f"<th data-col=\"{j}\">{h}</th>"
                        for j, h in enumerate(headers))
    # A text filter under every column except the row-number one.
    filt_html = "".join(
        "<th></th>" if j == 0
        else f"<th><input data-col=\"{j}\" placeholder=\"filtr…\"></th>"
        for j in range(len(headers)))

    return f"""<!DOCTYPE html>
<html lang="pl"><head><meta charset="utf-8">
<title>Raport historycznych sesji AI</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:1.2rem;color:#1d1d1f;background:#f5f5f7}}
 h1{{font-size:1.4rem;margin:0 0 .3rem}} .meta{{color:#555;font-size:.85rem;margin-bottom:.8rem}}
 .bar{{margin:.4rem 0 .8rem}} .bar input{{font-size:.9rem;padding:6px 10px;width:360px;max-width:90%;
   border:1px solid #bbb;border-radius:6px}}
 .bar button{{font-size:.8rem;padding:6px 10px;margin-left:6px;border:1px solid #bbb;border-radius:6px;
   background:#fff;cursor:pointer}} #count{{margin-left:10px;color:#555;font-size:.85rem}}
 table{{border-collapse:collapse;width:100%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.1);font-size:.78rem}}
 th,td{{border:1px solid #e0e0e0;padding:5px 7px;vertical-align:top;text-align:left}}
 thead th{{position:sticky;top:0;background:#0a4;color:#fff;z-index:3;font-size:.75rem;cursor:pointer;
   user-select:none;white-space:nowrap}}
 thead th:hover{{background:#097a37}} thead th.sorted-asc::after{{content:" ▲"}}
 thead th.sorted-desc::after{{content:" ▼"}}
 tr.filters th{{position:sticky;top:28px;background:#eef;z-index:2;padding:3px}}
 tr.filters input{{width:100%;box-sizing:border-box;font-size:.7rem;padding:3px;border:1px solid #ccd}}
 tbody tr:nth-child(even){{background:#fafafa}}
 .num{{color:#999;text-align:right}} .date{{white-space:nowrap;font-variant-numeric:tabular-nums}}
 .msgs{{text-align:right}} .model{{white-space:nowrap;font-weight:600}}
 .ws{{font-family:monospace;font-size:.7rem;color:#036;max-width:200px;word-break:break-all}}
 .st{{white-space:nowrap;font-weight:600;text-align:center}}
 .st.ok{{color:#0a7a2f}} .st.no{{color:#c0392b}} .st.na{{color:#999}}
 .title{{max-width:240px}} .about{{max-width:340px;color:#333}} .ended{{max-width:400px;color:#444}}
 .src{{font-family:monospace;font-size:.68rem;color:#777;max-width:200px;word-break:break-all}}
</style></head><body>
<h1>Raport historycznych sesji AI — <span id="total">{len(recs)}</span> rozmów</h1>
<div class="meta">Wygenerowano: {generated} · Źródła: {summary}<br>
SQLite czytany z kopii w trybie read-only/immutable — oryginał
<code>~/.local/share/kiro-cli/data.sqlite3</code> nietknięty.</div>
<div class="bar">
 <input id="q" placeholder="🔍 szukaj we wszystkich kolumnach…" autofocus>
 <button id="clear">Wyczyść filtry</button>
 <span id="count"></span>
</div>
<table id="t">
<thead>
<tr>{head_html}</tr>
<tr class="filters">{filt_html}</tr>
</thead>
<tbody>
{''.join(rows)}
</tbody></table>
<script>
(function(){{
 const table=document.getElementById('t');
 const tbody=table.tBodies[0];
 const rows=Array.from(tbody.rows);
 const q=document.getElementById('q');
 const colInputs=Array.from(document.querySelectorAll('tr.filters input'));
 const count=document.getElementById('count');
 const total={len(recs)};

 function apply(){{
   const g=q.value.toLowerCase();
   const cols=colInputs.map(i=>[+i.dataset.col, i.value.toLowerCase()])
                       .filter(x=>x[1]);
   let shown=0;
   for(const r of rows){{
     const cells=r.cells;
     let ok = !g || r.textContent.toLowerCase().includes(g);
     if(ok) for(const [c,v] of cols){{
       if(!(cells[c]&&cells[c].textContent.toLowerCase().includes(v))){{ok=false;break;}}
     }}
     r.style.display = ok ? '' : 'none';
     if(ok) shown++;
   }}
   count.textContent = shown+' / '+total+' widocznych';
 }}

 function sortBy(col, th){{
   const asc = !th.classList.contains('sorted-asc');
   document.querySelectorAll('thead th').forEach(h=>
     h.classList.remove('sorted-asc','sorted-desc'));
   th.classList.add(asc?'sorted-asc':'sorted-desc');
   const vis=rows.filter(r=>r.style.display!=='none');
   vis.sort((a,b)=>{{
     const ca=a.cells[col], cb=b.cells[col];
     const da=ca.dataset.sort, db=cb.dataset.sort;
     let x,y;
     if(da!==undefined&&db!==undefined){{x=+da;y=+db;}}
     else {{x=ca.textContent.trim().toLowerCase();y=cb.textContent.trim().toLowerCase();}}
     if(x<y) return asc?-1:1;
     if(x>y) return asc?1:-1;
     return 0;
   }});
   vis.forEach(r=>tbody.appendChild(r));
 }}

 document.querySelectorAll('thead tr:first-child th').forEach(th=>{{
   th.addEventListener('click',()=>sortBy(+th.dataset.col, th));
 }});
 q.addEventListener('input',apply);
 colInputs.forEach(i=>i.addEventListener('input',apply));
 document.getElementById('clear').addEventListener('click',()=>{{
   q.value=''; colInputs.forEach(i=>i.value=''); apply();
 }});
 apply();
}})();
</script>
</body></html>"""


def snapshot_db():
    """Copy the live SQLite DB to /tmp so we never lock or write the original.

    A plain file copy (not an sqlite connection) means the live DB is never
    opened by us; the copy is then read with mode=ro&immutable=1.
    """
    if not os.path.exists(DB_LIVE):
        return False
    shutil.copy2(DB_LIVE, DB_COPY)
    return True


def main():
    """Snapshot DB, collect from all sources, render HTML, clean up the copy."""
    have_db = snapshot_db()
    recs = collect(have_db)
    os.makedirs(OUT_DIR, exist_ok=True)
    out_html = os.path.join(OUT_DIR, "_report.ai.html")
    with open(out_html, "w", encoding="utf-8") as handle:
        handle.write(render_html(recs))
    if have_db and os.path.exists(DB_COPY):
        os.remove(DB_COPY)  # remove the 297 MB working copy from /tmp
    print(f"records: {len(recs)}")
    print(f"written: {out_html}")


if __name__ == "__main__":
    main()
