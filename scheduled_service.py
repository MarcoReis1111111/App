# -*- coding: utf-8 -*-
"""Tarefas programadas — CRUD, geração e materialização."""
from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

import scheduled_tasks_engine as ste
from tasks_common import AppError, can_edit_role, jval
from tasks_service import TASK_WRITE_COLS, clean_task_payload, validate_task_save

REC_LABELS = {"weekly": "Semanal", "monthly": "Mensal", "yearly": "Anual", "once": "Uma vez"}
MODE_LABELS = {"AUTO": "Automática", "MANUAL": "Manual"}
VIS_LABELS = {"PERSONAL": "Pessoal", "SHARED": "Partilhada"}

_ENSURE_SQL = """
IF OBJECT_ID(N'dbo.scheduled_task_templates', N'U') IS NULL
CREATE TABLE dbo.scheduled_task_templates (
    id BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    name NVARCHAR(200) NOT NULL,
    is_active BIT NOT NULL CONSTRAINT DF_sched_t_is_active DEFAULT 1,
    visibility NVARCHAR(10) NOT NULL CONSTRAINT DF_sched_t_vis DEFAULT N'PERSONAL',
    owner_username NVARCHAR(128) NOT NULL,
    recurrence NVARCHAR(10) NOT NULL,
    interval_n INT NOT NULL CONSTRAINT DF_sched_t_int DEFAULT 1,
    month_of_year TINYINT NULL,
    day_of_month TINYINT NULL,
    weekday_mask INT NULL,
    once_date DATE NULL,
    next_run_date DATE NOT NULL,
    lead_days INT NOT NULL CONSTRAINT DF_sched_t_lead DEFAULT 0,
    grace_days INT NOT NULL CONSTRAINT DF_sched_t_grace DEFAULT 30,
    create_mode NVARCHAR(10) NOT NULL CONSTRAINT DF_sched_t_mode DEFAULT N'MANUAL',
    generate_default_actions BIT NOT NULL CONSTRAINT DF_sched_t_gda DEFAULT 0,
    task_defaults_json NVARCHAR(MAX) NULL,
    action_defaults_json NVARCHAR(MAX) NULL,
    last_cycle_key NVARCHAR(32) NULL,
    last_generated_at DATETIME2 NULL,
    last_generated_taskid NVARCHAR(128) NULL,
    pending_cycle_key NVARCHAR(32) NULL,
    pending_from DATE NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_sched_t_cat DEFAULT SYSUTCDATETIME(),
    updated_at DATETIME2 NULL
);
IF OBJECT_ID(N'dbo.scheduled_task_instances', N'U') IS NULL
CREATE TABLE dbo.scheduled_task_instances (
    id BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    template_id BIGINT NOT NULL,
    cycle_key NVARCHAR(32) NOT NULL,
    occurrence_date DATE NOT NULL,
    status NVARCHAR(16) NOT NULL,
    task_id NVARCHAR(128) NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_sched_i_cat DEFAULT SYSUTCDATETIME(),
    error_message NVARCHAR(500) NULL,
    CONSTRAINT UQ_sched_task_inst UNIQUE (template_id, cycle_key),
    CONSTRAINT FK_sched_inst_tmpl FOREIGN KEY (template_id)
        REFERENCES dbo.scheduled_task_templates(id)
);
"""


def _template_from_row(r) -> Dict[str, Any]:
    keys = (
        "id", "name", "is_active", "visibility", "owner_username", "recurrence", "interval_n",
        "month_of_year", "day_of_month", "weekday_mask", "once_date", "next_run_date",
        "lead_days", "grace_days", "create_mode", "generate_default_actions",
        "task_defaults_json", "action_defaults_json", "last_cycle_key", "last_generated_at",
        "last_generated_taskid", "pending_cycle_key", "pending_from", "created_at", "updated_at",
    )
    return {k: jval(r[i]) if i < len(r) else None for i, k in enumerate(keys)}


