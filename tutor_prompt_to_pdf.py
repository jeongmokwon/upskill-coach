#!/usr/bin/env python3
"""Extract TUTOR_SYSTEM_PROMPT from coach.py and render a print-ready HTML."""

import os
import re
import subprocess
import sys

PROJECT_DIR = os.path.dirname(__file__)
COACH_PATH = os.path.join(PROJECT_DIR, "coach.py")
OUTPUT_HTML = os.path.join(PROJECT_DIR, "tutor_prompt_print.html")


def extract_prompt():
    src = open(COACH_PATH).read()
    # Grab the triple-quoted string literal assigned to TUTOR_SYSTEM_PROMPT
    m = re.search(r'TUTOR_SYSTEM_PROMPT\s*=\s*"""(.*?)"""', src, re.DOTALL)
    if not m:
        raise RuntimeError("Could not locate TUTOR_SYSTEM_PROMPT in coach.py")
    return m.group(1)


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_prompt_html(text: str) -> str:
    # Apply light markdown-ish formatting: headings, bold, inline code, lists.
    out = []
    in_code_block = False
    code_buffer = []
    for raw_line in text.split("\n"):
        line = raw_line.rstrip()

        # Fenced code block handling (``` ... ```)
        if line.strip().startswith("```"):
            if in_code_block:
                out.append(
                    '<pre class="block-code">'
                    + _escape("\n".join(code_buffer))
                    + "</pre>"
                )
                code_buffer = []
                in_code_block = False
            else:
                in_code_block = True
            continue
        if in_code_block:
            code_buffer.append(line)
            continue

        if not line.strip():
            out.append("<br>")
            continue

        # Headings (## h2, ### h3)
        if line.startswith("## "):
            out.append(f'<h2>{_escape(line[3:])}</h2>')
            continue
        if line.startswith("### "):
            out.append(f'<h3>{_escape(line[4:])}</h3>')
            continue

        escaped = _escape(line)
        # Bold **...**
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        # Inline code `...`
        escaped = re.sub(r"`([^`]+)`", r'<code class="inline-code">\1</code>', escaped)

        # JSON-looking line (starts with {"type"...)
        if line.strip().startswith('{"') and line.strip().endswith("}"):
            out.append(f'<pre class="json-line">{_escape(line)}</pre>')
            continue

        # Numbered / bulleted list item
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        indent_style = f' style="margin-left:{indent * 6}px"' if indent else ""

        if stripped.startswith("- "):
            out.append(f'<div class="bullet"{indent_style}>• {escaped[indent + 2:]}</div>')
        elif re.match(r"^\d+\.\s", stripped):
            out.append(f'<div class="numbered"{indent_style}>{escaped[indent:]}</div>')
        else:
            out.append(f'<p class="line"{indent_style}>{escaped[indent:]}</p>')
    return "\n".join(out)


def build_html(body_html: str, raw_len: int) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Upskill Coach — Tutor System Prompt</title>
<style>
  @page {{
    size: A4;
    margin: 12mm 14mm;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    background: #ffffff;
    color: #1f2328;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", sans-serif;
    font-size: 10pt;
    line-height: 1.55;
    padding: 20px 24px;
  }}
  .header {{
    text-align: center;
    margin-bottom: 14px;
    padding-bottom: 10px;
    border-bottom: 2px solid #d0d7de;
  }}
  .header h1 {{
    font-size: 14pt;
    color: #0550ae;
    font-weight: 700;
    letter-spacing: 0.5px;
  }}
  .header .sub {{
    font-size: 8pt;
    color: #656d76;
    margin-top: 4px;
  }}
  h2 {{
    font-size: 11.5pt;
    color: #0550ae;
    margin-top: 14px;
    margin-bottom: 6px;
    padding-bottom: 3px;
    border-bottom: 1px solid #d0d7de;
  }}
  h3 {{
    font-size: 10.5pt;
    color: #24292f;
    margin-top: 10px;
    margin-bottom: 4px;
  }}
  strong {{ color: #0a3069; }}
  p.line {{ margin-bottom: 3px; }}
  .bullet {{ margin-bottom: 3px; }}
  .numbered {{ margin-bottom: 3px; }}
  br {{ display: block; margin: 3px 0; }}
  .inline-code {{
    font-family: "SF Mono", "Menlo", monospace;
    font-size: 8.8pt;
    background: #f6f8fa;
    border: 1px solid #d0d7de;
    padding: 1px 4px;
    border-radius: 3px;
    color: #0550ae;
  }}
  .json-line {{
    font-family: "SF Mono", "Menlo", monospace;
    font-size: 8.5pt;
    background: #f6f8fa;
    border-left: 3px solid #0550ae;
    padding: 6px 10px;
    margin: 4px 0;
    white-space: pre-wrap;
    word-break: break-word;
    color: #0a3069;
  }}
  .block-code {{
    font-family: "SF Mono", "Menlo", monospace;
    font-size: 9pt;
    background: #f6f8fa;
    border: 1px solid #d0d7de;
    border-radius: 4px;
    padding: 8px 10px;
    margin: 6px 0;
    white-space: pre-wrap;
  }}
  @media print {{
    body {{ background: #ffffff !important; }}
    h2, h3 {{ break-after: avoid-page; }}
    .json-line, .block-code {{ break-inside: avoid; }}
  }}
</style>
</head>
<body>
  <div class="header">
    <h1>UPSKILL COACH — TUTOR SYSTEM PROMPT</h1>
    <div class="sub">TUTOR_SYSTEM_PROMPT constant &middot; {raw_len} characters &middot; extracted from coach.py</div>
  </div>
  {body_html}
</body>
</html>"""


def main():
    raw = extract_prompt()
    body = render_prompt_html(raw)
    html = build_html(body, len(raw))

    with open(OUTPUT_HTML, "w") as f:
        f.write(html)

    print(f"HTML written to: {OUTPUT_HTML}")
    print("Opening in browser — use Cmd+P to save as PDF")

    if sys.platform == "darwin":
        subprocess.run(["open", OUTPUT_HTML])
    elif sys.platform == "linux":
        subprocess.run(["xdg-open", OUTPUT_HTML])
    else:
        print(f"Open manually: file://{OUTPUT_HTML}")


if __name__ == "__main__":
    main()
