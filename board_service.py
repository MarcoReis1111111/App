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
_PRIO_ORDER = {"Alta": 0, "Média": 1, "Media": 1, "Baixa": 2}


def column_for_estado(estado: str) -> str:
    e = str(estado or "").strip() or "Não iniciado"
    if e in _STATE_TO_COLUMN:
        return _STATE_TO_COLUMN[e]
    if e in _COLUMN_KEYS:
        return e
    return "Não iniciado"


def due_badge(prazo: str, *, estado: str = "", data_conclusao: str = "") -> Dict[str, str]:
    """Badge de prazo. Em Concluído: mostra conclusão / atraso no fecho, nunca atraso activo."""
    st = str(estado or "").strip().lower()
    done = st in ("concluído", "concluido")
    conc_s = str(data_conclusao or "").strip()[:10]
    prazo_s = str(prazo or "").strip()[:10]
    if done:
        try:
            c = dt.date.fromisoformat(conc_s) if conc_s else None
            d = dt.date.fromisoformat(prazo_s) if prazo_s else None
            if c and d and c > d:
                lag = (c - d).days
                return {"text": f"Fechada +{lag}d", "bg": "#FEF3C7", "fg": "#92400E"}
            if c:
                return {"text": f"Concluída {c.isoformat()[5:]}", "bg": "#DCFCE7", "fg": "#166534"}
            return {"text": "Concluída", "bg": "#DCFCE7", "fg": "#166534"}
        except Exception:
            return {"text": "Concluída", "bg": "#DCFCE7", "fg": "#166534"}
    try:
        d = dt.date.fromisoformat(prazo_s)
        delta = (d - dt.date.today()).days
        if delta < 0:
            return {"text": f"Atraso ({abs(delta)}d)", "bg": "#FEE2E2", "fg": "#7A1E2D"}
        if delta == 0:
            return {"text": "HOJE", "bg": "#FEF9C3", "fg": "#6A5A00"}
        return {"text": f"D-{delta}", "bg": "#E0F2FE", "fg": "#1D4ED8"}
    except Exception:
        return {"text": "", "bg": "", "fg": ""}


def _truthy(v: Any) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def board_task_filters(filters: Dict[str, Any]) -> Dict[str, Any]:
    f = dict(filters or {})
    out: Dict[str, Any] = {
        "estado": str(f.get("estado") or "Todos"),
        "prioridade": str(f.get("prioridade") or "Todas"),
        "projeto": str(f.get("projeto") or "Todos"),
        "responsavel": str(f.get("responsavel") or "Todos"),
        "q": str(f.get("q") or "").strip(),
    }
    if _truthy(f.get("only_mine")):
        out["only_mine"] = True
    if _truthy(f.get("overdue_only")):
        out["overdue_only"] = True
    if _truthy(f.get("blocked_only")):
        out["blocked_only"] = True
    if _truthy(f.get("show_done")):
        out["show_done"] = True
    return out


def board_sort_key(card: Dict[str, Any]) -> tuple:
    overdue_rank = 0 if card.get("is_overdue") else 1
    prazo = str(card.get("Prazo") or "").strip()
    prazo_key = prazo if prazo else "9999-12-31"
    prio = _PRIO_ORDER.get(str(card.get("Prioridade") or "").strip(), 5)
    return (overdue_rank, prazo_key, prio)


def board_card(row: Dict[str, Any]) -> Dict[str, Any]:
    prazo = str(row.get("Prazo") or "")[:10]
    estado = str(row.get("Estado") or "").strip()
    data_conc = str(row.get("DataConclusao") or "")[:10]
    blocked_count = int(row.get("blocked_count") or 0)
    is_blocked = blocked_count > 0 or estado == "Bloqueado"
    is_overdue = bool(row.get("is_overdue"))
    return {
        "TaskID": row.get("TaskID"),
        "Tarefa": row.get("Tarefa"),
        "Estado": row.get("Estado"),
        "column": column_for_estado(estado),
        "Responsavel": row.get("Responsavel"),
        "Prioridade": row.get("Prioridade"),
        "Projeto": row.get("Projeto"),
        "Prazo": prazo,
        "DataConclusao": data_conc,
        "NotifEmoji": row.get("NotifEmoji"),
        "Notificacoes": row.get("Notificacoes"),
        "is_overdue": is_overdue,
        "is_blocked": is_blocked,
        "blocked_count": blocked_count,
        "due_badge": due_badge(prazo, estado=estado, data_conclusao=data_conc),
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
        task_f = board_task_filters(filters)
        rows = self.tasks.list_tasks(task_f, username, display, role, cfg)

        columns: Dict[str, List[Dict[str, Any]]] = {c["key"]: [] for c in BOARD_COLUMNS}
        for r in rows:
            card = board_card(r)
            col = card["column"]
            columns.setdefault(col, []).append(card)

        for col_key in columns:
            columns[col_key].sort(key=board_sort_key)

        counts = {k: len(v) for k, v in columns.items()}
        return {
            "columns": BOARD_COLUMNS,
            "cards": columns,
            "counts": counts,
            "total": len(rows),
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
