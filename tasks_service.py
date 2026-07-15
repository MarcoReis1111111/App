# -*- coding: utf-8 -*-
"""Serviço de Tarefas — listagem, detalhe, CRUD, dependências, histórico."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Dict, List, Optional

from tasks_common import (
    TASK_DB_COLS, TASK_PRIORITIES_DEFAULT, TASK_STATES_DEFAULT, TASK_WRITE_COLS,
    AppError, ConflictError, TasksDataAccess, can_edit_role, is_done_estado, jval, parse_date_iso,
    rowver_to_bytes, task_can_edit, task_visible,
)
from actions_service import ActionsService
from attachments_service import AttachmentsService
from archive_service import ArchiveService
from excel_filters import EXCEL_FILTER_COLS, column_unique_values, parse_excel_filters, row_matches_excel_filters
from files_service import folder_info
from gantt_service import GanttService
from planning_service import PlanningService


def clean_task_payload(values: Dict[str, Any]) -> Dict[str, Any]:
    today = dt.date.today().isoformat()
    v = dict(values or {})
    out: Dict[str, Any] = {}
    for k in TASK_WRITE_COLS:
        x = v.get(k, "")
        out[k] = x.strip() if isinstance(x, str) else ("" if x is None else x)
    out["Tarefa"] = str(out.get("Tarefa") or "").strip()
    out["Responsavel"] = str(out.get("Responsavel") or "").strip()
    out["Estado"] = str(out.get("Estado") or TASK_STATES_DEFAULT[0]).strip()
    out["Prioridade"] = str(out.get("Prioridade") or "Média").strip()
    out["DataRegisto"] = str(out.get("DataRegisto") or today)[:10]
    out["InicioPrevisto"] = str(out.get("InicioPrevisto") or today)[:10]
    out["Prazo"] = str(out["Prazo"])[:10] if out.get("Prazo") else ""
    out["Pessoal"] = ""
    priv = v.get("Private", v.get("private", 0))
    out["Private"] = 1 if str(priv) in ("1", "true", "True", "on") else 0
    return out


def validate_task_save(v: Dict[str, Any]) -> None:
    if not str(v.get("Tarefa") or "").strip():
        raise AppError("Tarefa é obrigatória")
    if not str(v.get("Responsavel") or "").strip():
        raise AppError("Responsável é obrigatório")
    for dk in ("DataRegisto", "InicioPrevisto", "Prazo"):
        if v.get(dk) and parse_date_iso(v[dk]) is None:
            raise AppError(f"Data inválida: {dk}")


def _estado_is_concluido(estado: Any) -> bool:
    s = str(estado or "").strip().lower()
    return s in ("concluído", "concluido")


def _conclusao_stamp_for_transition(prev_estado: Any, new_estado: Any, today: str) -> Optional[str]:
    """
    Retorna a data a gravar em DataConclusao, ou None para limpar.
    - Ao entrar em Concluído: grava `today` (última conclusão).
    - Ao sair de Concluído: limpa (NULL).
    - Sem transição: None especial via sentinel — use tuple.
    """
    prev_done = _estado_is_concluido(prev_estado)
    new_done = _estado_is_concluido(new_estado)
    if new_done and not prev_done:
        return today
    if (not new_done) and prev_done:
        return ""
    return None  # sem alteração


def _action_is_done(status: Any, done: Any = 0) -> bool:
    st = str(status or "").strip().lower()
    if st in ("concluído", "concluido"):
        return True
    try:
        return bool(int(done or 0))
    except Exception:
        return False


class TasksService:
    def __init__(self, da: TasksDataAccess, cache_dir_fn=None):
        self.da = da
        self.cache_dir_fn = cache_dir_fn
        self.actions = ActionsService(da)
        self.attachments = AttachmentsService(da, cache_dir_fn)
        self.planning = PlanningService(da, self.actions)
        self.archive = ArchiveService(da)
        self.gantt = GanttService()

    def task_lists(self) -> Dict[str, List[str]]:
        out = {
            "estados": list(TASK_STATES_DEFAULT),
            "prioridades": list(TASK_PRIORITIES_DEFAULT),
            "projects": [], "lines": [], "machines": [],
            "milestones": [], "assuntos": [],
        }
        try:
            with self.da.connect() as conn:
                cur = conn.cursor()
                cur.execute("SELECT tipo, valor FROM dbo.app_lists ORDER BY tipo, valor;")
                for tipo, valor in cur.fetchall():
                    t = str(tipo or "").strip()
                    v = str(valor or "").strip()
                    if t in out and v and v not in out[t]:
                        out[t].append(v)
        except Exception:
            pass
        return out

    def users_display(self) -> List[str]:
        try:
            with self.da.connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT COALESCE(display_name, username) FROM dbo.users WHERE COALESCE(active,1)=1 ORDER BY COALESCE(display_name,username);"
                )
                return [str(r[0]) for r in cur.fetchall() if str(r[0] or "").strip()]
        except Exception:
            return []

    def _enrich_row(self, d: Dict[str, Any], wmap: Dict[str, str], blocked: Dict[str, int], cfg: Dict[str, Any]) -> Dict[str, Any]:
        tid = str(d.get("TaskID") or "")
        d["Workers"] = wmap.get(tid, "")
        d["blocked_count"] = int(blocked.get(tid, 0))
        estado = str(d.get("Estado") or "").strip()
        prazo_s = str(d.get("Prazo") or "")[:10]
        today = dt.date.today()
        is_overdue = False
        if prazo_s and not is_done_estado(estado):
            try:
                is_overdue = dt.date.fromisoformat(prazo_s) < today
            except Exception:
                pass
        d["is_overdue"] = is_overdue
        data_reg = str(d.get("DataRegisto") or "")[:10]
        is_recent = False
        if data_reg and estado == "Não iniciado":
            try:
                is_recent = (today - dt.date.fromisoformat(data_reg)).days <= 7
            except Exception:
                pass
        d["is_recent"] = is_recent
        self.da.apply_notifications(d, cfg)
        return d

    def list_tasks(
        self, f: Dict[str, Any], username: str, display: str, role: str, cfg: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        cols_sql = ", ".join(f"t.[{c}]" for c in TASK_DB_COLS)
        rowver_sql = ""
        try:
            with self.da.connect() as conn:
                self.da.ensure_tasks_data_conclusao(conn)
                conn.commit()
                cur0 = conn.cursor()
                cur0.execute(
                    "SELECT COUNT(*) FROM sys.columns WHERE object_id=OBJECT_ID('dbo.tasks') AND name='RowVer';"
                )
                if int((cur0.fetchone() or [0])[0] or 0) > 0:
                    rowver_sql = ", t.RowVer"
        except Exception:
            pass
        sql = f"""
