#!/usr/bin/env python3
"""Convert ontology.json to a print-ready HTML with syntax highlighting."""

import json
import subprocess
import sys
import os

ONTOLOGY_PATH = os.path.join(os.path.dirname(__file__), "ontology.json")
OUTPUT_HTML = os.path.join(os.path.dirname(__file__), "ontology_print.html")

# ── manual syntax highlighter (no external deps) ─────────────────────
import re

# Print-friendly palette (white paper, dark ink)
C_KEY    = "#0550ae"   # deep blue — JSON keys
C_STR    = "#0a3069"   # dark navy — string values
C_NUM    = "#6f42c1"   # purple — numbers
C_BOOL   = "#cf222e"   # red — true/false/null
C_BRACE  = "#1f2328"   # near-black — brackets
C_PUNCT  = "#656d76"   # dark grey — commas, colons

def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def highlight_json(text: str) -> str:
    """Regex-based JSON syntax highlighter returning HTML spans."""
    out = []
    i = 0
    in_key_position = True  # after { or , we expect a key

    while i < len(text):
        ch = text[i]

        # whitespace
        if ch in " \t":
            out.append(ch)
            i += 1
            continue
        if ch == "\n":
            out.append("\n")
            i += 1
            in_key_position = True  # rough heuristic reset
            continue

        # strings
        if ch == '"':
            j = i + 1
            while j < len(text):
                if text[j] == '\\':
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                j += 1
            token = text[i:j]
            # Determine if this is a key (followed by ':') or a value
            rest = text[j:].lstrip()
            is_key = rest.startswith(":")
            color = C_KEY if is_key else C_STR
            out.append(f'<span style="color:{color}">{_escape(token)}</span>')
            i = j
            continue

        # numbers
        if ch in "-0123456789":
            m = re.match(r'-?\d+(\.\d+)?([eE][+-]?\d+)?', text[i:])
            if m:
                token = m.group()
                out.append(f'<span style="color:{C_NUM}">{_escape(token)}</span>')
                i += len(token)
                continue

        # booleans / null
        for kw in ("true", "false", "null"):
            if text[i:i+len(kw)] == kw:
                out.append(f'<span style="color:{C_BOOL}">{kw}</span>')
                i += len(kw)
                break
        else:
            # structural characters
            if ch in "{}[]":
                out.append(f'<span style="color:{C_BRACE}">{ch}</span>')
            elif ch in ",:":
                out.append(f'<span style="color:{C_PUNCT}">{ch}</span>')
            else:
                out.append(_escape(ch))
            i += 1
            continue
        # if kw matched, we already advanced i
        continue

    return "".join(out)


def main():
    with open(ONTOLOGY_PATH, "r") as f:
        raw = f.read()

    # Re-format with consistent indentation
    data = json.loads(raw)
    formatted = json.dumps(data, indent=4, ensure_ascii=False)
    highlighted = highlight_json(formatted)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Upskill Coach — Ontology v{data.get("ontology_version","1.0.0")}</title>
<style>
  @page {{
    size: A4;
    margin: 12mm 10mm;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    background: #ffffff;
    color: #1f2328;
    font-family: "SF Mono", "Fira Code", "JetBrains Mono", "Menlo", "Consolas", monospace;
    font-size: 7.5pt;
    line-height: 1.45;
    padding: 20px;
  }}
  .header {{
    text-align: center;
    margin-bottom: 16px;
    padding-bottom: 12px;
    border-bottom: 2px solid #d0d7de;
  }}
  .header h1 {{
    font-size: 14pt;
    color: #0550ae;
    font-weight: 700;
    letter-spacing: 1px;
  }}
  .header .sub {{
    font-size: 8pt;
    color: #656d76;
    margin-top: 4px;
  }}
  .legend {{
    display: flex;
    justify-content: center;
    gap: 20px;
    margin-bottom: 14px;
    font-size: 7pt;
    color: #656d76;
  }}
  .legend span {{
    display: flex;
    align-items: center;
    gap: 4px;
  }}
  .legend .dot {{
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
  }}
  pre {{
    white-space: pre-wrap;
    word-wrap: break-word;
    tab-size: 4;
  }}
  @media print {{
    body {{
      background: #ffffff !important;
    }}
  }}
</style>
</head>
<body>
  <div class="header">
    <h1>UPSKILL COACH — ONTOLOGY</h1>
    <div class="sub">v{data.get("ontology_version","1.0.0")} &middot; {len(data.get("user_states",[]))} user states &middot; {len(data.get("pedagogical_principles",[]))} pedagogical principles</div>
  </div>
  <div class="legend">
    <span><span class="dot" style="background:{C_KEY}"></span> key</span>
    <span><span class="dot" style="background:{C_STR}"></span> string</span>
    <span><span class="dot" style="background:{C_NUM}"></span> number</span>
    <span><span class="dot" style="background:{C_BOOL}"></span> bool/null</span>
    <span><span class="dot" style="background:{C_BRACE}"></span> bracket</span>
    <span><span class="dot" style="background:{C_PUNCT}"></span> punctuation</span>
  </div>
  <pre>{highlighted}</pre>
</body>
</html>"""

    with open(OUTPUT_HTML, "w") as f:
        f.write(html)

    print(f"HTML written to: {OUTPUT_HTML}")
    print("Opening in browser — use Cmd+P to save as PDF (enable 'Background graphics')")

    # Try to open in default browser
    if sys.platform == "darwin":
        subprocess.run(["open", OUTPUT_HTML])
    elif sys.platform == "linux":
        subprocess.run(["xdg-open", OUTPUT_HTML])
    else:
        print(f"Open manually: file://{OUTPUT_HTML}")

if __name__ == "__main__":
    main()
