import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import App_web_ui_moderno as m

html = m.HTML
out = Path(__file__).resolve().parent / "AppEngenhariaCache" / "tasks_v1_snip.txt"
parts = []
for key in ['id="task-filters"', 'class="toolbar"><button class="btn primary" id="tb_new"']:
    i = html.find(key)
    if i >= 0:
        parts.append(f"=== {key} @ {i} ===\n{html[max(0,i-400):i+800]}\n")
out.write_text("\n".join(parts), encoding="utf-8")
print("ok", out)
