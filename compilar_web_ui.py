# -*- coding: utf-8 -*-
"""
Gera App_web_ui_COMPILADO.py — ficheiro único para a Web UI (como o monólito desktop).

Uso:
    python compilar_web_ui.py
    python compilar_web_ui.py --output App_web_ui_COMPILADO.py

O ficheiro gerado:
  - Embute todos os módulos *_service.py e dependências (EMBED_* + exec)
  - Embute o bytecode base da app (_app_web_ui_base.cpython-313.pyc)
  - Inclui os patches de runtime (notas, tarefas, programadas, etc.)
  - Não precisa de .py externos para arrancar (só config em AppEngenhariaCache)
"""
from __future__ import annotations

import argparse
import base64
import re
import textwrap
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_PYC = ROOT / "_app_web_ui_base.cpython-313.pyc"
FALLBACK_PYC = ROOT / "__pycache__" / "_recovered" / "App_web_ui_moderno.cpython-313.pyc"
RUNTIME_PY = ROOT / "App_web_ui_moderno.py"
DEFAULT_OUT = ROOT / "App_web_ui_COMPILADO.py"

# Ordem de carga (dependências primeiro)
EMBED_MODULES = (
    "tasks_common",
    "scheduled_tasks_engine",
    "excel_filters",
    "files_service",
    "gantt_service",
    "planning_service",
    "actions_service",
    "attachments_service",
    "archive_service",
    "tasks_service",
    "board_service",
    "dashboard_service",
    "project_service",
    "scheduled_service",
    "catalog_service",
    "system_service",
    "notes_service",
    "diagnostic_service",
)


def _read_pyc_b64() -> str:
    pyc = DEFAULT_PYC if DEFAULT_PYC.is_file() else FALLBACK_PYC
    if not pyc.is_file():
        raise FileNotFoundError(
            f"Bytecode base em falta. Copie para:\n  {DEFAULT_PYC}"
        )
    return base64.b64encode(pyc.read_bytes()).decode("ascii")


def _embed_block(name: str, source: str) -> str:
    # Raw string; escape se o módulo tiver ''' isolado (raro)
    if "'''" in source:
        raise ValueError(f"Módulo {name}.py contém ''' — não suportado no embed.")
    return f'\n# ==== BEGIN: embedded {name} ====\nEMBED_{name} = r\'\'\'{source}\'\'\'\n'


def _bootstrap_code() -> str:
    lines = [
        "def _bootstrap_embedded_modules():",
        '    """Regista módulos embutidos em sys.modules (padrão desktop EMBED_*)."""',
        "    import sys",
        "    import types",
    ]
    for name in EMBED_MODULES:
        lines.append(f"    if '{name}' not in sys.modules:")
        lines.append(f"        _m = types.ModuleType('{name}')")
        lines.append(f"        exec(EMBED_{name}, _m.__dict__)")
        lines.append(f"        _m.__file__ = '<embedded>/{name}.py'")
        lines.append(f"        sys.modules['{name}'] = _m")
    lines.append("")
    lines.append("_bootstrap_embedded_modules()")
    lines.append("")
    return "\n".join(lines)


def _patch_load_base_for_embedded(runtime: str) -> str:
    """Substitui _load_base para suportar bytecode embutido."""
    loader = textwrap.dedent(
        '''
        def _load_base():
            global _loading_base
            if _loading_base:
                raise RuntimeError("Recursão ao carregar bytecode base")
            _loading_base = True
            try:
                import marshal
                import types
                raw = base64.b64decode(_EMBEDDED_PYC_B64)
                code = marshal.loads(raw[16:])
                mod = types.ModuleType("_app_web_base")
                exec(code, mod.__dict__)
                mod.__file__ = str(Path(__file__).resolve())
                return mod
            finally:
                _loading_base = False
        '''
    ).strip()
    runtime = re.sub(
        r"def _load_base\(\):.*?finally:\s*\n\s*_loading_base = False",
        loader,
        runtime,
        count=1,
        flags=re.DOTALL,
    )
    runtime = runtime.replace(
        '_PYC = _BASE_DIR / "_app_web_ui_base.cpython-313.pyc"',
        '_EMBEDDED_PYC_B64 = ""  # preenchido pelo compilador',
        1,
    )
    runtime = re.sub(
        r"if not _PYC\.is_file\(\):.*?_PYC = _BASE_DIR / \"__pycache__\".*?\n",
        "",
        runtime,
        count=1,
        flags=re.DOTALL,
    )
    return runtime