SELECT {cols_sql}, COALESCE(t.Private,0), COALESCE(t.CreatedBy,N''),
    CONVERT(VARCHAR(30), t.updated_at, 126) AS updated_at{rowver_sql}
FROM dbo.tasks t ORDER BY t.id;
"""
        with self.da.connect() as conn:
            try:
                self.da.ensure_tasks_data_conclusao(conn)
                conn.commit()
            except Exception:
                pass
            wmap, blocked = self.da.workers_blocked_maps(conn)
            cur = conn.cursor()
            cur.execute(sql)
            db_cols = [d[0] for d in cur.description]
            raw_rows = cur.fetchall()

        out: List[Dict[str, Any]] = []
        q = str(f.get("q") or "").strip().lower()
        f_prep = dict(f)
        f_prep["_excel_filters"] = parse_excel_filters(f.get("excel_filters"))
        if str(f_prep.get("involved_mode") or "") in ("1", "2", "3"):
            if str(f_prep.get("involved_mode")) == "3":
                f_prep["_involved_action_ids"] = self._involved_action_task_ids(username)
        for row in raw_rows:
            d = {k: jval(v) for k, v in zip(db_cols, row)}
            tid = str(d.get("TaskID") or "")
            priv = int(d.get("Private") or 0)
            created_by = str(d.get("CreatedBy") or "")
            responsavel = str(d.get("Responsavel") or "")
            if not task_visible(priv, created_by, responsavel, username, display, role):
                continue
            d["Private"] = priv
            self._enrich_row(d, wmap, blocked, cfg)
            if not self._match_filters(d, f_prep, display, q):
                continue
            out.append(d)
        return out

    def _match_filters(self, d: Dict[str, Any], f: Dict[str, Any], display: str, q: str) -> bool:
        estado = str(d.get("Estado") or "").strip()
        responsavel = str(d.get("Responsavel") or "")
        if f.get("estado") and f["estado"] not in ("Todos", "") and estado != f["estado"]:
            return False
        if f.get("prioridade") and f["prioridade"] not in ("Todas", "") and str(d.get("Prioridade") or "") != f["prioridade"]:
            return False
        if f.get("milestone") and f["milestone"] not in ("Todos", "") and str(d.get("Milestone") or "") != f["milestone"]:
            return False
        if f.get("assunto") and f["assunto"] not in ("Todos", "") and str(d.get("Assunto") or "") != f["assunto"]:
            return False
        if f.get("projeto") and f["projeto"] not in ("Todos", "") and str(d.get("Projeto") or "") != f["projeto"]:
            return False
        if f.get("linha") and f["linha"] not in ("Todos", "") and str(d.get("Linha") or "") != f["linha"]:
            return False
        if f.get("maquina") and f["maquina"] not in ("Todos", "") and str(d.get("Maquina") or "") != f["maquina"]:
            return False
        if f.get("responsavel") and f["responsavel"] not in ("Todos", "") and responsavel != f["responsavel"]:
            return False
        if f.get("date_from"):
            ref = str(d.get("Prazo") or d.get("DataRegisto") or "")[:10]
            if not ref or ref < f["date_from"]:
                return False
        if f.get("date_to"):
            ref = str(d.get("Prazo") or d.get("DataRegisto") or "")[:10]
            if not ref or ref > f["date_to"]:
                return False
        if f.get("only_mine"):
            disp = str(display or "").strip()
            wk = [s.strip() for s in str(d.get("Workers") or "").split(",") if s.strip()]
            if not (responsavel == disp or disp in wk):
                return False
        if f.get("involved_mode") in ("1", "2", "3"):
            if not self._involved_match(d, f, display):
                return False
        if f.get("blocked_only") and int(d.get("blocked_count") or 0) <= 0:
            return False
        if f.get("overdue_only") and not d.get("is_overdue"):
            return False
        if not f.get("show_done"):
            est_f = str(f.get("estado") or "").strip().lower()
            if est_f not in ("concluído", "concluido") and _estado_is_concluido(estado):
                return False
        excel_f = f.get("_excel_filters")
        if excel_f and not row_matches_excel_filters(d, excel_f):
            return False
        if q:
            hay = " ".join([
                str(d.get("TaskID") or ""), str(d.get("Tarefa") or ""),
                str(d.get("DescricaoNotas") or ""), str(d.get("Responsavel") or ""),
                str(d.get("Workers") or ""),
            ]).lower()
            if q not in hay:
                return False
        return True

    def _involved_match(self, d: Dict[str, Any], f: Dict[str, Any], display: str) -> bool:
        mode = str(f.get("involved_mode") or "1")
        u_norm = str(display or "").strip().lower()
        if not u_norm:
            return False
        resp_norm = str(d.get("Responsavel") or "").strip().lower()
        if resp_norm == u_norm:
            return True
        if mode == "1":
            return False
        workers_list = [w.strip().lower() for w in str(d.get("Workers") or "").split(",") if w.strip()]
        if u_norm in workers_list:
            return True
        if mode == "2":
            return False
        return bool(f.get("_involved_action_ids") and str(d.get("TaskID") or "") in f["_involved_action_ids"])

    def _involved_action_task_ids(self, username: str) -> set:
        u = str(username or "").strip().lower()
        if not u:
            return set()
        out: set = set()
        try:
            with self.da.connect() as conn:
                cur = conn.cursor()
                cur.execute("""
