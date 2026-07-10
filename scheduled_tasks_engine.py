# -*- coding: utf-8 -*-
"""Motor puro de recorrência para tarefas programadas (sem BD/UI)."""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from typing import Any, List, Optional

REC_ONCE = "once"
REC_WEEKLY = "weekly"
REC_MONTHLY = "monthly"
REC_YEARLY = "yearly"


def _last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def normalize_dom(year: int, month: int, dom: Optional[int]) -> int:
    """Dia do mês seguro: dom>último -> último; dom==31 em fev -> último dia."""
    last = _last_day_of_month(year, month)
    if dom is None:
        return last
    d = int(dom)
    if d < 1:
        return 1
    if d >= 31:
        return last
    return min(d, last)


def iso_week_key(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def compute_cycle_key(recurrence: str, occurrence_date: date) -> str:
    r = (recurrence or "").strip().lower()
    if r == REC_YEARLY:
        return f"Y:{occurrence_date.year:04d}"
    if r == REC_MONTHLY:
        return f"M:{occurrence_date.year:04d}-{occurrence_date.month:02d}"
    if r == REC_WEEKLY:
        return f"W:{iso_week_key(occurrence_date)}"
    if r == REC_ONCE:
        return f"O:{occurrence_date.isoformat()}"
    return f"X:{occurrence_date.isoformat()}"


def _add_months(y: int, m: int, delta: int) -> tuple[int, int]:
    m = int(m) + int(delta)
    while m > 12:
        m -= 12
        y += 1
    while m < 1:
        m += 12
        y -= 1
    return y, m


def _week_start_monday(d: date) -> date:
    return d - timedelta(days=int(d.weekday()))


def _next_weekly_occurrence(from_occurrence: date, weekday_mask: int, interval_n: int) -> date:
    """Próxima data > from_occurrence onde o bit weekday coincide; salta semanas por interval_n."""
    interval_n = max(1, int(interval_n))
    mask = int(weekday_mask or 0)
    if mask == 0:
        mask = 1 << from_occurrence.weekday()
    anchor_monday = _week_start_monday(from_occurrence)
    d = from_occurrence + timedelta(days=1)
    for _ in range(800):
        wd = d.weekday()
        if mask & (1 << wd):
            wm = _week_start_monday(d)
            weeks_diff = (wm - anchor_monday).days // 7
            if weeks_diff == 0 and d <= from_occurrence:
                d += timedelta(days=1)
                continue
            if weeks_diff % interval_n != 0:
                d += timedelta(days=1)
                continue
            return d
        d += timedelta(days=1)
    return from_occurrence + timedelta(days=7 * interval_n)


def compute_next_run_date(template: dict, from_occurrence: date) -> date:
    """Calcula a próxima ocorrência estritamente após from_occurrence."""
    rec = (template.get("recurrence") or "").strip().lower()
    interval_n = max(1, int(template.get("interval_n") or 1))

    if rec == REC_ONCE:
        return date(2099, 12, 31)

    if rec == REC_WEEKLY:
        wmask = template.get("weekday_mask")
        if wmask is None:
            wmask = 1 << from_occurrence.weekday()
        return _next_weekly_occurrence(from_occurrence, int(wmask), interval_n)

    if rec == REC_MONTHLY:
        dom = template.get("day_of_month")
        if dom is None:
            dom = from_occurrence.day
        y, m = _add_months(from_occurrence.year, from_occurrence.month, interval_n)
        d = normalize_dom(y, m, dom)
        cand = date(y, m, d)
        if cand <= from_occurrence:
            y, m = _add_months(y, m, 1)
            d = normalize_dom(y, m, dom)
            cand = date(y, m, d)
        return cand

    if rec == REC_YEARLY:
        mo = int(template.get("month_of_year") or from_occurrence.month)
        dom = template.get("day_of_month")
        if dom is None:
            dom = from_occurrence.day
        y = from_occurrence.year + interval_n
        d = normalize_dom(y, mo, dom)
        return date(y, mo, d)

    return from_occurrence + timedelta(days=1)


def parse_date(s: Any) -> Optional[date]:
    if s is None:
        return None
    if isinstance(s, date):
        return s
    t = str(s).strip()
    if not t:
        return None
    try:
        return date.fromisoformat(t[:10])
    except ValueError:
        return None


def window_state(as_of: date, occurrence_date: date, lead_days: int, grace_days: int) -> str:
    """'early' | 'in' | 'late' — janela [occurrence-lead, occurrence+grace]."""
    lead_days = max(0, int(lead_days))
    grace_days = max(0, int(grace_days))
    start = occurrence_date - timedelta(days=lead_days)
    end = occurrence_date + timedelta(days=grace_days)
    if as_of < start:
        return "early"
    if as_of > end:
        return "late"
    return "in"


def is_in_window(as_of: date, occurrence_date: date, lead_days: int, grace_days: int) -> bool:
    return window_state(as_of, occurrence_date, lead_days, grace_days) == "in"


def compute_occurrence_for_template(template: dict, as_of: date) -> date:
    """Usa next_run_date do template; se inválido, cai para as_of."""
    nrd = parse_date(template.get("next_run_date"))
    if nrd:
        return nrd
    return as_of


def preview_next_occurrences(template: dict, start_from: date, n: int = 12) -> List[date]:
    """Lista as próximas n ocorrências a partir de next_run_date (ou start_from)."""
    out: List[date] = []
    rec = (template.get("recurrence") or "").strip().lower()
    cur = parse_date(template.get("next_run_date")) or start_from
    if rec == REC_ONCE:
        od = parse_date(template.get("once_date"))
        return [od] if od else []
    for _ in range(max(1, n)):
        out.append(cur)
        nxt = compute_next_run_date(template, cur)
        if nxt <= cur or nxt.year >= 2099:
            break
        cur = nxt
    return out[:n]


def validate_template(t: dict) -> List[str]:
    errs: List[str] = []
    name = (t.get("name") or "").strip()
    if not name:
        errs.append("Nome é obrigatório.")
    rec = (t.get("recurrence") or "").strip().lower()
    if rec not in (REC_ONCE, REC_WEEKLY, REC_MONTHLY, REC_YEARLY):
        errs.append("Recorrência inválida (once/weekly/monthly/yearly).")
    vis = (t.get("visibility") or "PERSONAL").strip().upper()
    if vis not in ("PERSONAL", "SHARED"):
        errs.append("Visibilidade inválida.")
    mode = (t.get("create_mode") or "MANUAL").strip().upper()
    if mode not in ("AUTO", "MANUAL"):
        errs.append("Modo de criação inválido (AUTO/MANUAL).")
    if rec != REC_ONCE and int(t.get("interval_n") or 1) < 1:
        errs.append("O intervalo de repetição tem de ser >= 1.")
    if rec == REC_MONTHLY and t.get("day_of_month") is None:
        errs.append("Mensal: indique day_of_month.")
    if rec == REC_YEARLY:
        if t.get("month_of_year") is None:
            errs.append("Anual: indique month_of_year.")
        if t.get("day_of_month") is None:
            errs.append("Anual: indique day_of_month.")
    if rec == REC_WEEKLY:
        if not int(t.get("weekday_mask") or 0):
            errs.append("Semanal: escolha pelo menos um dia da semana.")
    if rec == REC_ONCE:
        if not parse_date(t.get("once_date")):
            errs.append("Uma vez: indique once_date.")
    if parse_date(t.get("next_run_date")) is None:
        errs.append("next_run_date é obrigatório (YYYY-MM-DD).")
    return errs


def _fmt_pt_date(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _fmt_pt_datetime(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M")
    t = str(value).strip()
    if not t:
        return ""
    try:
        if "T" in t:
            return datetime.fromisoformat(t.replace("Z", "+00:00")[:19]).strftime("%d/%m/%Y %H:%M")
        return datetime.strptime(t[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return t[:16]


def _last_generation_hint(template: dict) -> str:
    tid = (template.get("last_generated_taskid") or "").strip()
    if not tid:
        return ""
    when = _fmt_pt_datetime(template.get("last_generated_at"))
    return f" · última tarefa {tid}" + (f" ({when})" if when else "")


def describe_template_status(
    template: dict,
    as_of: date,
    instance: Optional[dict] = None,
) -> str:
    """Texto legível (PT) do estado atual de um template programado."""
    if not int(template.get("is_active") or 0):
        return "Inativa"

    occ = parse_date(template.get("next_run_date"))
    if not occ:
        return "Inválida — falta próxima ocorrência"

    rec = (template.get("recurrence") or "").strip().lower()
    lead = max(0, int(template.get("lead_days") or 0))
    grace = max(0, int(template.get("grace_days") if template.get("grace_days") is not None else 30))
    mode = (template.get("create_mode") or "MANUAL").strip().upper()
    ws = window_state(as_of, occ, lead, grace)
    win_start = occ - timedelta(days=lead)
    win_end = occ + timedelta(days=grace)
    last_hint = _last_generation_hint(template)

    if rec == REC_ONCE and occ.year >= 2099:
        tid = (template.get("last_generated_taskid") or "").strip()
        if tid:
            when = _fmt_pt_datetime(template.get("last_generated_at"))
            return f"Concluída — tarefa {tid}" + (f" ({when})" if when else "")
        return "Concluída (uma vez)"

    inst_st = str((instance or {}).get("status") or "").upper()
    inst_task = (instance or {}).get("task_id") or ""
    pend_ck = (template.get("pending_cycle_key") or "").strip()

    if inst_st == "CREATED" and inst_task:
        return f"Ciclo gerado — tarefa {inst_task}"
    if inst_st == "FAILED":
        err = str((instance or {}).get("error_message") or "").strip()
        msg = "Falhou ao gerar"
        if err:
            msg += f": {err[:50]}"
        return msg
    if inst_st == "SKIPPED":
        return f"Ciclo saltado (atraso) — aguarda processamento{last_hint}"

    if ws == "early":
        if lead > 0:
            return f"Aguarda — aviso a partir de {_fmt_pt_date(win_start)}, execução {_fmt_pt_date(occ)}{last_hint}"
        return f"Aguarda — execução {_fmt_pt_date(occ)}{last_hint}"

    if ws == "late":
        return (
            f"Atrasada — tolerância terminou {_fmt_pt_date(win_end)}; "
            f"será saltada ao processar{last_hint}"
        )

    # Dentro da janela [occ-lead, occ+grace]
    if inst_st == "PENDING" or pend_ck:
        return f"Pendente — usar 'Gerar agora' (tolerância até {_fmt_pt_date(win_end)})"

    if as_of < occ and lead > 0:
        aviso = f"Aviso ativo desde {_fmt_pt_date(win_start)}"
    elif as_of < occ:
        aviso = f"Antes do dia {_fmt_pt_date(occ)}"
    else:
        aviso = f"Dia {_fmt_pt_date(occ)} passou — tolerância até {_fmt_pt_date(win_end)}"

    if mode == "AUTO":
        if as_of >= occ:
            return f"Na janela — AUTO deveria criar já ({aviso}){last_hint}"
        return f"Na janela — AUTO criará até {_fmt_pt_date(occ)} ({aviso}){last_hint}"

    return f"Na janela — confirmar 'Gerar agora' ({aviso}){last_hint}"
