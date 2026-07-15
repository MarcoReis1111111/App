# -*- coding: utf-8 -*-
"""Arquivo de tarefas apagadas/concluídas."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from tasks_common import TASK_DB_COLS, AppError, TasksDataAccess, can_edit_role, is_admin, jval, utcnow_iso


class ArchiveService:
    def __init__(self, da: TasksDataAccess):
        self.da = da

    def _payload_from_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        task_map = {c: str(row.get(c) or "") for c in TASK_DB_COLS}
        task_map["Private"] = int(row.get("Private") or 0)
        task_map["CreatedBy"] = str(row.get("CreatedBy") or "")
        return {"task": task_map}

    def archive_conn(self, conn, task_id: str, action: str, user: str, row: Dict[str, Any]) -> None:
        tid = str(task_id or "").strip()
        if not tid:
            return
        try:
            payload_json = json.dumps(self._payload_from_row(row), ensure_ascii=False)
        except Exception:
            payload_json = "{}"
        conn.cursor().execute(
            "INSERT INTO dbo.archived_tasks (ts, TaskID, action, [user], payload_json) VALUES (?,?,?,?,?);",
            (utcnow_iso(), tid, str(action or ""), str(user or "-"), payload_json),
        )

    def list_archives(self, limit: int = 500) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            with self.da.connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"SELECT TOP {int(limit)} id, ts, TaskID, action, [user], payload_json "
                    "FROM dbo.archived_tasks ORDER BY id DESC;"
                )
                for rid, ts, tid, action, user, payload_json in cur.fetchall():
                    tarefa = ""
                    try:
                        payload = json.loads(payload_json or "{}")
                        tarefa = str((payload.get("task") or {}).get("Tarefa") or "")
                    except Exception:
                        pass
                    out.append({
                        "id": int(rid),
                        "ts": jval(ts),
                        "TaskID": str(tid or ""),
                        "action": str(action or ""),
                        "user": str(user or ""),
                        "Tarefa": tarefa,
                    })
        except Exception:
            pass
        return out

    def get_archive(self, archive_id: int) -> Optional[Dict[str, Any]]:
        try:
            with self.da.connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT id, ts, TaskID, action, [user], payload_json FROM dbo.archived_tasks WHERE id=?;",
                    (int(archive_id),),
                )
                row = cur.fetchone()
                if not row:
                    return None
                rid, ts, tid, action, user, payload_json = row
                payload = {}
                try:
                    payload = json.loads(payload_json or "{}")
                except Exception:
                    payload = {}
                return {
                    "id": int(rid),
                    "ts": jval(ts),
                    "TaskID": str(tid or ""),
                    "action": str(action or ""),
                    "user": str(user or ""),
                    "payload": payload,
                }
        except Exception:
            return None

    def restore(self, archive_id: int, username: str, display: str, role: str) -> str:
        if not can_edit_role(role):
            raise PermissionError("Sem permissão")
        rec = self.get_archive(archive_id)
        if not rec:
            raise AppError("Arquivo não encontrado")
        task_id = str(rec.get("TaskID") or "").strip()
        task = (rec.get("payload") or {}).get("task")
        if not isinstance(task, dict) or not task_id:
            raise AppError("Dados do arquivo inválidos")
        with self.da.lock, self.da.connect() as conn:
            self.da.ensure_tasks_data_conclusao(conn)
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM dbo.tasks WHERE TaskID=?;", (task_id,))
            if cur.fetchone():
                raise AppError(f"A tarefa {task_id} já existe")
            cols = list(TASK_DB_COLS)
            vals = []
            for c in cols:
                raw = task.get(c)
                if c == "DataConclusao" and (raw is None or str(raw).strip() == ""):
                    vals.append(None)
                else:
                    vals.append("" if raw is None else str(raw))
            cur.execute(
                f"INSERT INTO dbo.tasks ({', '.join(f'[{c}]' for c in cols)}) "
                f"VALUES ({', '.join(['?'] * len(cols))});",
                vals,
            )
            cur.execute(
                "UPDATE dbo.tasks SET Private=?, CreatedBy=? WHERE TaskID=?;",
                (int(task.get("Private") or 0), str(task.get("CreatedBy") or username), task_id),
            )
            conn.commit()
        return task_id

    def delete_archive(self, archive_id: int, role: str) -> None:
        if not is_admin(role):
            raise PermissionError("Apenas admin pode eliminar do arquivo")
        with self.da.lock, self.da.connect() as conn:
            conn.cursor().execute("DELETE FROM dbo.archived_tasks WHERE id=?;", (int(archive_id),))
            conn.commit()
