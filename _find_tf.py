import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import App_web_ui_moderno as m

html = m._patch_html(m._load_base().HTML)
for key in ["tf_overdue", "tf_blocked", "function tqs", "homeGoTasks"]:
    i = html.find(key)
    if i >= 0:
        Path(f"AppEngenhariaCache/_{key.replace(' ','')}.txt").write_text(
            html[max(0, i - 100) : i + 600], encoding="utf-8"
        )
