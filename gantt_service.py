# -*- coding: utf-8 -*-
"""Serviço Gantt — dados para gráfico Plotly no browser."""
from __future__ import annotations

from typing import Any, Dict, List


class GanttService:
    def build_gantt_data(self, task: Dict[str, Any], actions: List[Dict[str, Any]]) -> Dict[str, Any]:
        bars: List[Dict[str, Any]] = []
        tid = str(task.get("TaskID") or "")
        if task:
            bars.append({
                "id": tid,
                "label": str(task.get("Tarefa") or tid),
                "start": str(task.get("InicioPrevisto") or task.get("DataRegisto") or "")[:10],
                "end": str(task.get("Prazo") or "")[:10],
                "type": "task",
                "status": str(task.get("Estado") or ""),
            })
        for a in actions:
            if str(a.get("kind") or "ACTION") != "ACTION":
                continue
            bars.append({
                "id": a.get("id"),
                "label": str(a.get("item_text") or ""),
                "start": str(a.get("start_date") or "")[:10],
                "end": str(a.get("due_date") or "")[:10],
                "type": "action",
                "status": str(a.get("status") or ""),
                "owner": str(a.get("owner_display") or a.get("owner") or ""),
            })
        bars = [b for b in bars if b.get("start") and b.get("end")]
        return {
            "bars": bars,
            "task_id": tid,
            "implemented": bool(bars),
            "message": "Gantt renderizado no browser (Plotly)",
        }

    def build_portfolio_gantt(self, tasks: List[Dict[str, Any]], level: str = "task") -> Dict[str, Any]:
        bars: List[Dict[str, Any]] = []
        level = str(level or "task").strip().lower()
        if level == "milestone":
            buckets: Dict[str, Dict[str, Any]] = {}
            for t in tasks:
                ms = str(t.get("Milestone") or "(sem milestone)").strip() or "(sem milestone)"
                start = str(t.get("InicioPrevisto") or t.get("DataRegisto") or "")[:10]
                end = str(t.get("Prazo") or "")[:10]
                if not (start and end):
                    continue
                b = buckets.get(ms)
                if not b:
                    buckets[ms] = {"start": start, "end": end, "count": 1}
                else:
                    b["start"] = min(b["start"], start)
                    b["end"] = max(b["end"], end)
                    b["count"] += 1
            for ms, b in sorted(buckets.items()):
                bars.append({
                    "id": ms,
                    "label": f"{ms} ({b['count']})",
                    "start": b["start"],
                    "end": b["end"],
                    "type": "milestone",
                    "status": "",
                })
        else:
            for t in tasks:
                start = str(t.get("InicioPrevisto") or t.get("DataRegisto") or "")[:10]
                end = str(t.get("Prazo") or "")[:10]
                if not (start and end):
                    continue
                bars.append({
                    "id": t.get("TaskID"),
                    "label": str(t.get("Tarefa") or t.get("TaskID") or ""),
                    "start": start,
                    "end": end,
                    "type": "task",
                    "status": str(t.get("Estado") or ""),
                })
        return {
            "bars": bars,
            "level": level,
            "implemented": bool(bars),
            "message": "Gantt portfolio (Plotly)",
        }

    def export_plotly_html(self, task: Dict[str, Any], actions: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.build_gantt_data(task, actions)
