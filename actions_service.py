# -*- coding: utf-8 -*-
"""Serviço de ações / checklist (task_checklist)."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Dict, List, Optional

from tasks_common import (
    ACTION_STATUSES, AppError, TasksDataAccess, can_edit_role, jval, task_can_edit, task_visible,
)


def validate_action_save(v: Dict[str, Any]) -> None:
    if not str(v.get("item_text") or v.get("text") or "").strip():
        raise AppError("Texto da ação é obrigatório")
    if not str(v.get("owner") or "").strip():
        raise AppError("Owner é obrigatório")
    due = str(v.get("due_date") or "").strip()[:10]
    if not due:
        raise AppError("Prazo é obrigatório")
    st = str(v.get("status") or "Não iniciado").strip()
    if st not in ACTION_STATUSES:
        raise AppError(f"Estado inválido: {st}")


class ActionsService:
    def __init__(self, da: TasksDataAccess):
        self.da = da

    def _get_task_row(self, conn, task_id: str) -> Optional[Dict[str, Any]]:
        return self.da.fetch_task_row(conn, task_id)

    def _assert_task_access(self, task_id: str, username: str, display: str, role: str, need_edit: bool = False) -> Dict[str, Any]:
        with self.da.connect() as conn:
            row = self._get_task_row(conn, task_id)
        if not row:
            raise AppError("Tarefa não encontrada")
        if not task_visible(int(row.get("Private") or 0), row.get("CreatedBy", ""), row.get("Responsavel", ""), username, display, role):
            raise PermissionError("Sem permissão")
        if need_edit and not task_can_edit(row, username, display, role):
            raise PermissionError("Sem permissão para editar")
        return row

    def list_checklist(
        self, task_id: str, username: str, display: str, role: str, include_all: bool = True
    ) -> List[Dict[str, Any]]:
        self._assert_task_access(task_id, username, display, role)
        with self.da.connect() as conn:
            user_map = self.da.user_map(conn)
            cur = conn.cursor()
            kind_filter = "" if include_all else " AND COALESCE(kind,'CHECK')=N'ACTION'"
            cur.execute(f"""
SELECT id, item_text, COALESCE(done,0), COALESCE(ord,0), COALESCE(kind,'CHECK'),
       COALESCE(owner,''), COALESCE(workers,''), start_date, due_date,
       COALESCE(status,''), COALESCE(evidence,''), COALESCE(blocked_reason,''), COALESCE(item_uuid,'')
FROM dbo.task_checklist WHERE TaskID=?{kind_filter} ORDER BY ord, id;
""", (task_id,))
            cols = [d[0] for d in cur.description]
            out = []
            today = dt.date.today()
            for row in cur.fetchall():
                d = {k: jval(v) for k, v in zip(cols, row)}
                o = str(d.get("owner") or "")
                d["owner_display"] = user_map.get(o, o) if o else ""
                due_s = str(d.get("due_date") or "")[:10]
                st = str(d.get("status") or "")
                d["is_overdue"] = False
                if due_s and st != "Concluído":
                    try:
                        d["is_overdue"] = dt.date.fromisoformat(due_s) < today
                    except Exception:
                        pass
                d["is_blocked"] = st == "Bloqueado"
                d["is_done"] = bool(int(d.get("done") or 0)) or st == "Concluído"
                out.append(d)
            return out

    def list_actions(self, task_id: str, username: str, display: str, role: str) -> List[Dict[str, Any]]:
        return [x for x in self.list_checklist(task_id, username, display, role, include_all=False)]

    def progress_stats(self, actions: List[Dict[str, Any]]) -> Dict[str, Any]:
        total = len(actions)
        done = sum(1 for a in actions if a.get("is_done"))
        overdue = sum(1 for a in actions if a.get("is_overdue") and not a.get("is_done"))
        blocked = sum(1 for a in actions if a.get("is_blocked") and not a.get("is_done"))
        pct = round(100.0 * done / total, 1) if total else 0.0
        return {"total": total, "done": done, "overdue": overdue, "blocked": blocked, "percent": pct}

    def max_active_due_date(self, task_id: str) -> str:
        tid = str(task_id or "").strip()
        if not tid:
            return ""
        try:
            with self.da.connect() as conn:
                cur = conn.cursor()
                cur.execute("""
SELECT MAX(COALESCE(due_date,'')) FROM dbo.task_checklist
WHERE TaskID=? AND COALESCE(kind,'CHECK')=N'ACTION'
  AND COALESCE(status,'')!=N'Concluído' AND COALESCE(due_date,'')<>'';
