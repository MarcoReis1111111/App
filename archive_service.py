# -*- coding: utf-8 -*-
"""Arquivo de tarefas apagadas/concluídas."""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from tasks_common import TASK_DB_COLS, AppError, TasksDataAccess, can_edit_role, is_admin, jval, utcnow_iso

_CHECKLIST_COLS = (
    "id", "item_text", "kind", "done", "ord", "owner", "workers",
    "start_date", "due_date", "status", "evidence", "blocked_reason", "item_uuid",
)


class ArchiveService:
    def __init__(self, da: TasksDataAccess):
        self.da = da

    def _payload_from_row(self, row: Dict[str, Any], *, checklist: Optional[List[Dict[str, Any]]] = None,
                          task_deps: Optional[List[str]] = None,
                          action_deps: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        task_map = {c: str(row.get(c) or "") for c in TASK_DB_COLS}
        task_map["Private"] = int(row.get("Private") or 0)
        task_map["CreatedBy"] = str(row.get("CreatedBy") or "")
        # DataConclusao vazia → string vazia no JSON; restore trata NULL.
        return {
            "task": task_map,
            "checklist": list(checklist or []),
            "task_dependencies": list(task_deps or []),
            "action_dependencies": list(action_deps or []),
        }

    def _load_checklist(self, conn, task_id: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            cur = conn.cursor()
            cur.execute(
                """
SELECT id, item_text, COALESCE(kind,'CHECK'), COALESCE(done,0), COALESCE(ord,0),
       COALESCE(owner,''), COALESCE(workers,''), start_date, due_date,
       COALESCE(status,''), COALESCE(evidence,''), COALESCE(blocked_reason,''), COALESCE(item_uuid,'')
FROM dbo.task_checklist WHERE TaskID=? ORDER BY ord, id;
""",
                (task_id,),
            )
            for row in cur.fetchall():
                d = {k: jval(v) for k, v in zip(_CHECKLIST_COLS, row)}
                out.append(d)
        except Exception:
            pass
        return out

    def _load_task_deps(self, conn, task_id: str) -> List[str]:
        out: List[str] = []
        try:
            cur = conn.cursor()
            cur.execute("SELECT depends_on FROM dbo.task_dependencies WHERE task_id=?;", (task_id,))
            for (dep,) in cur.fetchall():
                s = str(dep or "").strip()
                if s:
                    out.append(s)
        except Exception:
            pass
        return out

    def _load_action_deps(self, conn, checklist: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Guarda deps de acções por item_uuid (ids mudam no restore)."""
        id_to_uuid = {
            int(it.get("id") or 0): str(it.get("item_uuid") or "").strip()
            for it in checklist
            if int(it.get("id") or 0) > 0
        }
        ids = [i for i in id_to_uuid.keys() if i > 0]
        if not ids:
            return []
        out: List[Dict[str, Any]] = []
        try:
            cur = conn.cursor()
            marks = ",".join(["?"] * len(ids))
            try:
                cur.execute(
                    f"SELECT action_id, depends_on, COALESCE(dep_type,''), COALESCE(lag_days,0) "
                    f"FROM dbo.action_dependencies WHERE ISNULL(is_deleted,0)=0 AND action_id IN ({marks});",
                    tuple(ids),
                )
            except Exception:
                cur.execute(
                    f"SELECT action_id, depends_on, COALESCE(dep_type,''), COALESCE(lag_days,0) "
                    f"FROM dbo.action_dependencies WHERE action_id IN ({marks});",
                    tuple(ids),
                )
            for aid, dep, dep_type, lag in cur.fetchall():
                au = id_to_uuid.get(int(aid or 0), "")
                du = id_to_uuid.get(int(dep or 0), "")
                if not au or not du:
                    continue
                out.append({
                    "action_uuid": au,
                    "depends_on_uuid": du,
                    "dep_type": str(dep_type or ""),
                    "lag_days": int(lag or 0),
                })
        except Exception:
            pass
        return out

    def archive_conn(self, conn, task_id: str, action: str, user: str, row: Dict[str, Any]) -> None:
        tid = str(task_id or "").strip()
        if not tid:
            return
        checklist = self._load_checklist(conn, tid)
        task_deps = self._load_task_deps(conn, tid)
        action_deps = self._load_action_deps(conn, checklist)
        try:
            payload_json = json.dumps(
                self._payload_from_row(row, checklist=checklist, task_deps=task_deps, action_deps=action_deps),
                ensure_ascii=False,
            )
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
                        "restorable": str(action or "").strip().lower() == "deleted",
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
        action = str(rec.get("action") or "").strip().lower()
        if action != "deleted":
            raise AppError(
                "Só é possível restaurar arquivos de tarefas apagadas "
                f"(este registo é '{rec.get('action') or action}')."
            )
        task_id = str(rec.get("TaskID") or "").strip()
        payload = rec.get("payload") or {}
        task = payload.get("task") if isinstance(payload, dict) else None
        if not isinstance(task, dict) or not task_id:
            raise AppError("Dados do arquivo inválidos")
        checklist = payload.get("checklist") if isinstance(payload.get("checklist"), list) else []
        task_deps = payload.get("task_dependencies") if isinstance(payload.get("task_dependencies"), list) else []
        action_deps = payload.get("action_dependencies") if isinstance(payload.get("action_dependencies"), list) else []

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

            uuid_to_new_id: Dict[str, int] = {}
            for it in checklist:
                if not isinstance(it, dict):
                    continue
                kind = str(it.get("kind") or "CHECK").strip().upper() or "CHECK"
                if kind not in ("CHECK", "ACTION"):
                    kind = "CHECK"
                item_uuid = str(it.get("item_uuid") or "").strip() or uuid.uuid4().hex
                text = str(it.get("item_text") or "").strip()
                status = str(it.get("status") or "").strip()
                try:
                    done = int(it.get("done") or 0)
                except Exception:
                    done = 0
                try:
                    ord_v = int(it.get("ord") or 0)
                except Exception:
                    ord_v = 0
                cur.execute(
                    """
INSERT INTO dbo.task_checklist(
    TaskID, item_text, kind, owner, workers, start_date, due_date,
    status, evidence, blocked_reason, done, item_uuid, ord
) OUTPUT INSERTED.id VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?);
""",
                    (
                        task_id,
                        text,
                        kind,
                        str(it.get("owner") or "").strip(),
                        str(it.get("workers") or "").strip(),
                        str(it.get("start_date") or "")[:10] or None,
                        str(it.get("due_date") or "")[:10] or None,
                        status,
                        str(it.get("evidence") or "").strip(),
                        str(it.get("blocked_reason") or "").strip(),
                        done,
                        item_uuid,
                        ord_v,
                    ),
                )
                new_id = int(cur.fetchone()[0])
                uuid_to_new_id[item_uuid] = new_id

            for dep in task_deps:
                dep_tid = str(dep or "").strip()
                if not dep_tid or dep_tid == task_id:
                    continue
                try:
                    cur.execute(
                        "INSERT INTO dbo.task_dependencies(task_id, depends_on) VALUES(?,?);",
                        (task_id, dep_tid),
                    )
                except Exception:
                    pass

            for ad in action_deps:
                if not isinstance(ad, dict):
                    continue
                au = str(ad.get("action_uuid") or "").strip()
                du = str(ad.get("depends_on_uuid") or "").strip()
                aid = uuid_to_new_id.get(au)
                did = uuid_to_new_id.get(du)
                if not aid or not did:
                    continue
                try:
                    cur.execute(
                        "INSERT INTO dbo.action_dependencies(action_id, depends_on, dep_type, lag_days) VALUES (?,?,?,?);",
                        (aid, did, str(ad.get("dep_type") or ""), int(ad.get("lag_days") or 0)),
                    )
                except Exception:
                    try:
                        cur.execute(
                            "INSERT INTO action_dependencies(action_id, depends_on, dep_type, lag_days) VALUES (?,?,?,?);",
                            (aid, did, str(ad.get("dep_type") or ""), int(ad.get("lag_days") or 0)),
                        )
                    except Exception:
                        pass

            conn.commit()
        return task_id

    def delete_archive(self, archive_id: int, role: str) -> None:
        if not is_admin(role):
            raise PermissionError("Apenas admin pode eliminar do arquivo")
        with self.da.lock, self.da.connect() as conn:
            conn.cursor().execute("DELETE FROM dbo.archived_tasks WHERE id=?;", (int(archive_id),))
            conn.commit()
