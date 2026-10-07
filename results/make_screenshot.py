"""Render the saved red-team reports as a terminal-style HTML page (assets/redteam.html).
Screenshot it with headless Chrome to produce assets/redteam.png."""
import html
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from decide_gateway import redteam  # noqa: E402


def colorize(text):
    t = html.escape(text)
    t = re.sub(r"\bHIJACKED\b", '<b class="r">HIJACKED</b>', t)
    t = re.sub(r"(\s)ok$", r'\1<span class="g">ok</span>', t, flags=re.M)
    t = re.sub(r"(\s)wrong$", r'\1<span class="y">wrong</span>', t, flags=re.M)
    t = re.sub(r"order-flip", '<span class="m">order-flip</span>', t)
    t = re.sub(r"^(decide redteam.*)$", r'<b>\1</b>', t, flags=re.M)
    return t


def main():
    un = json.load(open(os.path.join(ROOT, "results", "rt-unhardened.json")))
    hd = json.load(open(os.path.join(ROOT, "results", "rt-hardened.json")))
    left = redteam.render(un, color=False).replace("backend: llm (llm)", "backend: qwen3.5-4b · prompt: plain")
    right = redteam.render(hd, color=False).replace("backend: llm (llm)", "backend: qwen3.5-4b · prompt: hardened")
    right = right.split("\n  summary")[1]
    page = f"""<!doctype html><meta charset="utf-8"><style>
body{{margin:0;background:#0d1117;font:13px/1.45 Menlo,Consolas,monospace;color:#c9d1d9}}
.w{{padding:22px 26px;width:1040px}} pre{{margin:0;white-space:pre}}
.r{{color:#ff6b6b}} .g{{color:#3fb950}} .y{{color:#d29922}} .m{{color:#bc8cff}}
.p{{color:#58a6ff}} h3{{margin:18px 0 6px;color:#58a6ff;font-size:13px}}
.box{{border:1px solid #30363d;border-radius:8px;padding:12px 16px;margin-top:14px;background:#161b22}}
</style><div class="w"><pre><span class="p">$</span> decide redteam --llm http://127.0.0.1:8080/v1 --unhardened
{colorize(left)}</pre>
<div class="box"><pre><span class="p">$</span> decide redteam --llm http://127.0.0.1:8080/v1   <span style="color:#8b949e"># default: hardened prompt</span>
{colorize(right.rstrip())}</pre></div></div>"""
    out = os.path.join(ROOT, "assets", "redteam.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(out)


if __name__ == "__main__":
    main()
