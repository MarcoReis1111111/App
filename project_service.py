# -*- coding: utf-8 -*-
"""Vista Projeto — KPIs e Gantt multi-tarefa."""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List

DONE_STATES = {"Concluído"}


class ProjectService:
    def __init__(self, gantt_service):
        self.gantt = gantt_service

    def summary(
        self,
        rows: List[Dict[str, Any]],
        projeto: str = "Todos",
        level: str = "task",
    ) -> Dict[str, Any]:
        data = list(rows or [])
        if projeto and projeto != "Todos":
            data = [r for r in data if str(r.get("Projeto") or "") == projeto]
        total = len(data)
        done = sum(1 for r in data if str(r.get("Estado") or "") in DONE_STATES)
        overdue = sum(1 for r in data if r.get("is_overdue"))
        blocked = sum(1 for r in data if int(r.get("blocked_count") or 0) > 0)
        by_status = dict(Counter(str(r.get("Estado") or "—") for r in data))
        projects = sorted({str(r.get("Projeto") or "").strip() for r in rows if str(r.get("Projeto") or "").strip()})
        gantt = self.gantt.build_portfolio_gantt(data, level=level)
        return {
            "projeto": projeto,
            "level": level,
            "projects": ["Todos"] + projects,
            "kpis": {
                "total": total,
                "done": done,
                "done_pct": round(100 * done / total, 1) if total else 0,
                "overdue": overdue,
                "blocked": blocked,
            },
            "by_status": by_status,
            "gantt": gantt,
        }
