# -*- coding: utf-8 -*-
"""Filtros estilo Excel por coluna (mesma lógica que o Tkinter)."""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, List, Optional, Set

EXCEL_FILTER_COLS = [
    "TaskID", "Tarefa", "DescricaoNotas", "Milestone", "Assunto", "DataRegisto", "InicioPrevisto",
    "Responsavel", "Workers", "Estado", "Prioridade", "Notificacoes", "NotifEmoji", "Prazo",
    "Projeto", "Linha", "Maquina", "Pasta", "ResultadoInicial", "ResultadoFinal", "Links",
]


def default_excel_filters() -> Dict[str, Dict[str, Any]]:
    return {
        c: {
            "selected_values": None,
            "text_op": "contains",
            "text_query": "",
            "date_op": None,
            "date_a": "",
            "date_b": "",
        }
        for c in EXCEL_FILTER_COLS
    }


def parse_excel_filters(raw: Any) -> Dict[str, Dict[str, Any]]:
    base = default_excel_filters()
    if not raw:
        return base
    data = raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return base
        try:
            data = json.loads(s)
        except Exception:
            return base
    if not isinstance(data, dict):
        return base
    for col in EXCEL_FILTER_COLS:
        f = data.get(col)
        if not isinstance(f, dict):
            continue
        sel = f.get("selected_values")
        base[col] = {
            "selected_values": set(sel) if isinstance(sel, list) and sel else None,
            "text_op": str(f.get("text_op") or "contains"),
            "text_query": str(f.get("text_query") or ""),
            "date_op": f.get("date_op") or None,
            "date_a": str(f.get("date_a") or ""),
            "date_b": str(f.get("date_b") or ""),
        }
    return base


def excel_filters_active(filters: Dict[str, Dict[str, Any]]) -> bool:
    for col, f in (filters or {}).items():
        if f.get("selected_values") is not None:
            return True
        if str(f.get("text_query") or "").strip():
            return True
        if col in ("DataRegisto", "Prazo", "InicioPrevisto") and f.get("date_op"):
            return True
    return False


def _text_matches(field_val: Any, op: str, query: str) -> bool:
    v = str(field_val or "")
    q = str(query or "")
    if not q:
        return True
    v_low, q_low = v.lower(), q.lower()
    op = str(op or "contains")
    if op == "equals":
        return v == q
    if op == "starts":
        return v_low.startswith(q_low)
    if op == "ends":
        return v_low.endswith(q_low)
    if op == "not_contains":
        return q_low not in v_low
    return q_low in v_low


def _parse_date(s: Any) -> Optional[dt.date]:
    if not s:
        return None
    try:
        return dt.datetime.strptime(str(s).strip()[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _date_matches(field_val: Any, op: str, a: str, b: Optional[str] = None) -> bool:
    if not op:
        return True
    if not field_val:
        return False
    val_date = _parse_date(field_val)
    if not val_date:
        return False
    a_date = _parse_date(a)
    b_date = _parse_date(b) if b else None
    op = str(op)
    if op == "on":
        return a_date is not None and val_date == a_date
    if op == "before":
        return a_date is not None and val_date < a_date
    if op == "after":
        return a_date is not None and val_date > a_date
    if op == "between":
        if a_date is None or b_date is None:
            return False
        if a_date > b_date:
            a_date, b_date = b_date, a_date
        return a_date <= val_date <= b_date
    return True


def _selected_values_match(col: str, cell: Any, sel_vals: Set[str]) -> bool:
    if col == "Workers":
        comps = [s.strip() for s in str(cell or "").split(",") if s.strip()]
        for sv in sel_vals:
            if str(sv) == "" and not str(cell or "").strip():
                return True
            if sv == cell:
                return True
            for comp in comps:
                if str(sv).lower() == comp.lower() or str(sv).lower() in comp.lower():
                    return True
        return False
    if col == "Notificacoes":
        comps = [s.strip() for s in str(cell or "").split(";") if s.strip()]
        for sv in sel_vals:
            for comp in comps:
                if str(sv).lower() == comp.lower():
                    return True
        return False
    return str(cell or "") in sel_vals


def row_matches_excel_filters(row: Dict[str, Any], filters: Dict[str, Dict[str, Any]]) -> bool:
    if not filters:
        return True
    for col in EXCEL_FILTER_COLS:
        f = filters.get(col) or {}
        cell = row.get(col, "")
        sel_vals = f.get("selected_values")
        if sel_vals is not None:
            if not _selected_values_match(col, cell, sel_vals):
                return False
        if f.get("text_query"):
            if not _text_matches(cell, f.get("text_op"), f.get("text_query")):
                return False
        if col in ("DataRegisto", "Prazo", "InicioPrevisto") and f.get("date_op"):
            if not _date_matches(cell, f.get("date_op"), f.get("date_a"), f.get("date_b")):
                return False
    return True


def column_unique_values(rows: List[Dict[str, Any]], col: str) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for r in rows:
        v = str(r.get(col) or "")
        if v not in seen:
            seen.add(v)
            out.append(v)
    return sorted(out, key=lambda x: (x == "", x.lower()))