""", (tid,))
                row = cur.fetchone()
                if row and row[0]:
                    return str(row[0]).strip()
        except Exception:
            pass
        return ""

    def insert_check(self, task_id: str, text: str, username: str, display: str, role: str) -> int:
        if not can_edit_role(role):
            raise PermissionError("Sem permissão")
        self._assert_task_access(task_id, username, display, role, need_edit=True)
        t = str(text or "").strip()
        if not t:
            raise AppError("Texto do check é obrigatório")
        item_uuid = uuid.uuid4().hex
        with self.da.lock, self.da.connect() as conn:
            cur = conn.cursor()
            cur.execute("""
INSERT INTO dbo.task_checklist(TaskID, item_text, kind, done, item_uuid, ord)
OUTPUT INSERTED.id VALUES (?, ?, N'CHECK', 0, ?, COALESCE((SELECT MAX(ord)+1 FROM dbo.task_checklist WHERE TaskID=?),0));
""", (task_id, t, item_uuid, task_id))
            new_id = int(cur.fetchone()[0])
            self.da.add_task_history(conn, task_id, username, "check_add", t[:200])
            conn.commit()
            return new_id

    def update_checklist_item(self, item_id: int, values: Dict[str, Any], username: str, display: str, role: str) -> None:
        if not can_edit_role(role):
            raise PermissionError("Sem permissão")
        with self.da.lock, self.da.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT TaskID, COALESCE(kind,'CHECK'), item_text, COALESCE(done,0), owner, workers, start_date, due_date, status, evidence, blocked_reason FROM dbo.task_checklist WHERE id=?;",
                (int(item_id),),
            )
            row = cur.fetchone()
            if not row:
                raise AppError("Item não encontrado")
            tid, kind, text, done, owner, workers, sd, dd, status, evidence, blocked = row
            task_row = self._get_task_row(conn, str(tid))
            if not task_row or not task_can_edit(task_row, username, display, role):
                raise PermissionError("Sem permissão")
            v = dict(values or {})
            if str(kind or "CHECK") == "CHECK":
                new_text = str(v.get("item_text", text) or "").strip()
                if not new_text:
                    raise AppError("Texto obrigatório")
                new_done = int(v.get("done", done) or 0)
                cur.execute("UPDATE dbo.task_checklist SET item_text=?, done=? WHERE id=?;", (new_text, new_done, int(item_id)))
            else:
                merged = {
                    "item_text": text, "owner": owner, "workers": workers,
                    "start_date": jval(sd), "due_date": jval(dd),
                    "status": status, "evidence": evidence, "blocked_reason": blocked,
                }
                merged.update({k: v[k] for k in v if v[k] is not None})
                merged["item_text"] = str(merged.get("item_text") or "").strip()
                validate_action_save(merged)
                st = str(merged.get("status") or "Não iniciado").strip()
                new_done = 1 if st == "Concluído" else 0
                cur.execute("""
UPDATE dbo.task_checklist SET item_text=?, owner=?, workers=?, start_date=?, due_date=?, status=?, evidence=?, blocked_reason=?, done=?
WHERE id=?;
""", (
                    merged["item_text"],
                    str(merged.get("owner") or "").strip(),
                    str(merged.get("workers") or "").strip(),
                    str(merged.get("start_date") or "")[:10] or None,
                    str(merged.get("due_date") or "")[:10] or None,
                    st,
                    str(merged.get("evidence") or "").strip(),
                    str(merged.get("blocked_reason") or "").strip(),
                    new_done,
                    int(item_id),
                ))
            conn.commit()

    def toggle_check_done(self, item_id: int, username: str, display: str, role: str) -> None:
        if not can_edit_role(role):
            raise PermissionError("Sem permissão")
        with self.da.lock, self.da.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT TaskID, COALESCE(kind,'CHECK'), COALESCE(done,0) FROM dbo.task_checklist WHERE id=?;", (int(item_id),))
            row = cur.fetchone()
            if not row:
                raise AppError("Item não encontrado")
            tid, kind, done = row
            if str(kind or "CHECK") != "CHECK":
                raise AppError("Apenas itens CHECK podem ser alternados assim")
            task_row = self._get_task_row(conn, str(tid))
            if not task_row or not task_can_edit(task_row, username, display, role):
                raise PermissionError("Sem permissão")
            cur.execute("UPDATE dbo.task_checklist SET done=? WHERE id=?;", (0 if int(done or 0) else 1, int(item_id)))
            self.da.add_task_history(conn, str(tid), username, "check_toggle", f"id={item_id}")
            conn.commit()

    def delete_checklist_item(self, item_id: int, username: str, display: str, role: str) -> None:
        self.delete_action(item_id, username, display, role)

    def insert_action(self, task_id: str, values: Dict[str, Any], username: str, display: str, role: str) -> int:
        if not can_edit_role(role):
            raise PermissionError("Sem permissão")
        self._assert_task_access(task_id, username, display, role, need_edit=True)
        v = dict(values or {})
        text = str(v.get("item_text") or v.get("text") or "").strip()
        v["item_text"] = text
        validate_action_save(v)
        status = str(v.get("status") or "Não iniciado").strip()
        done = 1 if status == "Concluído" else 0
        item_uuid = str(v.get("item_uuid") or uuid.uuid4().hex)
        with self.da.lock, self.da.connect() as conn:
            cur = conn.cursor()
            cur.execute("""
