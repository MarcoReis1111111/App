# -*- coding: utf-8 -*-
"""Diagnóstico técnico da Web UI local (SQL, OneDrive, serviços, log)."""
from __future__ import annotations

import importlib
import os
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

SERVICE_MODULES = (
    "tasks_common",
    "tasks_service",
    "actions_service",
    "files_service",
    "catalog_service",
    "excel_filters",
    "notes_service",
    "scheduled_service",
    "board_service",
    "dashboard_service",
)


def _check(name: str, ok: bool, detail: str = "", level: str = "info") -> Dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail, "level": level}


def _log_errors(log_path: Path, limit: int = 50) -> List[str]:
    if not log_path.is_file():
        return []
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    hits: List[str] = []
    for line in reversed(lines):
        low = line.lower()
        if any(k in low for k in ("error", "traceback", "erro", "failed", "denied", "exception")):
            hits.append(line.strip())
            if len(hits) >= limit:
                break
    hits.reverse()
    return hits


def run_diagnostics(
    *,
    cfg: Dict[str, Any],
    user: Dict[str, Any],
    version: str,
    ui_build: str,
    host: str,
    port: int,
    cache_dir: Path,
    log_path: Path,
    onedrive: Dict[str, Any],
    connect_fn: Optional[Callable] = None,
    include_http: bool = True,
    log_label: str = "Log web_ui_local.log",
) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []

    # pyodbc
    try:
        import pyodbc  # type: ignore

        drivers = list(pyodbc.drivers())
        checks.append(_check("pyodbc", True, f"OK — {len(drivers)} driver(s) ODBC"))
    except ImportError as ex:
        checks.append(_check("pyodbc", False, str(ex), "error"))

    # Serviços
    missing: List[str] = []
    for mod in SERVICE_MODULES:
        try:
            importlib.import_module(mod)
        except Exception as ex:
            missing.append(f"{mod}: {ex}")
    checks.append(
        _check(
            "Serviços Python",
            not missing,
            "OK — " + ", ".join(SERVICE_MODULES) if not missing else "; ".join(missing),
            "error" if missing else "info",
        )
    )

    # Utilizador / role
    checks.append(
        _check(
            "Utilizador sessão",
            bool(str(user.get("username") or "").strip()),
            f"{user.get('display_name') or user.get('username')} ({user.get('role') or '?'})",
        )
    )

    # Versão
    checks.append(_check("Versão aplicação", True, f"App {version} / UI {ui_build}"))

    # Porta HTTP (apenas servidor Web UI)
    if include_http:
        port_ok = False
        port_detail = f"{host}:{port}"
        try:
            with socket.create_connection((host, int(port)), timeout=2):
                port_ok = True
                port_detail += " — a escutar"
        except OSError as ex:
            port_detail += f" — {ex}"
        checks.append(_check("Porta HTTP", port_ok, port_detail, "error" if not port_ok else "info"))
    else:
        checks.append(_check("Modo cliente", True, "App Flet (sem servidor HTTP local)"))

    # Cache / log
    checks.append(_check("AppEngenhariaCache", cache_dir.is_dir(), str(cache_dir)))
    checks.append(_check(log_label, log_path.is_file(), str(log_path)))

    # Escrita cache
    write_ok = False
    write_detail = ""
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        probe = cache_dir / ".write_probe"
        probe.write_text(datetime.utcnow().isoformat(), encoding="utf-8")
        probe.unlink(missing_ok=True)
        write_ok = True
        write_detail = "Escrita OK"
    except OSError as ex:
        write_detail = str(ex)
    checks.append(_check("Permissão escrita cache", write_ok, write_detail, "error" if not write_ok else "info"))

    # SQL Server
    sql = cfg.get("sqlserver") or {}
    sql_label = f"{sql.get('server') or '?'} / {sql.get('database') or '?'} ({sql.get('auth') or 'windows'})"
    if connect_fn is None:
        checks.append(_check("SQL Server", False, "connect_fn em falta", "error"))
    else:
        try:
            conn = connect_fn()
            try:
                cur = conn.cursor()
                cur.execute("SELECT TOP 1 TaskID FROM dbo.tasks;")
                row = cur.fetchone()
                checks.append(
                    _check(
                        "SQL Server (SELECT tasks)",
                        True,
                        f"{sql_label} — OK (ex.: {row[0] if row else 'vazio'})",
                    )
                )
            finally:
                conn.close()
        except Exception as ex:
            checks.append(_check("SQL Server (SELECT tasks)", False, f"{sql_label} — {ex}", "error"))

    # OneDrive
    od_root = str(onedrive.get("onedrive_root") or cfg.get("onedrive_app_folder") or cfg.get("onedrive_app_root") or "").strip()
    od_valid = bool(onedrive.get("valid"))
    od_exists = bool(od_root and os.path.isdir(od_root))
    basename_ok = os.path.basename(od_root).strip().lower() == "06 pasta da app" if od_root else False
    od_detail = od_root or (onedrive.get("message") or "Não configurado")
    if od_valid and od_exists and basename_ok:
        checks.append(_check("OneDrive (06 Pasta da App)", True, od_detail))
    else:
        parts = []
        if not od_root:
            parts.append("caminho vazio")
        elif not od_exists:
            parts.append("pasta não existe")
        elif not basename_ok:
            parts.append(f"nome pasta: {os.path.basename(od_root)}")
        if onedrive.get("message"):
            parts.append(str(onedrive.get("message")))
        checks.append(
            _check(
                "OneDrive (06 Pasta da App)",
                False,
                f"{od_detail} — " + "; ".join(parts) if parts else od_detail,
                "warn" if od_root else "error",
            )
        )

    errors = _log_errors(log_path, 50)
    summary_ok = all(c["ok"] for c in checks if c.get("level") != "warn")

    return {
        "ok": summary_ok,
        "checks": checks,
        "errors": errors,
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "python": sys.version.split()[0],
        "frozen": bool(getattr(sys, "frozen", False)),
    }
