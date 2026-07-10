# -*- coding: utf-8 -*-
"""Gestão de pastas OneDrive / tasks_files (sem UI Tkinter)."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from tasks_common import AppError


def normalize_folder_path(path: str) -> str:
    p = (path or "").strip()
    return os.path.normpath(p) if p else ""


def is_abs_or_unc_path(path: str) -> bool:
    s = (path or "").strip()
    return bool(re.match(r"^[a-zA-Z]:[\\/]", s) or s.startswith("\\\\"))


def validate_onedrive_root(root: str) -> Tuple[bool, str]:
    r = normalize_folder_path(root)
    if not r:
        return False, "Pasta OneDrive nao definida."
    if os.path.basename(r).strip().lower() != "06 pasta da app":
        return False, "A pasta tem de ser '06 Pasta da App'."
    if not os.path.isdir(r):
        return False, f"Pasta nao encontrada: {r}"
    probe = os.path.join(r, f".app_write_test_{uuid.uuid4().hex}.tmp")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
    except Exception as ex:
        return False, f"Sem permissao de escrita em {r}: {ex}"
    return True, ""


def _cfg_username(cfg: Dict[str, Any]) -> str:
    return str(cfg.get("_web_username") or "").strip()


def load_user_onedrive(username: str, cache_dir_fn) -> str:
    if not str(username or "").strip():
        return ""
    safe = re.sub(r"[^\w\-.@]+", "_", str(username or "user").lower())
    p = cache_dir_fn() / "web_users" / f"{safe}.json"
    if not p.exists():
        return ""
    try:
        import json
        data = json.loads(p.read_text(encoding="utf-8")) or {}
        return normalize_folder_path(str(data.get("onedrive_app_root") or ""))
    except Exception:
        return ""


def resolve_app_root(cfg: Dict[str, Any], cache_dir_fn=None) -> str:
    username = _cfg_username(cfg)
    if username and cache_dir_fn:
        user_root = load_user_onedrive(username, cache_dir_fn)
        if user_root:
            ok, _ = validate_onedrive_root(user_root)
            if ok:
                cfg["onedrive_app_root"] = user_root
                cfg["onedrive_app_folder"] = user_root
                return user_root
    root = normalize_folder_path(
        str(cfg.get("onedrive_app_root") or cfg.get("onedrive_app_folder") or "")
    )
    ok, _ = validate_onedrive_root(root)
    if ok:
        cfg["onedrive_app_root"] = root
        cfg["onedrive_app_folder"] = root
        return root
    return ""


def join_app_root(cfg: Dict[str, Any], rel_path: str, cache_dir_fn=None) -> str:
    rp = (rel_path or "").strip().replace("/", os.sep).replace("\\", os.sep)
    if not rp:
        return ""
    if is_abs_or_unc_path(rp):
        return normalize_folder_path(rp)
    root = resolve_app_root(cfg, cache_dir_fn)
    if not root:
        return ""
    return normalize_folder_path(os.path.join(root, rp.lstrip("\\/")))


def task_folder_rel(task_id: str) -> str:
    return os.path.join("tasks_files", (task_id or "Task_NEW").strip())


def resolve_task_folder(cfg: Dict[str, Any], folder_value: str, cache_dir_fn=None) -> str:
    p = (folder_value or "").strip()
    if not p:
        return ""
    if is_abs_or_unc_path(p):
        return normalize_folder_path(p)
    full = join_app_root(cfg, p, cache_dir_fn)
    return full if full else ""


def open_folder_path(path: str, log_fn=None) -> bool:
    p = normalize_folder_path(path)
    if not p or not os.path.isdir(p):
        return False
    try:
        if os.name == "nt":
            os.startfile(p)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
        return True
    except Exception as ex:
        if log_fn:
            log_fn(f"open_folder_path: {ex}")
        return False


def create_task_folder_on_disk(cfg: Dict[str, Any], task_id: str, cache_dir_fn=None) -> Dict[str, Any]:
    rel = task_folder_rel(task_id)
    full = join_app_root(cfg, rel, cache_dir_fn)
    if not full:
        raise AppError("Root OneDrive invalida. Configure '06 Pasta da App'.")
    os.makedirs(full, exist_ok=True)
    return {"rel": rel, "full": full, "exists": os.path.isdir(full)}


def to_rel_under_app_root(cfg: Dict[str, Any], abs_path: str, cache_dir_fn=None) -> str:
    ap = normalize_folder_path(abs_path)
    if not ap:
        return ""
    root = resolve_app_root(cfg, cache_dir_fn)
    if not root:
        return ap
    try:
        ap_n = os.path.normcase(ap)
        root_n = os.path.normcase(root)
        if ap_n == root_n:
            return "."
        if ap_n.startswith(root_n + os.sep):
            return os.path.relpath(ap, root)
    except Exception:
        pass
    return ap


def make_unique_path(folder: str, name: str) -> str:
    base, ext = os.path.splitext(name)
    candidate = os.path.join(folder, name)
    n = 1
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{base}_{n}{ext}")
        n += 1
    return candidate


def save_url_file(folder: str, hint: str, url: str) -> str:
    name = (hint or "Link").strip()
    if not name.lower().endswith(".url"):
        name = f"{name}.url"
    path = make_unique_path(folder, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write("[InternetShortcut]\n")
        f.write(f"URL={url.strip()}\n")
    return path


def folder_info(cfg: Dict[str, Any], task_id: str, pasta_field: str = "", cache_dir_fn=None) -> Dict[str, Any]:
    rel = (pasta_field or "").strip() or task_folder_rel(task_id)
    full = resolve_task_folder(cfg, rel, cache_dir_fn)
    root = resolve_app_root(cfg, cache_dir_fn)
    ok_root, root_msg = validate_onedrive_root(root) if root else (False, "OneDrive nao configurada")
    return {
        "rel": rel,
        "full": full,
        "exists": bool(full and os.path.isdir(full)),
        "onedrive_root": root,
        "onedrive_valid": ok_root,
        "onedrive_message": root_msg,
        "suggested_rel": task_folder_rel(task_id),
    }