INSERT INTO dbo.task_checklist(TaskID, item_text, kind, owner, workers, start_date, due_date, status, evidence, blocked_reason, done, item_uuid, ord)
OUTPUT INSERTED.id VALUES (?, ?, N'ACTION', ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT MAX(ord)+1 FROM dbo.task_checklist WHERE TaskID=?),0));
""", (
                task_id, text,
                str(v.get("owner") or "").strip(),
                str(v.get("workers") or "").strip(),
                str(v.get("start_date") or "")[:10] or None,
                str(v.get("due_date") or "")[:10] or None,
                status,
                str(v.get("evidence") or "").strip(),
                str(v.get("blocked_reason") or "").strip(),
                done, item_uuid, task_id,
            ))
            new_id = int(cur.fetchone()[0])
            self.da.add_task_history(conn, task_id, username, "action_add", text[:200])
            conn.commit()
            return new_id

    def update_action(self, action_id: int, values: Dict[str, Any], username: str, display: str, role: str) -> None:
        if not can_edit_role(role):
            raise PermissionError("Sem permissão")
        with self.da.lock, self.da.connect() as conn:
            cur = conn.cursor()
            cur.execute("""
SELECT TaskID, item_text, owner, workers, start_date, due_date, status, evidence, blocked_reason
FROM dbo.task_checklist WHERE id=?;
""", (int(action_id),))
            row = cur.fetchone()
            if not row:
                raise AppError("Ação não encontrada")
            tid = str(row[0])
            task_row = self._get_task_row(conn, tid)
            if not task_row or not task_can_edit(task_row, username, display, role):
                raise PermissionError("Sem permissão")
            currow = {
                "item_text": row[1], "owner": row[2], "workers": row[3],
                "start_date": jval(row[4]), "due_date": jval(row[5]),
                "status": row[6], "evidence": row[7], "blocked_reason": row[8],
            }
            merged = {**currow, **{k: values[k] for k in values if values[k] is not None}}
            merged["item_text"] = str(merged.get("item_text") or "").strip()
            validate_action_save(merged)
            status = str(merged.get("status") or "Não iniciado").strip()
            done = 1 if status == "Concluído" else 0
            cur.execute("""
UPDATE dbo.task_checklist SET item_text=?, owner=?, workers=?, start_date=?, due_date=?, status=?, evidence=?, blocked_reason=?, done=?
WHERE id=?;
""", (
                merged["item_text"],
                str(merged.get("owner") or "").strip(),
                str(merged.get("workers") or "").strip(),
                str(merged.get("start_date") or "")[:10] or None,
                str(merged.get("due_date") or "")[:10] or None,
                status,
                str(merged.get("evidence") or "").strip(),
                str(merged.get("blocked_reason") or "").strip(),
                done,
                int(action_id),
            ))
            self.da.add_task_history(conn, tid, username, "action_update", merged["item_text"][:200])
            conn.commit()

    def delete_action(self, action_id: int, username: str, display: str, role: str) -> None:
        if not can_edit_role(role):
            raise PermissionError("Sem permissão")
        with self.da.lock, self.da.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT TaskID FROM dbo.task_checklist WHERE id=?;", (int(action_id),))
            row = cur.fetchone()
            if not row:
                raise AppError("Ação não encontrada")
            tid = str(row[0])
            task_row = self._get_task_row(conn, tid)
            if not task_row or not task_can_edit(task_row, username, display, role):
                raise PermissionError("Sem permissão")
            cur.execute("DELETE FROM dbo.task_checklist WHERE id=?;", (int(action_id),))
            self.da.add_task_history(conn, tid, username, "action_delete", f"id={action_id}")
            conn.commit()