def _human_recurrence(row: Dict[str, Any]) -> str:
    rec = str(row.get("recurrence") or "").strip().lower()
    base = REC_LABELS.get(rec, rec or "—")
    try:
        n = int(row.get("interval_n") or 1)
        if n > 1 and rec in ("weekly", "monthly", "yearly"):
            return f"{base} (cada {n})"
    except Exception:
        pass
    return base


def _can_edit(row: Dict[str, Any], username: str, role: str) -> bool:
    if str(row.get("owner_username") or "").strip().lower() == str(username or "").strip().lower():
        return True
    return can_edit_role(role)


def _is_unique_violation(exc: Exception) -> bool:
    msg = str(exc).upper()
    return "UNIQUE" in msg or "2627" in msg or "23000" in msg


def _normalize_payload(d: Dict[str, Any], username: str) -> Dict[str, Any]:
    task_defaults = d.get("task_defaults")
    if isinstance(task_defaults, dict):
        task_json = json.dumps(task_defaults, ensure_ascii=False)
    else:
        task_json = str(d.get("task_defaults_json") or "").strip() or None
    action_defaults = d.get("action_defaults")
    if isinstance(action_defaults, list):
        action_json = json.dumps(action_defaults, ensure_ascii=False)
    elif isinstance(d.get("action_defaults_json"), list):
        action_json = json.dumps(d["action_defaults_json"], ensure_ascii=False)
    else:
        aj = d.get("action_defaults_json")
        action_json = json.dumps(aj, ensure_ascii=False) if isinstance(aj, (list, dict)) else (str(aj or "").strip() or None)
    dom = d.get("day_of_month")
    moy = d.get("month_of_year")
    wmask = d.get("weekday_mask")
    once = d.get("once_date")
    return {
        "name": str(d.get("name") or "").strip(),
        "is_active": bool(d.get("is_active", True)),
        "visibility": str(d.get("visibility") or "PERSONAL").strip().upper(),
        "owner_username": str(d.get("owner_username") or username).strip(),
        "recurrence": str(d.get("recurrence") or "monthly").strip().lower(),
        "interval_n": int(d.get("interval_n") or 1),
        "month_of_year": int(moy) if moy not in (None, "") else None,
        "day_of_month": int(dom) if dom not in (None, "") else None,
        "weekday_mask": int(wmask) if wmask not in (None, "") else None,
        "once_date": str(once)[:10] if once else None,
        "next_run_date": str(d.get("next_run_date") or dt.date.today().isoformat())[:10],
        "lead_days": int(d.get("lead_days") or 0),
        "grace_days": int(d.get("grace_days") if d.get("grace_days") is not None else 30),
        "create_mode": str(d.get("create_mode") or "MANUAL").strip().upper(),
        "generate_default_actions": bool(d.get("generate_default_actions")),
        "task_defaults_json": task_json,
        "action_defaults_json": action_json,
    }


def _payload_for_engine(p: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": p["name"],
        "recurrence": p["recurrence"],
        "interval_n": p["interval_n"],
        "month_of_year": p["month_of_year"],
        "day_of_month": p["day_of_month"],
        "weekday_mask": p["weekday_mask"],
        "once_date": p["once_date"],
        "next_run_date": p["next_run_date"],
        "lead_days": p["lead_days"],
        "grace_days": p["grace_days"],
        "create_mode": p["create_mode"],
        "visibility": p["visibility"],
        "is_active": p["is_active"],
    }


def _validate_action_defaults_json(action_json: Optional[str]) -> List[str]:
    if not action_json or not str(action_json).strip():
        return []
    try:
        data = json.loads(action_json)
    except json.JSONDecodeError as e:
        return [f"Ações default: JSON inválido — {e.msg} (posição {e.pos})."]
    if not isinstance(data, list):
        return ['Ações default: tem de ser um array, ex. [{"text":"Revisão","due_offset_days":0}]']
    errs: List[str] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            errs.append(f"Ação #{i + 1}: cada entrada tem de ser um objeto {{...}}.")
            continue
        if not str(item.get("text") or item.get("item_text") or "").strip():
            errs.append(f'Ação #{i + 1}: falta o campo "text".')
    return errs