SELECT DISTINCT TaskID, COALESCE(owner,''), COALESCE(workers,'')
FROM dbo.task_checklist
WHERE COALESCE(kind,'CHECK')=N'ACTION'
  AND (LOWER(COALESCE(owner,''))=? OR LOWER(COALESCE(workers,'')) LIKE ?);
""", (u, f"%{u}%"))
                for tid, owner, workers in cur.fetchall():
                    if str(owner or "").strip().lower() == u:
                        out.add(str(tid))
                        continue
                    for tok in str(workers or "").split(","):
                        if tok.strip().lower() == u:
                            out.add(str(tid))
                            break
        except Exception:
            pass
        return out

    def get_dependencies(self, task_id: str) -> List[str]:
        return self.planning.get_dependencies(task_id)

    def set_dependencies(self, task_id: str, deps: List[str], username: str, display: str, role: str) -> None:
        return self.planning.set_dependencies(task_id, deps, username, display, role)

    def recalc_task_from_deps(self, task_id: str, username: str, display: str, role: str) -> Optional[str]:
        if not can_edit_role(role):
            raise PermissionError("Sem permissão")
        tid = str(task_id or "").strip()
        with self.da.connect() as conn:
            row = self.da.fetch_task_row(conn, tid)
            if not row or not task_can_edit(row, username, display, role):
                raise PermissionError("Sem permissão")
        return self.planning.recalc_task_start_from_deps(tid)

    def sync_prazo_from_actions(self, task_id: str, username: str, display: str, role: str) -> str:
        return self.planning.sync_prazo_from_actions(task_id, username, display, role)

    def dependency_candidates(
        self, task_id: str, milestone: str, username: str, display: str, role: str, cfg: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        tid = str(task_id or "").strip()
        ms = str(milestone or "").strip()
        out: List[Dict[str, Any]] = []
        for r in self.list_tasks({}, username, display, role, cfg):
            if str(r.get("TaskID") or "") == tid:
                continue
            if ms and str(r.get("Milestone") or "").strip() != ms:
                continue
            out.append({
                "TaskID": r.get("TaskID"),
                "Tarefa": r.get("Tarefa"),
                "Estado": r.get("Estado"),
                "Prazo": r.get("Prazo"),
            })
        return out

    def get_task(self, task_id: str, username: str, display: str, role: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for r in self.list_tasks({"q": task_id}, username, display, role, cfg):
            if str(r.get("TaskID") or "") == str(task_id or "").strip():
                return r
        return None

    def list_history(self, task_id: str, limit: int = 300) -> List[Dict[str, Any]]:
        tid = str(task_id or "").strip()
        if not tid:
            return []
        try:
            with self.da.connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"SELECT TOP {int(limit)} ts, [user], event, details FROM dbo.task_history WHERE TaskID=? ORDER BY id DESC;",
                    (tid,),
                )
                cols = [d[0] for d in cur.description]
                return [{k: jval(v) for k, v in zip(cols, row)} for row in cur.fetchall()]
        except Exception:
            return []

    def get_task_detail(
        self, task_id: str, username: str, display: str, role: str, cfg: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        task = self.get_task(task_id, username, display, role, cfg)
        if not task:
            return None
        tid = str(task_id)
        checklist = self.actions.list_checklist(tid, username, display, role, include_all=True)
        actions = [x for x in checklist if str(x.get("kind") or "CHECK") == "ACTION"]
        checks = [x for x in checklist if str(x.get("kind") or "CHECK") != "ACTION"]
        progress = self.actions.progress_stats(actions)
        deps = self.get_dependencies(tid)
        dep_tasks: List[Dict[str, Any]] = []
        for dep_id in deps:
            dr = self.get_task(dep_id, username, display, role, cfg)
            if dr:
                dep_tasks.append({"TaskID": dr.get("TaskID"), "Tarefa": dr.get("Tarefa"), "Estado": dr.get("Estado"), "Prazo": dr.get("Prazo")})
        folder = folder_info(cfg, tid, str(task.get("Pasta") or ""), self.cache_dir_fn)
        attachments = self.attachments.list_for_task(cfg, tid, str(task.get("Pasta") or ""), self.cache_dir_fn)
        planning = self.planning.summary(task, actions)
        history = self.list_history(tid)
        candidates = self.dependency_candidates(tid, str(task.get("Milestone") or ""), username, display, role, cfg)
        gantt = self.gantt.build_gantt_data(task, actions)
        return {
            "task": task,
            "checklist": checks,
            "actions": actions,
            "actions_progress": progress,
            "dependencies": deps,
            "dependency_tasks": dep_tasks,
            "dependency_candidates": candidates,
            "folder": folder,
            "attachments": attachments,
            "planning": planning,
            "history": history,
            "gantt": gantt,
            "can_edit": task_can_edit(task, username, display, role),
            "can_see": True,
        }

    def _gantt_progress_from_status(self, status: str) -> int:
        st = str(status or "").strip().lower()
        if st in ("concluído", "concluido"):
            return 100
        if st in ("em progresso", "em progresso "):
            return 50
        return 0

    def _normalize_gantt_dates(self, start_raw: Any, due_raw: Any) -> tuple[Optional[str], Optional[str], Optional[str]]:
        start_s = str(start_raw or "").strip()[:10]
        due_s = str(due_raw or "").strip()[:10]
        start_d = parse_date_iso(start_s) if start_s else None
        due_d = parse_date_iso(due_s) if due_s else None
        if start_s and start_d is None:
            return None, None, "start_date inválida"
        if due_s and due_d is None:
            return None, None, "due_date inválida"
        if start_d and due_d:
            return start_d.isoformat(), due_d.isoformat(), None
        if start_d and not due_d:
            one = start_d.isoformat()
            return one, one, None
        if due_d and not start_d:
            one = due_d.isoformat()
            return one, one, None
        return None, None, "sem datas"

    def _action_dependencies_map(self, action_ids: List[int]) -> Dict[int, str]:
        out: Dict[int, str] = {}
        ids = [int(x) for x in action_ids if int(x or 0) > 0]
        if not ids:
            return out
        marks = ",".join(["?"] * len(ids))
        sql = (
            "SELECT action_id, depends_on "
            "FROM dbo.action_dependencies "
            f"WHERE COALESCE(is_deleted,0)=0 AND action_id IN ({marks}) "
            "ORDER BY action_id, depends_on;"
        )
        try:
            with self.da.connect() as conn:
                cur = conn.cursor()
                cur.execute(sql, tuple(ids))
                dep_map: Dict[int, List[str]] = {}
                for action_id, depends_on in cur.fetchall() or []:
                    aid = int(action_id or 0)
                    did = int(depends_on or 0)
                    if aid <= 0 or did <= 0:
                        continue
                    dep_map.setdefault(aid, []).append(f"action_{did}")
                for aid, deps in dep_map.items():
                    out[aid] = ",".join(deps)
        except Exception:
            return {}
        return out

    def _ensure_all_actions_done(self, conn, task_id: str) -> None:
        cur = conn.cursor()
        deleted_filter = ""
        try:
            cur.execute(
                "SELECT COUNT(*) FROM sys.columns WHERE object_id=OBJECT_ID('dbo.task_checklist') AND name='is_deleted';"
            )
            if int((cur.fetchone() or [0])[0] or 0) > 0:
                deleted_filter = " AND ISNULL(is_deleted,0)=0"
        except Exception:
            pass
        cur.execute(
            f"""
