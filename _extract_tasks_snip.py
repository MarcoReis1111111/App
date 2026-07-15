import sys
import re
sys.path.insert(0, r"c:\Users\Marco Reis\OneDrive\[0]App_Engenharia_Web\02_WEB_UI")
import App_web_ui_moderno as m
html = m.HTML
for pat in [r'id="page-tasks"[^>]*>[\s\S]{0,8000}', r'section class="kpis"[\s\S]{0,2000}']:
    m2 = re.search(pat, html)
    if m2:
        out = m2.group(0)[:7500]
        open("_tasks_snip.txt", "w", encoding="utf-8").write(out)
        print("written", len(out))
        break