def _validate_payload(p: Dict[str, Any], require_name: bool = True) -> List[str]:
    tpl = _payload_for_engine(p)
    if not require_name and not tpl["name"]:
        tpl = {**tpl, "name": "(preview)"}
    errs = list(ste.validate_template(tpl))
    if p.get("generate_default_actions") or p.get("action_defaults_json"):
        errs.extend(_validate_action_defaults_json(p.get("action_defaults_json")))
    return errs


def _raise_if_invalid(p: Dict[str, Any], require_name: bool = True) -> None:
    errs = _validate_payload(p, require_name)
    if errs:
        raise AppError(" | ".join(errs))


class ScheduledService:
    def __init__(self, da):
        self.da = da

    def ensure_tables(self, conn) -> None:
        cur = conn.cursor()
        for stmt in _ENSURE_SQL.split(";"):
            s = stmt.strip()
            if s:
                cur.execute(s)

    def get_template(self, template_id: int, username: str, role: str) -> Optional[Dict[str, Any]]:
        with self.da.lock, self.da.connect() as conn:
            self.ensure_tables(conn)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, name, is_active, visibility, owner_username, recurrence, interval_n,
                       month_of_year, day_of_month, weekday_mask, once_date, next_run_date,
                       lead_days, grace_days, create_mode, generate_default_actions,
                       task_defaults_json, action_defaults_json, last_cycle_key, last_generated_at,
                       last_generated_taskid, pending_cycle_key, pending_from, created_at, updated_at
                FROM dbo.scheduled_task_templates WHERE id=?;
                """,
                (int(template_id),),
            )
            r = cur.fetchone()
            if not r:
                return None
            row = _template_from_row(r)
            if not _can_edit(row, username, role) and str(row.get("visibility") or "").upper() != "SHARED":
                if str(row.get("owner_username") or "").lower() != str(username or "").lower():
                    return None
            return self._enrich_row(row, username, role)

    def list_templates(self, username: str, role: str, include_shared: bool = True) -> Dict[str, Any]:
        u = str(username or "").strip()
        with self.da.lock, self.da.connect() as conn:
            self.ensure_tables(conn)
            if include_shared:
                where = "(owner_username = ? OR visibility = N'SHARED')"
            else:
                where = "(owner_username = ? AND visibility = N'PERSONAL')"
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT id, name, is_active, visibility, owner_username, recurrence, interval_n,
                       month_of_year, day_of_month, weekday_mask, once_date, next_run_date,
                       lead_days, grace_days, create_mode, generate_default_actions,
                       task_defaults_json, action_defaults_json, last_cycle_key, last_generated_at,
                       last_generated_taskid, pending_cycle_key, pending_from, created_at, updated_at
                FROM dbo.scheduled_task_templates
                WHERE {where}
                ORDER BY is_active DESC, next_run_date, id;
                """,
                (u,),
            )
            raw = [_template_from_row(r) for r in cur.fetchall() or []]
        rows = [self._enrich_row(t, username, role) for t in raw]
        pending_n = sum(1 for r in rows if r.get("pending_human") == "Sim")
        active_n = sum(1 for r in rows if int(r.get("is_active") or 0))
        next7 = sum(
            1 for r in rows
            if ste.parse_date(r.get("next_run_date"))
            and 0 <= (ste.parse_date(r.get("next_run_date")) - dt.date.today()).days <= 7
        )
        shared_n = sum(1 for r in rows if str(r.get("visibility") or "").upper() == "SHARED")
        failed_n = sum(1 for r in rows if "Falhou" in str(r.get("state_human") or ""))
        return {
            "rows": rows,
            "summary": {
                "pending": pending_n,
                "active": active_n,
                "next7": next7,
                "shared": shared_n,
                "failed": failed_n,
            },
        }

    def _enrich_row(self, t: Dict[str, Any], username: str, role: str) -> Dict[str, Any]:
        today = dt.date.today()
        occ = ste.parse_date(t.get("next_run_date"))
        ck = ste.compute_cycle_key(str(t.get("recurrence") or ""), occ) if occ else ""
        inst = self._get_instance(int(t["id"]), ck) if ck else None
        row = dict(t)
        row["recurrence_human"] = _human_recurrence(t)
        row["mode_human"] = MODE_LABELS.get(str(t.get("create_mode") or "").upper(), t.get("create_mode"))
        row["visibility_human"] = VIS_LABELS.get(str(t.get("visibility") or "").upper(), t.get("visibility"))
        row["state_human"] = ste.describe_template_status(t, today, inst)
        row["pending_human"] = "Sim" if t.get("pending_cycle_key") else "—"
        row["can_edit"] = _can_edit(t, username, role)
        row["last_task_id"] = t.get("last_generated_taskid") or (inst or {}).get("task_id")
        try:
            row["task_defaults"] = json.loads(t.get("task_defaults_json") or "{}")
        except Exception:
            row["task_defaults"] = {}
        try:
            row["action_defaults"] = json.loads(t.get("action_defaults_json") or "[]")
        except Exception:
            row["action_defaults"] = []
        return row

    def _get_instance(self, template_id: int, cycle_key: str) -> Optional[Dict[str, Any]]:
        ck = str(cycle_key or "").strip()[:32]
        if not ck:
            return None
        with self.da.lock, self.da.connect() as conn:
            return self._get_instance_conn(conn, template_id, ck)

    def _get_instance_conn(self, conn, template_id: int, cycle_key: str) -> Optional[Dict[str, Any]]:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, template_id, cycle_key, occurrence_date, status, task_id, created_at, error_message
            FROM dbo.scheduled_task_instances WHERE template_id = ? AND cycle_key = ?;
            """,
            (int(template_id), str(cycle_key or "").strip()[:32]),
        )
        r = cur.fetchone()
        if not r:
            return None
        return {
            "id": r[0], "template_id": r[1], "cycle_key": r[2],
            "occurrence_date": jval(r[3]), "status": r[4], "task_id": r[5],
            "created_at": jval(r[6]), "error_message": r[7],
        }

    def insert_template(self, payload: Dict[str, Any], username: str, role: str) -> int:
        if not can_edit_role(role):
            raise PermissionError("Sem permissão")
        p = _normalize_payload(payload, username)
        if not p["name"]:
            raise AppError("Nome obrigatório")
        _raise_if_invalid(p)
        with self.da.lock, self.da.connect() as conn:
            self.ensure_tables(conn)
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO dbo.scheduled_task_templates (
                    name, is_active, visibility, owner_username, recurrence, interval_n,
                    month_of_year, day_of_month, weekday_mask, once_date, next_run_date,
                    lead_days, grace_days, create_mode, generate_default_actions,
                    task_defaults_json, action_defaults_json
                ) OUTPUT INSERTED.id VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                """,
                (
                    p["name"], 1 if p["is_active"] else 0, p["visibility"], p["owner_username"],
                    p["recurrence"], p["interval_n"], p["month_of_year"], p["day_of_month"],
                    p["weekday_mask"], p["once_date"], p["next_run_date"], p["lead_days"],
                    p["grace_days"], p["create_mode"], 1 if p["generate_default_actions"] else 0,
                    p["task_defaults_json"], p["action_defaults_json"],
                ),
            )
            row = cur.fetchone()
            conn.commit()
            return int(row[0])

    def update_template(self, template_id: int, payload: Dict[str, Any], username: str, role: str) -> None:
        row = self.get_template(template_id, username, role)
        if not row:
            raise AppError("Template não encontrado")
        if not row.get("can_edit"):
            raise PermissionError("Sem permissão")
        p = _normalize_payload({**row, **(payload or {})}, username)
        _raise_if_invalid(p)
        with self.da.lock, self.da.connect() as conn:
            self.ensure_tables(conn)
            conn.cursor().execute(
                """
                UPDATE dbo.scheduled_task_templates SET
                    name=?, is_active=?, visibility=?, recurrence=?, interval_n=?,
                    month_of_year=?, day_of_month=?, weekday_mask=?, once_date=?, next_run_date=?,
                    lead_days=?, grace_days=?, create_mode=?, generate_default_actions=?,
                    task_defaults_json=?, action_defaults_json=?, updated_at=SYSUTCDATETIME()
                WHERE id=?;
                """,
                (
                    p["name"], 1 if p["is_active"] else 0, p["visibility"], p["recurrence"],
                    p["interval_n"], p["month_of_year"], p["day_of_month"], p["weekday_mask"],
                    p["once_date"], p["next_run_date"], p["lead_days"], p["grace_days"],
                    p["create_mode"], 1 if p["generate_default_actions"] else 0,
                    p["task_defaults_json"], p["action_defaults_json"], int(template_id),
                ),
            )
            conn.commit()

    def preview_occurrences(
        self, payload: Dict[str, Any], username: str, count: int = 12,
    ) -> Dict[str, Any]:
        p = _normalize_payload(payload, username)
        errs = _validate_payload(p, require_name=bool(p["name"]))
        today = dt.date.today()
        n = max(1, min(int(count or 12), 24))
        occurrences: List[Dict[str, Any]] = []
        if not errs:
            tpl = _payload_for_engine(p)
            if not tpl.get("name"):
                tpl["name"] = "(preview)"
            labels = {"early": "Cedo (fora da janela)", "in": "Na janela lead/grace", "late": "Atrasado"}
            for d in ste.preview_next_occurrences(tpl, today, n):
                ws = ste.window_state(today, d, p["lead_days"], p["grace_days"])
                occurrences.append({
                    "date": d.isoformat(),
                    "window": ws,
                    "window_label": labels.get(ws, ws),
                })
        return {"occurrences": occurrences, "errors": errs, "valid": not errs}

    def toggle_active(self, template_id: int, username: str, role: str) -> bool:
        row = self.get_template(template_id, username, role)
        if not row:
            raise AppError("Template não encontrado")
        if not row.get("can_edit"):
            raise PermissionError("Sem permissão")
        new_val = 0 if int(row.get("is_active") or 0) else 1
        with self.da.lock, self.da.connect() as conn:
            self.ensure_tables(conn)
            conn.cursor().execute(
                "UPDATE dbo.scheduled_task_templates SET is_active=?, updated_at=SYSUTCDATETIME() WHERE id=?;",
                (new_val, int(template_id)),
            )
            conn.commit()
            return bool(new_val)

    def generate_now(self, template_id: int, username: str, display: str, role: str) -> str:
        row = self.get_template(template_id, username, role)
        if not row:
            raise AppError("Template não encontrado")
        if not row.get("can_edit"):
            raise PermissionError("Sem permissão")
        occ = ste.parse_date(row.get("next_run_date"))
        if not occ:
            raise AppError("Próxima data inválida")
        ck = ste.compute_cycle_key(str(row.get("recurrence") or ""), occ)[:32]
        ok, msg, existed = self._materialize_cycle(row, occ, ck, username, display)
        if not ok:
            raise AppError(msg)
        return msg

    def materialize_pending(self, template_id: int, username: str, display: str, role: str) -> str:
        row = self.get_template(template_id, username, role)
        if not row:
            raise AppError("Template não encontrado")
        if not row.get("can_edit"):
            raise PermissionError("Sem permissão")
        ck = str(row.get("pending_cycle_key") or "").strip()[:32]
        if not ck:
            raise AppError("Nada pendente para este template")
        inst = self._get_instance(int(template_id), ck)
        if not inst:
            raise AppError("Instância não encontrada")
        if str(inst.get("status") or "").upper() == "CREATED" and inst.get("task_id"):
            return str(inst.get("task_id"))
        occ = ste.parse_date(inst.get("occurrence_date"))
        if not occ:
            raise AppError("Data da ocorrência inválida")
        ok, msg, _ = self._materialize_cycle(row, occ, ck, username, display)
        if not ok:
            raise AppError(msg)
        return msg

    def generate_due(
        self, username: str, display: str, role: str, dry_run: bool = False,
    ) -> Dict[str, Any]:
        if not can_edit_role(role):
            raise PermissionError("Sem permissão")
        today = dt.date.today()
        rep: Dict[str, Any] = {
            "ok": True,
            "dry_run": dry_run,
            "processed": 0,
            "auto_created": 0,
            "manual_pending": 0,
            "skipped_late": 0,
            "errors": [],
            "created_tasks": [],
        }
        data = self.list_templates(username, role, True)
        for tmpl in data.get("rows") or []:
            if not int(tmpl.get("is_active") or 0):
                continue
            try:
                tid = int(tmpl["id"])
                occ = ste.parse_date(tmpl.get("next_run_date"))
                if not occ:
                    continue
                ck = ste.compute_cycle_key(str(tmpl.get("recurrence") or ""), occ)[:32]
                lead = int(tmpl.get("lead_days") or 0)
                grace = int(tmpl.get("grace_days") if tmpl.get("grace_days") is not None else 30)
                ws = ste.window_state(today, occ, lead, grace)
                mode = str(tmpl.get("create_mode") or "MANUAL").strip().upper()
                if ws == "early":
                    continue
                if ws == "late":
                    rep["skipped_late"] += 1
                    if dry_run:
                        continue
                    self._skip_late_cycle(tmpl, occ, ck)
                    continue
                inst = self._get_instance(tid, ck)
                st = str(inst.get("status") or "").upper() if inst else ""
                if st in ("CREATED", "SKIPPED"):
                    continue
                if st == "PENDING" and mode == "MANUAL":
                    continue
                if dry_run:
                    if mode == "MANUAL" and st != "PENDING":
                        rep["manual_pending"] += 1
                    elif mode == "AUTO":
                        rep["auto_created"] += 1
                    continue
                if mode == "MANUAL":
                    self._set_manual_pending(tmpl, occ, ck, today)
                    rep["manual_pending"] += 1
                    rep["processed"] += 1
                    continue
                ok, msg, existed = self._materialize_cycle(tmpl, occ, ck, username, display)
                if ok and not existed:
                    rep["auto_created"] += 1
                    rep["processed"] += 1
                    rep["created_tasks"].append({"template_id": tid, "task_id": msg})
                elif not ok:
                    rep["errors"].append(f"auto {tid}: {msg}")
            except Exception as e:
                rep["errors"].append(str(e)[:200])
        rep["pending_count"] = self.list_templates(username, role, True).get("summary", {}).get("pending", 0)
        return rep

    def _skip_late_cycle(self, template_row: Dict[str, Any], occurrence_date: dt.date, cycle_key: str) -> None:
        tid_tpl = int(template_row["id"])
        ck = str(cycle_key or "").strip()[:32]
        with self.da.lock, self.da.connect() as conn:
            self.ensure_tables(conn)
            cur = conn.cursor()
            inst = self._get_instance_conn(conn, tid_tpl, ck)
            if not inst:
                cur.execute(
                    """
                    INSERT INTO dbo.scheduled_task_instances
                    (template_id, cycle_key, occurrence_date, status)
                    VALUES (?,?,?,N'SKIPPED');
                    """,
                    (tid_tpl, ck, occurrence_date),
                )
            elif str(inst.get("status") or "").upper() == "PENDING":
                cur.execute(
                    "UPDATE dbo.scheduled_task_instances SET status = N'SKIPPED' WHERE id = ?;",
                    (int(inst["id"]),),
                )
            next_d = ste.compute_next_run_date(template_row, occurrence_date)
            cur.execute(
                """
                UPDATE dbo.scheduled_task_templates SET
                    next_run_date = ?, pending_cycle_key = NULL, pending_from = NULL,
                    updated_at = SYSUTCDATETIME()
                WHERE id = ?;
                """,
                (next_d.isoformat(), tid_tpl),
            )
            conn.commit()

    def _set_manual_pending(
        self, template_row: Dict[str, Any], occurrence_date: dt.date, cycle_key: str, as_of: dt.date,
    ) -> None:
        tid_tpl = int(template_row["id"])
        ck = str(cycle_key or "").strip()[:32]
        with self.da.lock, self.da.connect() as conn:
            self.ensure_tables(conn)
            cur = conn.cursor()
            inst = self._get_instance_conn(conn, tid_tpl, ck)
            if not inst:
                cur.execute(
                    """
                    INSERT INTO dbo.scheduled_task_instances
                    (template_id, cycle_key, occurrence_date, status)
                    VALUES (?,?,?,N'PENDING');
                    """,
                    (tid_tpl, ck, occurrence_date),
                )
            cur.execute(
                """
                UPDATE dbo.scheduled_task_templates SET
                    pending_cycle_key = ?, pending_from = ?, updated_at = SYSUTCDATETIME()
                WHERE id = ?;
                """,
                (ck, as_of.isoformat(), tid_tpl),
            )
            conn.commit()

    def _materialize_cycle(
        self,
        template_row: Dict[str, Any],
        occurrence_date: dt.date,
        cycle_key: str,
        username: str,
        display: str,
    ) -> Tuple[bool, str, bool]:
        tid_tpl = int(template_row["id"])
        ck = str(cycle_key or "").strip()[:32]
        if not ck:
            ck = ste.compute_cycle_key(str(template_row.get("recurrence") or ""), occurrence_date)[:32]
        try:
            with self.da.lock, self.da.connect() as conn:
                self.ensure_tables(conn)
                claim, inst = self._try_claim_instance(conn, tid_tpl, ck, occurrence_date)
                if claim == "already_done":
                    task_existing = str((inst or {}).get("task_id") or "").strip()
                    return True, task_existing or "já processado", True
                if claim == "skipped":
                    return True, "Ciclo já saltado", True
                task_id = self._insert_task_from_template(
                    conn, template_row, occurrence_date, username, display,
                )
                conn.cursor().execute(
                    """
                    UPDATE dbo.scheduled_task_instances
                    SET status = N'CREATED', task_id = ?, error_message = NULL
                    WHERE template_id = ? AND cycle_key = ?;
                    """,
                    (task_id, tid_tpl, ck),
                )
                next_d = ste.compute_next_run_date(template_row, occurrence_date)
                conn.cursor().execute(
                    """
                    UPDATE dbo.scheduled_task_templates SET
                        next_run_date = ?,
                        last_cycle_key = ?,
                        last_generated_at = SYSUTCDATETIME(),
                        last_generated_taskid = ?,
                        pending_cycle_key = NULL,
                        pending_from = NULL,
                        updated_at = SYSUTCDATETIME()
                    WHERE id = ?;
                    """,
                    (next_d.isoformat(), ck, task_id, tid_tpl),
                )
                conn.commit()
            return True, task_id, False
        except Exception as e:
            try:
                with self.da.lock, self.da.connect() as conn:
                    conn.cursor().execute(
                        """
                        UPDATE dbo.scheduled_task_instances SET status = N'FAILED', error_message = ?
                        WHERE template_id = ? AND cycle_key = ? AND status <> N'CREATED';
                        """,
                        (str(e)[:500], tid_tpl, ck),
                    )
                    conn.commit()
            except Exception:
                pass
            return False, str(e), False

    def _try_claim_instance(
        self, conn, template_id: int, cycle_key: str, occurrence_date: dt.date,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        ck = str(cycle_key or "").strip()[:32]
        inst = self._get_instance_conn(conn, template_id, ck)
        if inst:
            st = str(inst.get("status") or "").upper()
            if st == "CREATED":
                return "already_done", inst
            if st == "SKIPPED":
                return "skipped", inst
            if st == "FAILED":
                conn.cursor().execute(
                    "UPDATE dbo.scheduled_task_instances SET status=N'PENDING', error_message=NULL WHERE id=?;",
                    (int(inst["id"]),),
                )
                return "claimed", self._get_instance_conn(conn, template_id, ck)
            return "claimed", inst
        try:
            conn.cursor().execute(
                """
                INSERT INTO dbo.scheduled_task_instances
                (template_id, cycle_key, occurrence_date, status)
                VALUES (?,?,?,N'PENDING');
                """,
                (int(template_id), ck, occurrence_date),
            )
            return "claimed", self._get_instance_conn(conn, template_id, ck)
        except Exception as e:
            if _is_unique_violation(e):
                inst2 = self._get_instance_conn(conn, template_id, ck)
                if inst2:
                    st2 = str(inst2.get("status") or "").upper()
                    if st2 == "CREATED":
                        return "already_done", inst2
                    if st2 == "SKIPPED":
                        return "skipped", inst2
                    return "claimed", inst2
            raise

    def _insert_task_from_template(
        self,
        conn,
        template_row: Dict[str, Any],
        occurrence_date: dt.date,
        username: str,
        display: str,
    ) -> str:
        tj: Dict[str, Any] = {}
        try:
            tj = json.loads(template_row.get("task_defaults_json") or "{}")
        except Exception:
            tj = {}
        if not isinstance(tj, dict):
            tj = {}
        values = {c: str(tj.get(c, "") or "") for c in TASK_WRITE_COLS if c in tj}
        title = f"{str(template_row.get('name') or '').strip()} ({occurrence_date.isoformat()})"
        values["Tarefa"] = title
        values["DataRegisto"] = dt.date.today().isoformat()
        values["InicioPrevisto"] = occurrence_date.isoformat()
        values["Prazo"] = occurrence_date.isoformat()
        if not str(values.get("DescricaoNotas") or "").strip():
            values["DescricaoNotas"] = f"Gerada por Programadas (template #{template_row.get('id')})."
        if not values.get("Responsavel"):
            values["Responsavel"] = display or username
        if not values.get("Estado"):
            values["Estado"] = "Não iniciado"
        if not values.get("Prioridade"):
            values["Prioridade"] = "Média"
        v = clean_task_payload(values)
        validate_task_save(v)
        tmp_tid = f"Task_TMP_{uuid.uuid4().hex[:10]}"
        insert_cols = ["TaskID"] + TASK_WRITE_COLS
        vals = [tmp_tid] + [v.get(c, "") for c in TASK_WRITE_COLS]
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO dbo.tasks ({', '.join(insert_cols)}) OUTPUT INSERTED.id VALUES ({', '.join(['?'] * len(insert_cols))});",
            vals,
        )
        new_id = int(cur.fetchone()[0])
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        final_tid = f"Task_{ts}_N{new_id}"
        cur.execute("UPDATE dbo.tasks SET TaskID=? WHERE id=?;", (final_tid, new_id))
        priv = int(tj.get("Private") or 0)
        cur.execute("UPDATE dbo.tasks SET Private=?, CreatedBy=? WHERE id=?;", (priv, username, new_id))
        self.da.add_task_history(conn, final_tid, username, "create", f"Tarefa gerada (Programadas #{template_row.get('id')})")
        if template_row.get("generate_default_actions") and template_row.get("action_defaults_json"):
            try:
                acts = json.loads(template_row.get("action_defaults_json") or "[]")
            except Exception:
                acts = []
            if isinstance(acts, list):
                for a in acts:
                    if not isinstance(a, dict):
                        continue
                    txt = str(a.get("text") or a.get("item_text") or "").strip() or "Ação"
                    try:
                        off = int(a.get("due_offset_days") or 0)
                    except Exception:
                        off = 0
                    due_d = occurrence_date + dt.timedelta(days=off)
                    owner_a = str(a.get("owner") or display or username).strip()
                    st = str(a.get("status") or "Não iniciado").strip()
                    done = 1 if st == "Concluído" else 0
                    item_uuid = uuid.uuid4().hex
                    cur.execute(
                        """
                        INSERT INTO dbo.task_checklist(TaskID, item_text, kind, owner, workers, start_date, due_date, status, evidence, blocked_reason, done, item_uuid, ord)
                        OUTPUT INSERTED.id VALUES (?, ?, N'ACTION', ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT MAX(ord)+1 FROM dbo.task_checklist WHERE TaskID=?),0));
                        """,
                        (
                            final_tid, txt, owner_a, str(a.get("workers") or ""),
                            occurrence_date.isoformat(), due_d.isoformat(), st,
                            str(a.get("evidence") or ""), str(a.get("blocked_reason") or ""),
                            done, item_uuid, final_tid,
                        ),
                    )
        return final_tid