SELECT COALESCE(item_text,''), COALESCE(status,''), COALESCE(done,0), COALESCE(kind,'CHECK')
FROM dbo.task_checklist
WHERE TaskID=?{deleted_filter};
""",
            (task_id,),
        )
        open_actions: List[str] = []
        open_checks: List[str] = []
        for text, status, done, kind in cur.fetchall() or []:
            if _action_is_done(status, done):
                continue
            label = str(text or "").strip() or "(sem texto)"
            if str(kind or "CHECK").strip().upper() == "ACTION":
                open_actions.append(label)
            else:
                open_checks.append(label)
        if not open_actions and not open_checks:
            return
        parts: List[str] = []
        if open_actions:
            na = len(open_actions)
            if na == 1:
                parts.append(f"1 ação («{open_actions[0]}»)")
            else:
                preview = "», «".join(open_actions[:3])
                extra = f" (+{na - 3} mais)" if na > 3 else ""
                parts.append(f"{na} ações («{preview}»{extra})")
        if open_checks:
            nc = len(open_checks)
            if nc == 1:
                parts.append(f"1 check («{open_checks[0]}»)")
            else:
                preview = "», «".join(open_checks[:3])
                extra = f" (+{nc - 3} mais)" if nc > 3 else ""
                parts.append(f"{nc} checks («{preview}»{extra})")
        raise AppError(f"Não pode concluir a tarefa: ainda existem {', '.join(parts)} por concluir.")

    def get_task_gantt_data(
        self, task_id: str, username: str, display: str, role: str, cfg: Dict[str, Any]
    ) -> Dict[str, Any]:
        tid = str(task_id or "").strip()
        if not tid:
            raise AppError("TaskID em falta")
        with self.da.connect() as conn:
            row = self.da.fetch_task_row(conn, tid)
        if not row:
            raise AppError("Tarefa não encontrada")
        if not task_visible(
            int(row.get("Private") or 0),
            row.get("CreatedBy", ""),
            row.get("Responsavel", ""),
            username,
            display,
            role,
        ):
            raise PermissionError("Sem permissão")
        can_edit = bool(task_can_edit(row, username, display, role))
        checklist = self.actions.list_checklist(tid, username, display, role, include_all=True)
        action_ids = [int(x.get("id") or 0) for x in checklist if int(x.get("id") or 0) > 0]
        deps_map = self._action_dependencies_map(action_ids)
        today = dt.date.today().isoformat()
        items: List[Dict[str, Any]] = []
        undated: List[Dict[str, Any]] = []
        for it in checklist:
            kind = str(it.get("kind") or "CHECK").strip().upper()
            if kind not in ("ACTION", "CHECK"):
                continue
            aid = int(it.get("id") or 0)
            name = str(it.get("item_text") or "").strip()
            status = str(it.get("status") or "").strip() or ("Concluído" if it.get("is_done") else "Não iniciado")
            owner = str(it.get("owner_display") or it.get("owner") or "").strip()
            workers = str(it.get("workers") or "").strip()
            start, end, reason = self._normalize_gantt_dates(it.get("start_date"), it.get("due_date"))
            if not (start and end):
                undated.append(
                    {
                        "id": f"action_{aid}",
                        "action_id": aid,
                        "name": name,
                        "status": status,
                        "owner": owner,
                        "workers": workers,
                        "kind": kind,
                        "reason": reason or "sem datas válidas",
                    }
                )
                continue
            is_overdue = bool(end < today and str(status).strip().lower() not in ("concluído", "concluido"))
            items.append(
                {
                    "id": f"action_{aid}",
                    "action_id": aid,
                    "name": name,
                    "start": start,
                    "end": end,
                    "progress": self._gantt_progress_from_status(status),
                    "status": status,
                    "owner": owner,
                    "workers": workers,
                    "kind": kind,
                    "dependencies": deps_map.get(aid, "") if kind == "ACTION" else "",
                    "is_overdue": is_overdue,
                }
            )
        return {
            "task_id": tid,
            "task_title": str(row.get("Tarefa") or tid),
            "items": items,
            "undated_items": undated,
            "permissions": {"can_edit": can_edit},
        }

    def insert_task(self, values: Dict[str, Any], username: str, display: str, role: str) -> str:
        if not can_edit_role(role):
            raise PermissionError("Sem permissão para criar tarefas")
        v = clean_task_payload(values)
        if not v.get("Responsavel"):
            v["Responsavel"] = display or username
        validate_task_save(v)
        tmp_tid = f"Task_TMP_{uuid.uuid4().hex[:10]}"
        insert_cols = ["TaskID"] + TASK_WRITE_COLS
        vals = [tmp_tid] + [v.get(c, "") for c in TASK_WRITE_COLS]
        with self.da.lock, self.da.connect() as conn:
            cur = conn.cursor()
            self.da.ensure_tasks_data_conclusao(conn)
            cur.execute(
                f"INSERT INTO dbo.tasks ({', '.join(insert_cols)}) OUTPUT INSERTED.id VALUES ({', '.join(['?'] * len(insert_cols))});",
                vals,
            )
            new_id = int(cur.fetchone()[0])
            ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            final_tid = f"Task_{ts}_N{new_id}"
            cur.execute("UPDATE dbo.tasks SET TaskID=? WHERE id=?;", (final_tid, new_id))
            cur.execute("UPDATE dbo.tasks SET Private=?, CreatedBy=? WHERE id=?;", (int(v.get("Private") or 0), username, new_id))
            if _estado_is_concluido(v.get("Estado")):
                today = dt.date.today().isoformat()
                cur.execute("UPDATE dbo.tasks SET DataConclusao=? WHERE id=?;", (today, new_id))
            self.da.add_task_history(conn, final_tid, username, "create", f"Tarefa criada: {v.get('Tarefa', '')}")
            conn.commit()
            return final_tid

    def set_task_pasta(self, task_id: str, pasta_rel: str) -> None:
        tid = str(task_id or "").strip()
        if not tid:
            raise AppError("TaskID em falta")
        with self.da.lock, self.da.connect() as conn:
            conn.cursor().execute("UPDATE dbo.tasks SET Pasta=? WHERE TaskID=?;", (str(pasta_rel or "").strip(), tid))
            conn.commit()

    def update_task(self, task_id: str, values: Dict[str, Any], username: str, display: str, role: str) -> None:
        if not can_edit_role(role):
            raise PermissionError("Sem permissão para editar tarefas")
        tid = str(task_id or "").strip()
        raw = dict(values or {})
        auto_prazo = bool(raw.pop("auto_prazo", False) or raw.pop("_auto_prazo", False))
        recalc_deps = bool(raw.pop("recalc_deps", False) or raw.pop("_recalc_deps", False))
        deps = raw.pop("dependencies", None)
        force = bool(raw.pop("force", False))
        expected_rowver = raw.pop("RowVer", None) if "RowVer" in raw else raw.pop("rowver", None)
        v = clean_task_payload(raw)
        validate_task_save(v)
        with self.da.lock, self.da.connect() as conn:
            self.da.ensure_tasks_data_conclusao(conn)
            row = self.da.fetch_task_row(conn, tid)
            if not row:
                raise AppError("Tarefa não encontrada")
            if not task_visible(int(row.get("Private") or 0), row.get("CreatedBy", ""), row.get("Responsavel", ""), username, display, role):
                raise PermissionError("Sem permissão para ver/editar esta tarefa")
            if not task_can_edit(row, username, display, role):
                raise PermissionError("Sem permissão para editar esta tarefa")
            prev_estado = str(row.get("Estado") or "")
            new_estado = str(v.get("Estado") or "")
            if _estado_is_concluido(new_estado) and not _estado_is_concluido(prev_estado):
                self._ensure_all_actions_done(conn, tid)
            sets = ", ".join(f"[{c}]=?" for c in TASK_WRITE_COLS)
            params = [v.get(c, "") for c in TASK_WRITE_COLS]
            cur = conn.cursor()
            rv_bytes = rowver_to_bytes(expected_rowver) if not force else None
            if rv_bytes is not None:
                cur.execute(f"UPDATE dbo.tasks SET {sets} WHERE TaskID=? AND RowVer=?;", params + [tid, rv_bytes])
                if int(cur.rowcount or 0) == 0:
                    raise ConflictError(
                        "Conflito de versão — outro utilizador alterou a tarefa. Atualize e tente novamente."
                    )
            else:
                cur.execute(f"UPDATE dbo.tasks SET {sets} WHERE TaskID=?;", params + [tid])
            cur.execute(
                "UPDATE dbo.tasks SET Private=?, CreatedBy=? WHERE TaskID=?;",
                (int(v.get("Private") or 0), row.get("CreatedBy") or username, tid),
            )
            today = dt.date.today().isoformat()
            stamp = _conclusao_stamp_for_transition(prev_estado, new_estado, today)
            if stamp is not None:
                if stamp == "":
                    cur.execute("UPDATE dbo.tasks SET DataConclusao=NULL WHERE TaskID=?;", (tid,))
                else:
                    cur.execute("UPDATE dbo.tasks SET DataConclusao=? WHERE TaskID=?;", (stamp, tid))
                    self.da.add_task_history(
                        conn, tid, username, "change", f"Data conclusão: {stamp}"
                    )
            if prev_estado.strip() != "Concluído" and new_estado.strip() == "Concluído":
                try:
                    arch_row = dict(row)
                    arch_row.update(v)
                    arch_row["Estado"] = new_estado
                    if stamp:
                        arch_row["DataConclusao"] = stamp
                    self.archive.archive_conn(conn, tid, "completed", username, arch_row)
                except Exception:
                    pass
            if prev_estado != new_estado:
                self.da.add_task_history(conn, tid, username, "change", f"Estado: '{prev_estado}' -> '{new_estado}'")
            self.da.add_task_history(conn, tid, username, "update", "Tarefa atualizada (Web UI)")
            conn.commit()
        if deps is not None:
            self.set_dependencies(tid, deps if isinstance(deps, list) else [], username, display, role)
        if auto_prazo:
            self.sync_prazo_from_actions(tid, username, display, role)
        if recalc_deps:
            self.recalc_task_from_deps(tid, username, display, role)

    def delete_task(self, task_id: str, username: str, display: str, role: str, cfg: Dict[str, Any]) -> None:
        if not can_edit_role(role):
            raise PermissionError("Sem permissão para apagar tarefas")
        tid = str(task_id or "").strip()
        with self.da.lock, self.da.connect() as conn:
            row = self.da.fetch_task_row(conn, tid)
            if not row:
                raise AppError("Tarefa não encontrada")
            if not task_visible(int(row.get("Private") or 0), row.get("CreatedBy", ""), row.get("Responsavel", ""), username, display, role):
                raise PermissionError("Sem permissão")
            if not task_can_edit(row, username, display, role):
                raise PermissionError("Sem permissão para apagar esta tarefa")
            cur = conn.cursor()
            try:
                self.archive.archive_conn(conn, tid, "deleted", username, row)
            except Exception:
                pass
            self.da.add_task_history(conn, tid, username, "delete", "Apagado (Web UI)")
            cur.execute("DELETE FROM dbo.task_dependencies WHERE task_id=? OR depends_on=?;", (tid, tid))
            cur.execute("DELETE FROM dbo.task_checklist WHERE TaskID=?;", (tid,))
            cur.execute("DELETE FROM dbo.tasks WHERE TaskID=?;", (tid,))
            conn.commit()

    def duplicate_task(self, task_id: str, username: str, display: str, role: str, cfg: Dict[str, Any], copy_actions: bool = True) -> str:
        src = self.get_task(task_id, username, display, role, cfg)
        if not src:
            raise AppError("Tarefa não encontrada")
        payload = {k: src.get(k, "") for k in TASK_WRITE_COLS}
        payload["Tarefa"] = f"{payload.get('Tarefa', '')} (cópia)".strip()
        payload["Estado"] = "Não iniciado"
        payload["Private"] = src.get("Private", 0)
        new_tid = self.insert_task(payload, username, display, role)
        if copy_actions:
            for a in self.actions.list_actions(task_id, username, display, role):
                self.actions.insert_action(new_tid, {
                    "item_text": a.get("item_text"), "owner": a.get("owner"), "workers": a.get("workers"),
                    "start_date": a.get("start_date"), "due_date": a.get("due_date"), "status": a.get("status"),
                    "evidence": a.get("evidence"), "blocked_reason": a.get("blocked_reason"),
                }, username, display, role)
        return new_tid

    def column_filter_values(
        self, col: str, username: str, display: str, role: str, cfg: Dict[str, Any], base_f: Optional[Dict[str, Any]] = None
    ) -> List[str]:
        c = str(col or "").strip()
        if c not in EXCEL_FILTER_COLS:
            return []
        bf = dict(base_f or {})
        bf.pop("excel_filters", None)
        rows = self.list_tasks(bf, username, display, role, cfg)
        return column_unique_values(rows, c)

    def list_archives(self, limit: int = 500) -> List[Dict[str, Any]]:
        return self.archive.list_archives(limit)

    def restore_archive(self, archive_id: int, username: str, display: str, role: str) -> str:
        return self.archive.restore(archive_id, username, display, role)

    def delete_archive(self, archive_id: int, role: str) -> None:
        return self.archive.delete_archive(archive_id, role)

    def add_history_note(
        self, task_id: str, note: str, username: str, display: str, role: str, cfg: Dict[str, Any]
    ) -> None:
        tid = str(task_id or "").strip()
        text = str(note or "").strip()
        if not text:
            raise AppError("Nota em falta")
        if not self.get_task(tid, username, display, role, cfg):
            raise AppError("Tarefa não encontrada")
        if not can_edit_role(role):
            raise PermissionError("Sem permissão")
        with self.da.lock, self.da.connect() as conn:
            self.da.add_task_history(conn, tid, display or username, "NOTA", text)
            conn.commit()