def _extract_runtime_body() -> str:
    text = RUNTIME_PY.read_text(encoding="utf-8")
    m = re.search(r"^from __future__ import annotations", text, re.M)
    if not m:
        raise RuntimeError("App_web_ui_moderno.py: formato inesperado")
    body = text[m.start() :]
    body = re.sub(r"\nif __name__ == ['\"]__main__['\"]:.*\Z", "", body, flags=re.DOTALL)
    # Evitar duplicar future imports no ficheiro gerado
    body = re.sub(r"^from __future__ import annotations\s*\n", "", body, count=1)
    body = re.sub(
        r"^import importlib\.util\s*\nimport re\s*\nimport sys\s*\nimport traceback\s*\n"
        r"from pathlib import Path\s*\nfrom typing import Any\s*\n",
        "",
        body,
        count=1,
    )
    return _patch_load_base_for_embedded(body)


_PYI_IMPORTS = """
def _pyi_collect_imports():
    \"\"\"Imports estáticos para PyInstaller (módulos embutidos via exec).\"\"\"
    import calendar  # noqa: F401
    import contextlib  # noqa: F401
    import datetime  # noqa: F401
    import decimal  # noqa: F401
    import http.server  # noqa: F401
    import json  # noqa: F401
    import marshal  # noqa: F401
    import mimetypes  # noqa: F401
    import os  # noqa: F401
    import re  # noqa: F401
    import shutil  # noqa: F401
    import socketserver  # noqa: F401
    import subprocess  # noqa: F401
    import threading  # noqa: F401
    import time  # noqa: F401
    import types  # noqa: F401
    import uuid  # noqa: F401
    import webbrowser  # noqa: F401
    from collections import Counter, defaultdict  # noqa: F401
    try:
        import pyodbc  # type: ignore  # noqa: F401
    except ImportError:
        pass


_pyi_collect_imports()
"""


def build(output: Path) -> None:
    version_m = re.search(r'^APP_VERSION = "([^"]+)"', RUNTIME_PY.read_text(encoding="utf-8"), re.M)
    version = version_m.group(1) if version_m else "0.0.0"
    pyc_b64 = _read_pyc_b64()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    parts = [
        "# -*- coding: utf-8 -*-",
        '"""',
        f"App Engenharia — Web UI (ficheiro único v{version})",
        f"Gerado por compilar_web_ui.py em {stamp}",
        "Não editar manualmente — altere os módulos fonte e recompile.",
        '"""',
        "from __future__ import annotations",
        "",
        "import base64",
        "import importlib.util",
        "import re",
        "import sys",
        "import traceback",
        "from pathlib import Path",
        "from typing import Any",
        "",
    ]

    for name in EMBED_MODULES:
        path = ROOT / f"{name}.py"
        if not path.is_file():
            raise FileNotFoundError(f"Módulo em falta: {path}")
        parts.append(_embed_block(name, path.read_text(encoding="utf-8")))

    parts.append(_bootstrap_code())
    runtime = _extract_runtime_body().replace(
        '_EMBEDDED_PYC_B64 = ""  # preenchido pelo compilador',
        f'_EMBEDDED_PYC_B64 = "{pyc_b64}"',
        1,
    )
    parts.append(runtime)
    parts.append(_PYI_IMPORTS)
    parts.append("")
    parts.append('if __name__ == "__main__":')
    parts.append("    run()")
    parts.append("")

    out_text = "\n".join(parts)
    output.write_text(out_text, encoding="utf-8")
    size_kb = len(out_text.encode("utf-8")) // 1024
    print(f"OK: {output} ({size_kb} KB, v{version}, {len(EMBED_MODULES)} módulos embutidos)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Compilar Web UI num único ficheiro .py")
    ap.add_argument("--output", "-o", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    build(args.output.resolve())


if __name__ == "__main__":
    main()
