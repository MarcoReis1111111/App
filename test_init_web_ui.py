# -*- coding: utf-8 -*-
"""
Teste de inicialização da App Web UI.

Corre no mesmo ambiente que Iniciar_Web_UI.bat:
  py -3.13 test_init_web_ui.py

Não precisa de SQL para os testes de import/patches.
"""
from __future__ import annotations

import importlib
import socket
import sys
import traceback
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
results: list[tuple[str, str, str]] = []


def ok(name: str, detail: str = "") -> None:
    results.append(("OK", name, detail))
    print(f"[OK]   {name}" + (f" — {detail}" if detail else ""))


def fail(name: str, detail: str = "") -> None:
    results.append(("FAIL", name, detail))
    print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))


def warn(name: str, detail: str = "") -> None:
    results.append(("WARN", name, detail))
    print(f"[WARN] {name}" + (f" — {detail}" if detail else ""))


def _summary() -> int:
    print()
    print("===== RESUMO =====")
    print(dict(Counter(st for st, _, _ in results)))
    fails = [r for r in results if r[0] == "FAIL"]
    if fails:
        print("\nCorrigir primeiro:")
        for _, name, detail in fails:
            print(f"  - {name}: {detail}")
        print("\nDicas:")
        print("  1) py -3.13 --version   (tem de ser 3.13.x)")
        print("  2) py -3.13 -m pip install -r requirements-web.txt")
        print("  3) Confirmar _app_web_ui_base.cpython-313.pyc na mesma pasta")
        print("  4) Se porta em uso: Parar_Web_UI.bat ou fechar o terminal antigo")
        return 1
    print("\nInicialização OK — podes correr Iniciar_Web_UI.bat")
    return 0


def main() -> int:
    print("=== Teste de inicialização App Web UI ===")
    print(f"Pasta: {ROOT}")
    print(f"Python: {sys.version}")
    print()

    if sys.version_info[:2] == (3, 13):
        ok("Python 3.13", sys.version.split()[0])
    else:
        fail(
            "Python 3.13",
            f"encontrado {sys.version_info.major}.{sys.version_info.minor} — "
            "o bytecode _app_web_ui_base.cpython-313.pyc só corre em Python 3.13",
        )

    pyc = ROOT / "_app_web_ui_base.cpython-313.pyc"
    moderno = ROOT / "App_web_ui_moderno.py"
    if pyc.is_file():
        ok("bytecode base", f"{pyc.stat().st_size} bytes")
    else:
        fail("bytecode base", f"em falta: {pyc.name}")
    if moderno.is_file():
        ok("App_web_ui_moderno.py")
    else:
        fail("App_web_ui_moderno.py", "em falta")

    for mod in ("pyodbc", "pandas", "openpyxl"):
        try:
            importlib.import_module(mod)
            ok(f"dependência {mod}")
        except Exception as e:
            fail(f"dependência {mod}", f"{type(e).__name__}: {e}")

    sys.path.insert(0, str(ROOT))
    for mod in (
        "tasks_common",
        "tasks_service",
        "board_service",
        "dashboard_service",
        "archive_service",
        "excel_filters",
        "notes_service",
        "scheduled_service",
    ):
        try:
            importlib.import_module(mod)
            ok(f"import {mod}")
        except Exception as e:
            fail(f"import {mod}", f"{type(e).__name__}: {e}")

    if "App_web_ui_moderno" in sys.modules:
        del sys.modules["App_web_ui_moderno"]
    try:
        import App_web_ui_moderno as app

        ok("import App_web_ui_moderno", f"APP_VERSION={getattr(app, 'APP_VERSION', '?')}")
    except Exception as e:
        fail("import App_web_ui_moderno", f"{type(e).__name__}: {e}")
        traceback.print_exc()
        return _summary()

    html = getattr(app, "HTML", "") or ""
    ver = str(getattr(app, "APP_VERSION", ""))

    checks = [
        ("UI_BUILD na HTML", f"UI_BUILD='{ver}'" in html or f'UI_BUILD="{ver}"' in html),
        ("patch Dashboard Eficiência", "eficiencia" in html),
        ("patch Meu Dia", 'id="page-myday"' in html),
        ("patch DataConclusao UI", "td_f_data_conc" in html or "DataConclusao" in html),
        ("patch return detalhe", "_detailReturnPage" in html),
        ("patch prioridade Todas", "fill('db_prio_f',['Todas'" in html),
        ("símbolos run/Handler", callable(getattr(app, "run", None)) and hasattr(app, "Handler")),
    ]
    for name, cond in checks:
        if cond:
            ok(name)
        else:
            fail(name)

    show_done_ok = False
    try:
        for name, modb in list(sys.modules.items()):
            if name in ("_app_web_base", "_app_web_ui_base") or "app_web_base" in name:
                cand = getattr(modb, "dashboard_filters", None)
                if callable(cand):
                    show_done_ok = bool((cand({}) or {}).get("show_done") is True)
                    break
    except Exception as e:
        warn("dashboard_filters check", str(e))
    if show_done_ok:
        ok("dashboard_filters show_done")
    else:
        fail("dashboard_filters show_done")

    port = int(getattr(app, "DEFAULT_PORT", 8765) or 8765)
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        ok(f"porta {port} livre")
    except OSError as e:
        warn(f"porta {port}", f"em uso ou bloqueada: {e}")
    finally:
        s.close()

    return _summary()


if __name__ == "__main__":
    raise SystemExit(main())
