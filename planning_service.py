# -*- coding: utf-8 -*-
"""Serviço de planeamento — datas, duração, prazo automático, dependências."""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Set

from tasks_common import AppError, TasksDataAccess, can_edit_role, parse_date_iso, task_can_edit, task_visible


class PlanningService:
    def __init__(self, da: TasksDataAccess, actions_service=None):
        self.da = da
        self.actions = actions_service

    def calc_due_from_duration(self, inicio: str, amount: int, unit: str = "days") -> str:
        d0 = parse_date_iso(inicio)
        if not d0 or amount <= 0:
            return ""
        if str(unit or "days").lower().startswith("w"):
            delta = dt.timedelta(weeks=int(amount))
        else:
            delta = dt.timedelta(days=int(amount))
        return (d0 + delta).isoformat()

    def duration_days(self, inicio: str, prazo: str) -> Optional[int]:
        d0 = parse_date_iso(inicio)
        d1 = parse_date_iso(prazo)
        if not d0 or not d1:
            return None
        return (d1 - d0).days

    def max_active_action_due(self, task_id: str) -> str:
        if self.actions:
            return self.actions.max_active_due_date(task_id)
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

    def summary(self, task: Dict[str, Any], actions: List[Dict[str, Any]]) -> Dict[str, Any]:
        inicio = str(task.get("InicioPrevisto") or "")[:10]
        prazo = str(task.get("Prazo") or "")[:10]
        max_action_due = ""
        for a in actions:
            if a.get("is_done"):
                continue
            due = str(a.get("due_date") or "")[:10]
            if due and (not max_action_due or due > max_action_due):
                max_action_due = due
        if not max_action_due:
            max_action_due = self.max_active_action_due(str(task.get("TaskID") or ""))
        duration = self.duration_days(inicio, prazo)
        return {
            "inicio_previsto": inicio,
            "prazo": prazo,
            "duration_days": duration,
            "max_action_due": max_action_due,
            "prazo_from_actions_rule": "Prazo tarefa = MAX(prazo ações ativas)",
            "prazo_matches_max_action": bool(max_action_due and prazo == max_action_due),
            "suggested_prazo_from_actions": max_action_due,
        }

    def get_dependencies(self, task_id: str) -> List[str]:
        tid = str(task_id or "").strip()
        if not tid:
            return []
        try:
            with self.da.connect() as conn:
                cur = conn.cursor()
                cur.execute("SELECT depends_on FROM dbo.task_dependencies WHERE task_id=?;", (tid,))
                return [str(r[0]) for r in cur.fetchall() if str(r[0] or "").strip()]
        except Exception:
            return []

    def set_dependencies(self, task_id: str, deps: List[str], username: str, display: str, role: str) -> None:
        if not can_edit_role(role):
            raise PermissionError("Sem permissão")
        tid = str(task_id or "").strip()
        with self.da.lock, self.da.connect() as conn:
            row = self.da.fetch_task_row(conn, tid)
            if not row:
                raise AppError("Tarefa não encontrada")
            if not task_can_edit(row, username, display, role):
                raise PermissionError("Sem permissão")
            cur = conn.cursor()
            cur.execute("DELETE FROM dbo.task_dependencies WHERE task_id=?;", (tid,))
            seen: Set[str] = set()
            for d in deps or []:
                dep = str(d or "").strip()
                if not dep or dep == tid or dep in seen:
                    continue
                seen.add(dep)
                cur.execute("INSERT INTO dbo.task_dependencies(task_id, depends_on) VALUES(?,?);", (tid, dep))
            conn.commit()

    def recalc_task_start_from_deps(self, task_id: str) -> Optional[str]:
        """InicioPrevisto = MAX(Prazo) das dependências; Prazo preserva duração."""
        tid = str(task_id or "").strip()
        if not tid:
            return None
        try:
            with self.da.lock, self.da.connect() as conn:
                cur = conn.cursor()
                cur.execute("""
SELECT MAX(NULLIF(Prazo,'')) FROM dbo.tasks
WHERE TaskID IN (SELECT depends_on FROM dbo.task_dependencies WHERE task_id=?);
""", (tid,))
                max_due = (cur.fetchone() or [None])[0]
                if not max_due:
                    return None
                max_s = str(max_due)[:10]
                cur.execute("SELECT InicioPrevisto, Prazo FROM dbo.tasks WHERE TaskID=?;", (tid,))
                row = cur.fetchone() or ("", "")
                start_old = str(row[0] or "")[:10]
                end_old = str(row[1] or "")[:10]
                d_start = parse_date_iso(start_old)
                d_end = parse_date_iso(end_old)
                d_new_start = parse_date_iso(max_s)
                if not d_new_start:
                    return None
                delta_days = None
                if d_start and d_end:
                    delta_days = max(0, (d_end - d_start).days)
                if delta_days is None:
                    new_end = end_old or d_new_start.isoformat()
                else:
                    new_end = (d_new_start + dt.timedelta(days=delta_days)).isoformat()
                cur.execute(
                    "UPDATE dbo.tasks SET InicioPrevisto=?, Prazo=? WHERE TaskID=?;",
                    (max_s, new_end[:10], tid),
                )
                conn.commit()
                return max_s
        except Exception:
            return None

    def sync_prazo_from_actions(self, task_id: str, username: str, display: str, role: str) -> str:
        if not can_edit_role(role):
            raise PermissionError("Sem permissão")
        tid = str(task_id or "").strip()
        max_due = self.max_active_action_due(tid)
        if not max_due:
            return ""
        with self.da.lock, self.da.connect() as conn:
            row = self.da.fetch_task_row(conn, tid)
            if not row or not task_can_edit(row, username, display, role):
                raise PermissionError("Sem permissão")
            conn.cursor().execute("UPDATE dbo.tasks SET Prazo=? WHERE TaskID=?;", (max_due[:10], tid))
            conn.commit()
        return max_due[:10]
