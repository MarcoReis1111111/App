import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import App_web_ui_moderno as m
html = m.HTML
for key in ["function tpBadge", "function teBadge"]:
    i = html.find(key)
    Path(__file__).resolve().parent.joinpath("AppEngenhariaCache", "badge_snip.txt").open("a", encoding="utf-8").write(
        f"\n=== {key} ===\n{html[i:i+500]}\n"
    )
