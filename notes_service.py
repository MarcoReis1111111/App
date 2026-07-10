# -*- coding: utf-8 -*-
"""Notas técnicas por utilizador — ficheiro texto (igual ao desktop)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Callable, Dict, Optional


def notes_file_path(base_dir: Path, username: str) -> Path:
    user = str(username or "anon").replace(" ", "_")
    return Path(base_dir) / f"notes_{user}.txt"


class NotesService:
    """Lê/grava notes_{user}.txt em BASE_DIR (como _build_notes_tab no desktop)."""

    def __init__(
        self,
        base_dir_fn: Callable[[], Path],
        log_fn: Optional[Callable[[str], None]] = None,
    ):
        self.base_dir_fn = base_dir_fn
        self.log_fn = log_fn

    def read(self, username: str) -> Dict[str, Any]:
        path = notes_file_path(self.base_dir_fn(), username)
        content = ""
        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8")
            except Exception:
                content = ""
        return {
            "content": content,
            "path": str(path),
            "username": str(username or ""),
            "exists": path.is_file(),
        }

    def save(self, username: str, content: str) -> Dict[str, Any]:
        path = notes_file_path(self.base_dir_fn(), username)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content or "", encoding="utf-8")
        saved_at = dt.datetime.now().strftime("%H:%M")
        if self.log_fn:
            try:
                self.log_fn(f"NOTES | notes_save | {username} | {path}")
            except Exception:
                pass
        return {
            "ok": True,
            "path": str(path),
            "saved_at": saved_at,
        }
