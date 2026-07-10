# -*- coding: utf-8 -*-
"""Dashboard — dados agregados para gráficos Plotly (3 modos)."""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

OPEN_STATES = {"Não iniciado", "A Fazer", "Em Progresso", "Bloqueado"}


def _apply_filters(rows: List[Dict[str, Any]], f: Dict[str, Any]) -> List[Dict[str, Any]]:
    estado_f = str(f.get("estado") or "Todos")
    prio_f = str(f.get("prioridade") or "Todos")
    resp_f = str(f.get("responsavel") or "Todos")
    proj_f = str(f.get("projeto") or "Todos")
    only_open = bool(f.get("only_open"))
    out: List[Dict[str, Any]] = []
    for r in rows:
        est = str(r.get("Estado") or "")
        if estado_f != "Todos" and est != estado_f:
            continue
        if prio_f != "Todos" and str(r.get("Prioridade") or "") != prio_f:
            continue
        if resp_f != "Todos" and str(r.get("Responsavel") or "") != resp_f:
            continue
        if proj_f != "Todos" and str(r.get("Projeto") or "") != proj_f:
            continue
        if only_open and est == "Concluído":
            continue
        out.append(r)
    return out


def _week_key(d: dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _parse_date(s: Any) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(str(s or "")[:10])
    except Exception:
        return None


class DashboardService:
    def charts(
        self,
        rows: List[Dict[str, Any]],
        mode: str = "executivo",
        filters: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        data = _apply_filters(rows, filters or {})
        mode = str(mode or "executivo").strip().lower()
        if mode in ("operacao", "operação", "ops"):
            return self._mode_operacao(data)
        if mode in ("analitico", "analítico", "analytics"):
            return self._mode_analitico(data)
        return self._mode_executivo(data)

    def _mode_executivo(self, data: List[Dict[str, Any]]) -> Dict[str, Any]:
        by_estado = Counter(str(r.get("Estado") or "—") for r in data)
        today = dt.date.today()
        weeks: Dict[str, int] = defaultdict(int)
        for r in data:
            d = _parse_date(r.get("DataRegisto"))
            if d and (today - d).days <= 56:
                weeks[_week_key(d)] += 1
        week_labels = sorted(weeks.keys())[-8:]
        critical = sorted(
            [r for r in data if r.get("is_overdue") and str(r.get("Estado") or "") != "Concluído"],
            key=lambda x: str(x.get("Prazo") or ""),
        )[:10]
        crit_labels = [str(r.get("Tarefa") or r.get("TaskID") or "")[:42] for r in critical]
        crit_vals = [1] * len(critical)
        overdue_n = sum(1 for r in data if r.get("is_overdue") and str(r.get("Estado") or "") != "Concluído")
        open_n = sum(1 for r in data if str(r.get("Estado") or "") in OPEN_STATES)
        return {
            "mode": "executivo",
            "kpis": {"total": len(data), "open": open_n, "overdue": overdue_n},
            "charts": [
                {
                    "id": "estado_pie",
                    "title": "Tarefas por estado",
                    "type": "pie",
                    "labels": list(by_estado.keys()),
                    "values": list(by_estado.values()),
                },
                {
                    "id": "trend_week",
                    "title": "Novas tarefas por semana (8 sem.)",
                    "type": "bar",
                    "x": week_labels,
                    "y": [weeks.get(w, 0) for w in week_labels],
                },
                {
                    "id": "critical",
                    "title": "Top atrasadas",
                    "type": "bar_h",
                    "x": crit_vals,
                    "y": crit_labels,
                    "orientation": "h",
                },
            ],
        }

    def _mode_operacao(self, data: List[Dict[str, Any]]) -> Dict[str, Any]:
        estados = sorted({str(r.get("Estado") or "—") for r in data})
        prios = ["Alta", "Média", "Baixa"]
        matrix: Dict[str, Dict[str, int]] = {e: {p: 0 for p in prios} for e in estados}
        for r in data:
            e = str(r.get("Estado") or "—")
            p = str(r.get("Prioridade") or "Média")
            if p not in prios:
                p = "Média"
            matrix.setdefault(e, {x: 0 for x in prios})
            matrix[e][p] = matrix[e].get(p, 0) + 1
        z = [[matrix[e].get(p, 0) for p in prios] for e in estados]
        by_resp = Counter(
            str(r.get("Responsavel") or "—")
            for r in data
            if str(r.get("Estado") or "") in OPEN_STATES
        )
        top_resp = by_resp.most_common(8)
        by_proj = Counter(
            str(r.get("Projeto") or "(sem projeto)")
            for r in data
            if str(r.get("Estado") or "") in OPEN_STATES
        )
        top_proj = by_proj.most_common(8)
        return {
            "mode": "operacao",
            "kpis": {"open": sum(by_resp.values()), "projects": len(by_proj)},
            "charts": [
                {
                    "id": "heatmap",
                    "title": "Estado × Prioridade",
                    "type": "heatmap",
                    "x": prios,
                    "y": estados,
                    "z": z,
                },
                {
                    "id": "top_resp",
                    "title": "Top responsáveis (abertas)",
                    "type": "bar",
                    "x": [x[0] for x in top_resp],
                    "y": [x[1] for x in top_resp],
                },
                {
                    "id": "backlog_proj",
                    "title": "Backlog por projeto",
                    "type": "bar",
                    "x": [x[0] for x in top_proj],
                    "y": [x[1] for x in top_proj],
                },
            ],
        }

    def _mode_analitico(self, data: List[Dict[str, Any]]) -> Dict[str, Any]:
        durations: List[int] = []
        for r in data:
            d0 = _parse_date(r.get("DataRegisto"))
            d1 = _parse_date(r.get("Prazo"))
            if d0 and d1 and d1 >= d0:
                durations.append((d1 - d0).days)
        buckets = {"0-7d": 0, "8-14d": 0, "15-30d": 0, "31-60d": 0, "60+d": 0}
        for n in durations:
            if n <= 7:
                buckets["0-7d"] += 1
            elif n <= 14:
                buckets["8-14d"] += 1
            elif n <= 30:
                buckets["15-30d"] += 1
            elif n <= 60:
                buckets["31-60d"] += 1
            else:
                buckets["60+d"] += 1
        by_prio = Counter(str(r.get("Prioridade") or "—") for r in data)
        by_milestone = Counter(str(r.get("Milestone") or "(sem milestone)") for r in data)
        top_ms = by_milestone.most_common(10)
        avg_dur = round(sum(durations) / len(durations), 1) if durations else 0
        return {
            "mode": "analitico",
            "kpis": {"avg_duration_days": avg_dur, "with_dates": len(durations)},
            "charts": [
                {
                    "id": "duration",
                    "title": "Duração planeada (registo → prazo)",
                    "type": "bar",
                    "x": list(buckets.keys()),
                    "y": list(buckets.values()),
                },
                {
                    "id": "prio_pie",
                    "title": "Distribuição por prioridade",
                    "type": "pie",
                    "labels": list(by_prio.keys()),
                    "values": list(by_prio.values()),
                },
                {
                    "id": "milestone",
                    "title": "Top milestones",
                    "type": "bar",
                    "x": [x[0] for x in top_ms],
                    "y": [x[1] for x in top_ms],
                },
            ],
        }
