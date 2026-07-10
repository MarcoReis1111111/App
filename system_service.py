# -*- coding: utf-8 -*-
"""Página Sistema — informação local, cache e log."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    try:
        for root, _, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def tail_lines(path: Path, n: int = 100) -> List[str]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-max(1, int(n)) :]
    except Exception:
        return []


class SystemService:
    def info(
        self,
        cfg: Dict[str, Any],
        user: Dict[str, Any],
        version: str,
        started_at: str,
        cache_path: Path,
        log_path: Path,
        onedrive: Dict[str, Any],
    ) -> Dict[str, Any]:
        sql = cfg.get("sqlserver") or {}
        cache_mb = round(_dir_size(cache_path) / (1024 * 1024), 2)
        log_mb = round(log_path.stat().st_size / (1024 * 1024), 2) if log_path.exists() else 0
        return {
            "version": version,
            "started_at": started_at,
            "user": user,
            "database": {
                "backend": cfg.get("db_backend", "sqlserver"),
                "server": str(sql.get("server") or ""),
                "database": str(sql.get("database") or ""),
                "auth": str(sql.get("auth") or "windows"),
            },
            "onedrive": onedrive,
            "cache": {
                "path": str(cache_path),
                "size_mb": cache_mb,
            },
            "log": {
                "path": str(log_path),
                "size_mb": log_mb,
                "lines": tail_lines(log_path, 120),
            },
        }
