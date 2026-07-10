# -*- coding: utf-8 -*-
"""Serviço de anexos (attachments.json em tasks_files) — alinhado com Tkinter."""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import shutil
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from files_service import (
    create_task_folder_on_disk,
    make_unique_path,
    resolve_task_folder,
    save_url_file,
    task_folder_rel,
    to_rel_under_app_root,
)
from tasks_common import AppError, TasksDataAccess, can_edit_role, task_can_edit, task_visible


class AttachmentsService:
    def __init__(self, da: TasksDataAccess, cache_dir_fn: Optional[Callable] = None):
        self.da = da
        self.cache_dir_fn = cache_dir_fn

    def _attachments_json_path(self, folder: str) -> str:
        return os.path.join(folder, "attachments.json")

    def _lock_path(self, folder: str) -> str:
        return os.path.join(folder, "attachments.json.lock")

    def _acquire_lock(self, lock_path: str, timeout: float = 5.0) -> bool:
        start = time.time()
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, f"{os.getpid()}|{time.time()}".encode("utf-8"))
                finally:
                    os.close(fd)
                return True
            except FileExistsError:
                try:
                    if (time.time() - os.path.getmtime(lock_path)) > 120:
                        os.remove(lock_path)
                        continue
                except Exception:
                    pass
                if (time.time() - start) > timeout:
                    return False
                time.sleep(0.1)
            except Exception:
                return False

    def _release_lock(self, lock_path: str) -> None:
        try:
            if os.path.isfile(lock_path):
                os.remove(lock_path)
        except Exception:
            pass

    @contextlib.contextmanager
    def _lock(self, folder: str):
        lock_path = self._lock_path(folder)
        ok = self._acquire_lock(lock_path)
        try:
            yield ok
        finally:
            if ok:
                self._release_lock(lock_path)

    def _read_raw(self, path: str) -> List[Dict[str, Any]]:
        try:
            if not os.path.isfile(path):
                return []
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
        except Exception:
            return []

    def _write_atomic(self, path: str, items: List[Dict[str, Any]]) -> None:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _task_folder(self, cfg: Dict[str, Any], task_id: str, pasta_field: str = "", ensure: bool = False) -> Tuple[str, str]:
        rel = (pasta_field or "").strip() or task_folder_rel(task_id)
        full = resolve_task_folder(cfg, rel, self.cache_dir_fn)
        if ensure and not full:
            fo = create_task_folder_on_disk(cfg, task_id, self.cache_dir_fn)
            rel = fo["rel"]
            full = fo["full"]
        if not full:
            raise AppError("Pasta da tarefa não disponível. Crie a pasta OneDrive primeiro.")
        os.makedirs(full, exist_ok=True)
        return rel, full

    def _normalize_meta(self, it: Dict[str, Any], folder_full: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
        name = str(it.get("nome") or it.get("name") or it.get("filename") or "")
        loc = str(it.get("caminho") or it.get("location") or it.get("path") or "")
        url = str(it.get("url") or "")
        att_id = str(it.get("id") or "")
        tipo = str(it.get("tipo") or it.get("type") or it.get("kind") or "")
        date_s = str(it.get("dataCriacao") or it.get("date") or it.get("added_at") or "")
        full_path = loc if os.path.isabs(loc) else os.path.join(folder_full, loc) if loc else ""
        size = int(it.get("tamanho") or it.get("size") or 0)
        if not size and full_path and os.path.isfile(full_path):
            try:
                size = os.path.getsize(full_path)
            except Exception:
                pass
        return {
            "id": att_id,
            "name": name,
            "type": tipo,
            "size": size,
            "size_mb": round(size / (1024 * 1024), 2) if size else 0,
            "date": date_s,
            "location": loc,
            "full_path": full_path,
            "url": url,
        }

    def list_for_task(
        self, cfg: Dict[str, Any], task_id: str, pasta_field: str = "", cache_dir_fn=None
    ) -> List[Dict[str, Any]]:
        try:
            _, full = self._task_folder(cfg, task_id, pasta_field, ensure=False)
        except AppError:
            return []
        if not full or not os.path.isdir(full):
            return []
        items = self._read_raw(self._attachments_json_path(full))
        return [self._normalize_meta(it, full, cfg) for it in items]

    def _max_bytes(self, cfg: Dict[str, Any]) -> int:
        mb = float(cfg.get("attachments_max_mb") or 200)
        return int(mb * 1024 * 1024)

    def _assert_edit(self, cfg: Dict[str, Any], task_id: str, username: str, display: str, role: str) -> Dict[str, Any]:
        if not can_edit_role(role):
            raise PermissionError("Sem permissão")
        with self.da.connect() as conn:
            row = self.da.fetch_task_row(conn, task_id)
        if not row:
            raise AppError("Tarefa não encontrada")
        if not task_visible(int(row.get("Private") or 0), row.get("CreatedBy", ""), row.get("Responsavel", ""), username, display, role):
            raise PermissionError("Sem permissão")
        if not task_can_edit(row, username, display, role):
            raise PermissionError("Sem permissão para editar")
        return row

    def add_files(
        self, cfg: Dict[str, Any], task_id: str, file_paths: List[str], username: str, display: str, role: str,
        pasta_field: str = "",
    ) -> List[Dict[str, Any]]:
        self._assert_edit(cfg, task_id, username, display, role)
        _, full = self._task_folder(cfg, task_id, pasta_field, ensure=True)
        max_bytes = self._max_bytes(cfg)
        added: List[Dict[str, Any]] = []
        json_path = self._attachments_json_path(full)

        def _copy_one(src: str) -> Optional[Dict[str, Any]]:
            if not os.path.isfile(src):
                return None
            size = os.path.getsize(src)
            if size > max_bytes:
                raise AppError(f"Ficheiro excede o limite ({max_bytes // (1024*1024)} MB): {os.path.basename(src)}")
            dst = make_unique_path(full, os.path.basename(src))
            shutil.copy2(src, dst)
            return {
                "id": str(uuid.uuid4()),
                "nome": os.path.basename(dst),
                "tipo": "Ficheiro",
                "caminho": to_rel_under_app_root(cfg, dst, self.cache_dir_fn),
                "url": "",
                "tamanho": size,
                "dataCriacao": dt.datetime.now().isoformat(timespec="seconds"),
            }

        with self._lock(full) as ok:
            if not ok:
                raise AppError("Não foi possível obter lock de attachments.json")
            items = self._read_raw(json_path)
            for src in file_paths or []:
                meta = _copy_one(str(src))
                if meta:
                    items.append(meta)
                    added.append(self._normalize_meta(meta, full, cfg))
            if added:
                self._write_atomic(json_path, items)
        return added

    def add_url(
        self, cfg: Dict[str, Any], task_id: str, url: str, username: str, display: str, role: str,
        pasta_field: str = "", hint: str = "",
    ) -> Dict[str, Any]:
        self._assert_edit(cfg, task_id, username, display, role)
        u = str(url or "").strip()
        if not u:
            raise AppError("URL em falta")
        _, full = self._task_folder(cfg, task_id, pasta_field, ensure=True)
        json_path = self._attachments_json_path(full)
        path = save_url_file(full, hint or os.path.basename(u) or "Link", u)
        meta = {
            "id": str(uuid.uuid4()),
            "nome": os.path.basename(path),
            "tipo": "Link",
            "caminho": to_rel_under_app_root(cfg, path, self.cache_dir_fn),
            "url": u,
            "tamanho": os.path.getsize(path) if os.path.isfile(path) else 0,
            "dataCriacao": dt.datetime.now().isoformat(timespec="seconds"),
        }
        with self._lock(full) as ok:
            if not ok:
                raise AppError("Não foi possível obter lock de attachments.json")
            items = self._read_raw(json_path)
            items.append(meta)
            self._write_atomic(json_path, items)
        return self._normalize_meta(meta, full, cfg)

    def remove(
        self, cfg: Dict[str, Any], task_id: str, att_id: str, username: str, display: str, role: str,
        pasta_field: str = "", delete_file: bool = False,
    ) -> None:
        self._assert_edit(cfg, task_id, username, display, role)
        _, full = self._task_folder(cfg, task_id, pasta_field, ensure=False)
        json_path = self._attachments_json_path(full)
        aid = str(att_id or "").strip()
        if not aid:
            raise AppError("ID do anexo em falta")
        with self._lock(full) as ok:
            if not ok:
                raise AppError("Não foi possível obter lock de attachments.json")
            items = self._read_raw(json_path)
            kept: List[Dict[str, Any]] = []
            removed: Optional[Dict[str, Any]] = None
            for it in items:
                if str(it.get("id") or "") == aid:
                    removed = it
                else:
                    kept.append(it)
            if not removed:
                raise AppError("Anexo não encontrado")
            self._write_atomic(json_path, kept)
        if delete_file and removed:
            loc = str(removed.get("caminho") or removed.get("location") or "")
            fp = loc if os.path.isabs(loc) else os.path.join(full, loc) if loc else ""
            if fp and os.path.isfile(fp):
                try:
                    os.remove(fp)
                except Exception:
                    pass

    def save_upload_bytes(
        self, cfg: Dict[str, Any], task_id: str, filename: str, data: bytes,
        username: str, display: str, role: str, pasta_field: str = "",
    ) -> Dict[str, Any]:
        self._assert_edit(cfg, task_id, username, display, role)
        max_bytes = self._max_bytes(cfg)
        if len(data) > max_bytes:
            raise AppError(f"Ficheiro excede o limite ({max_bytes // (1024*1024)} MB)")
        _, full = self._task_folder(cfg, task_id, pasta_field, ensure=True)
        safe_name = os.path.basename(str(filename or "upload.bin"))
        dst = make_unique_path(full, safe_name)
        with open(dst, "wb") as f:
            f.write(data)
        meta = {
            "id": str(uuid.uuid4()),
            "nome": os.path.basename(dst),
            "tipo": "Ficheiro",
            "caminho": to_rel_under_app_root(cfg, dst, self.cache_dir_fn),
            "url": "",
            "tamanho": len(data),
            "dataCriacao": dt.datetime.now().isoformat(timespec="seconds"),
        }
        json_path = self._attachments_json_path(full)
        with self._lock(full) as ok:
            if not ok:
                raise AppError("Não foi possível obter lock de attachments.json")
            items = self._read_raw(json_path)
            items.append(meta)
            self._write_atomic(json_path, items)
        return self._normalize_meta(meta, full, cfg)
