# -*- coding: utf-8 -*-
"""Board Kanban — agrupamento por estado e mudança rápida de estado."""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

from tasks_common import AppError, can_edit_role

BOARD_COLUMNS = [
    {"key": "Não iniciado", "label": "Não iniciado", "color": "#EFEFEF"},
    {"key": "Em Progresso", "label": "Em Progresso", "color": "#BBE1FA"},
    {"key": "Bloqueado", "label": "Bloqueado", "color": "#FF9999"},
    {"key": "Concluído", "label": "Concluído", "color": "#90EE90"},
]

_COLUMN_KEYS = {c["key"] for c in BOARD_COLUMNS}
_STATE_TO_COLUMN = {"A Fazer": "Não iniciado"}


def column_for_estado(estado: str) -> str:
    e = str(estado or "").strip() or "Não iniciado"
    if e in _STATE_TO_COLUMN:
        return _STATE_TO_COLUMN[e]
    if e in _COLUMN_KEYS:
        return e
    return "Não iniciado"


def due_badge(prazo: str) -> Dict[str, str]:
    try:
        d = dt.date.fromisoformat(str(prazo or "")[:10])
        delta = (d - dt.date.today()).days
        if delta < 0:
            return {"text": f"Atraso ({abs(delta)}d)", "bg": "#FEE2E2", "fg": "#7A1E2D"}
        if delta == 0:
            return {"text": "HOJE", "bg": "#FEF9C3", "fg": "#6A5A00"}
        return {"text": f"D-{delta}", "bg": "#E0F2FE", "fg": "#1D4ED8"}
    except Exception:
        return {"text": "", "bg": "", "fg": ""}


def board_card(row: Dict[str, Any]) -> Dict[str, Any]:
    prazo = str(row.get("Prazo") or "")[:10]
    return {
        "TaskID": row.get("TaskID"),
        "Tarefa": row.get("Tarefa"),
        "Estado": row.get("Estado"),
        "column": column_for_estado(str(row.get("Estado") or "")),
        "Responsavel": row.get("Responsavel"),
        "Prioridade": row.get("Prioridade"),
        "Projeto": row.get("Projeto"),
        "Prazo": prazo,
        "NotifEmoji": row.get("NotifEmoji"),
        "Notificacoes": row.get("Notificacoes"),
        "is_overdue": bool(row.get("is_overdue")),
        "due_badge": due_badge(prazo),
    }


class BoardService:
    def __init__(self, tasks_service):
        self.tasks = tasks_service

    def list_board(
        self,
        filters: Dict[str, Any],
        username: str,
        display: str,
        role: str,
        cfg: Dict[str, Any],
    ) -> Dict[str, Any]:
        f = dict(filters or {})
        rows = self.tasks.list_tasks({}, username, display, role, cfg)
        estado_f = str(f.get("estado") or "Todos")
        prio_f = str(f.get("prioridade") or "Todas")
        proj_f = str(f.get("projeto") or "Todos")
        resp_f = str(f.get("responsavel") or "Todos")
        q = str(f.get("q") or "").strip().lower()

        filtered: List[Dict[str, Any]] = []
        for r in rows:
            if estado_f != "Todos" and str(r.get("Estado") or "") != estado_f:
                continue
            if prio_f not in ("", "Todas") and str(r.get("Prioridade") or "") != prio_f:
                continue
            if proj_f != "Todos" and str(r.get("Projeto") or "") != proj_f:
                continue
            if resp_f != "Todos" and str(r.get("Responsavel") or "") != resp_f:
                continue
            if q:
                blob = " ".join(
                    str(r.get(k) or "")
                    for k in ("TaskID", "Tarefa", "DescricaoNotas", "Responsavel", "Projeto")
                ).lower()
                if q not in blob:
                    continue
            filtered.append(r)

        columns: Dict[str, List[Dict[str, Any]]] = {c["key"]: [] for c in BOARD_COLUMNS}
        for r in filtered:
            card = board_card(r)
            col = card["column"]
            columns.setdefault(col, []).append(card)

        counts = {k: len(v) for k, v in columns.items()}
        return {
            "columns": BOARD_COLUMNS,
            "cards": columns,
            "counts": counts,
            "total": len(filtered),
        }

    def move_card(
        self,
        task_id: str,
        new_state: str,
        username: str,
        display: str,
        role: str,
        cfg: Dict[str, Any],
    ) -> None:
        if not can_edit_role(role):
            raise PermissionError("Sem permissão para mover tarefas")
        tid = str(task_id or "").strip()
        estado = str(new_state or "").strip()
        if estado not in _COLUMN_KEYS:
            raise AppError(f"Estado inválido: {estado}")
        row = self.tasks.get_task(tid, username, display, role, cfg)
        if not row:
            raise AppError("Tarefa não encontrada")
        payload = dict(row)
        payload["Estado"] = estado
        payload["RowVer"] = row.get("RowVer")
        self.tasks.update_task(tid, payload, username, display, role)
