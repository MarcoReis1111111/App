# -*- coding: utf-8 -*-
"""Constantes, permissões e acesso SQL partilhado pelos serviços de Tarefas."""
from __future__ import annotations

import datetime as dt
import decimal
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

try:
    import pyodbc  # type: ignore
except Exception:
    pyodbc = None

TASK_STATES_DEFAULT = ("Não iniciado", "A Fazer", "Em Progresso", "Bloqueado", "Concluído")
TASK_PRIORITIES_DEFAULT = ("Baixa", "Média", "Alta")
TASK_DB_COLS = [
    "TaskID", "Tarefa", "DescricaoNotas", "Milestone", "Assunto", "DataRegisto", "InicioPrevisto", "Responsavel",
    "Estado", "Prioridade", "Prazo", "DataConclusao", "Projeto", "Linha", "Maquina", "Pasta",
    "ResultadoInicial", "ResultadoFinal", "Links",
]
TASK_LIST_COLS = TASK_DB_COLS + [
    "Workers", "Notificacoes", "NotifEmoji", "Private", "CreatedBy",
    "blocked_count", "is_overdue", "is_recent", "updated_at",
]
# DataConclusao é gerida pelo servidor (stamp ao concluir); não vem do payload do browser.
TASK_WRITE_COLS = [c for c in TASK_DB_COLS if c not in ("TaskID", "DataConclusao")]
ACTION_STATUSES = ("Não iniciado", "Em Progresso", "Bloqueado", "Concluído")
CHECKLIST_KINDS = ("CHECK", "ACTION")


class AppError(RuntimeError):
    pass


class ConflictError(AppError):
    """RowVer / conflito de versão otimista."""
    pass


def utcnow_iso() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def is_done_estado(estado: Any) -> bool:
    s = str(estado or "").strip().lower()
    return s in ("concluído", "concluido")


def rowver_to_bytes(rv: Any) -> Optional[bytes]:
    if rv is None:
        return None
    if isinstance(rv, (bytes, bytearray)):
        return bytes(rv)
    s = str(rv).strip()
    if not s:
        return None
    try:
        return bytes.fromhex(s)
    except Exception:
        return None


def jval(v: Any) -> Any:
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, bytes):
        return v.hex()
    return v


def is_admin(role: str) -> bool:
    return str(role or "").lower() == "admin"


def can_edit_role(role: str) -> bool:
    return str(role or "").lower() in ("edit", "admin")


def format_taskid_display(taskid: Any) -> str:
    s = str(taskid or "").strip()
    if not s:
        return ""
    if s.startswith("Task_N"):
        return s
    m = re.search(r"_N(\d+)", s)
    if m:
        return f"Task_N{m.group(1)}"
    return s if len(s) <= 16 else (s[:13] + "...")


def parse_date_iso(s: Any) -> Optional[dt.date]:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def task_visible(priv: int, created_by: str, responsavel: str, username: str, display: str, role: str) -> bool:
    if int(priv or 0) == 0:
        return True
    if is_admin(role):
        return True
    if str(created_by or "").lower() == str(username or "").lower():
        return True
    if str(responsavel or "").strip() == str(display or "").strip():
        return True
    return False


def task_can_edit(row: Dict[str, Any], username: str, display: str, role: str) -> bool:
    if not can_edit_role(role):
        return False
    if is_admin(role):
        return True
    if str(row.get("CreatedBy") or "").lower() == str(username or "").lower():
        return True
    if str(row.get("Responsavel") or "").strip() == str(display or "").strip():
        return True
    return False


def conn_str(cfg: Dict[str, Any]) -> str:
    ss = cfg.get("sqlserver") or {}
    driver = ss.get("driver") or "ODBC Driver 18 for SQL Server"
    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={ss.get('server') or ''}",
        f"DATABASE={ss.get('database') or ''}",
        f"Encrypt={'yes' if ss.get('encrypt', True) else 'no'}",
        f"TrustServerCertificate={'yes' if ss.get('trust_server_certificate', True) else 'no'}",
    ]
    if str(ss.get("auth") or "windows").lower() == "sql":
        parts += [f"UID={ss.get('username') or ''}", f"PWD={ss.get('password') or ''}"]
    else:
        parts += ["Trusted_Connection=yes"]
    return ";".join(parts) + ";"


class TasksDataAccess:
    """Ligação SQL Server e helpers partilhados (sem UI)."""

    def __init__(self, cfg: Dict[str, Any], lock: Optional[threading.RLock] = None):
        self.cfg = cfg
        self.lock = lock or threading.RLock()

    def connect(self):
        if pyodbc is None:
            raise AppError("pyodbc não está instalado. Instala pyodbc para usar SQL Server.")
        try:
            c = pyodbc.connect(conn_str(self.cfg), timeout=10, autocommit=False)
            try:
                c.timeout = 30
            except Exception:
                pass
            return c
        except Exception as ex:
            raise AppError(f"Falha ao ligar ao SQL Server: {ex}") from ex

    def ensure_tasks_data_conclusao(self, conn) -> None:
        """Garante coluna dbo.tasks.DataConclusao (DATE NULL)."""
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM sys.columns WHERE object_id=OBJECT_ID('dbo.tasks') AND name='DataConclusao';"
        )
        if int((cur.fetchone() or [0])[0] or 0) > 0:
            return
        cur.execute("ALTER TABLE dbo.tasks ADD DataConclusao DATE NULL;")

    def user_map(self, conn) -> Dict[str, str]:
        try:
            cur = conn.cursor()
            cur.execute("SELECT username, COALESCE(display_name, username) FROM dbo.users WHERE COALESCE(active,1)=1;")
            return {str(r[0]): str(r[1] or r[0]) for r in cur.fetchall() if str(r[0] or "").strip()}
        except Exception:
            return {}

    def workers_blocked_maps(self, conn) -> Tuple[Dict[str, str], Dict[str, int]]:
        wmap: Dict[str, List[str]] = {}
        blocked: Dict[str, int] = {}
        try:
            cur = conn.cursor()
            # Inclui variantes com/sem acento; filtra concluídas em Python via is_done_estado.
            cur.execute("""
SELECT TaskID, COALESCE(owner,''), COALESCE(workers,''), COALESCE(status,''), COALESCE(done,0)
FROM dbo.task_checklist
WHERE COALESCE(kind,'CHECK')=N'ACTION';
""")
            user_map = self.user_map(conn)
            for tid, owner, workers, status, done in cur.fetchall():
                tid = str(tid or "")
                st = str(status or "")
                try:
                    done_i = int(done or 0)
                except Exception:
                    done_i = 0
                if is_done_estado(st) or done_i:
                    continue
                if st.strip() == "Bloqueado":
                    blocked[tid] = int(blocked.get(tid, 0)) + 1
                arr: List[str] = []
                o = str(owner or "").strip()
                if o:
                    arr.append(user_map.get(o, o))
                for tok in str(workers or "").split(","):
                    t = tok.strip()
                    if t:
                        arr.append(user_map.get(t, t))
                if arr:
                    wmap.setdefault(tid, []).extend(arr)
        except Exception:
            pass
        out_w: Dict[str, str] = {}
        for tid, lst in wmap.items():
            seen: set = set()
            parts: List[str] = []
            for t in lst:
                if t and t not in seen:
                    seen.add(t)
                    parts.append(t)
            out_w[tid] = ", ".join(parts)
        return out_w, blocked

    def fetch_task_row(self, conn, task_id: str) -> Optional[Dict[str, Any]]:
        self.ensure_tasks_data_conclusao(conn)
        cols_sql = ", ".join(f"t.[{c}]" for c in TASK_DB_COLS)
        rowver_sql = ""
        try:
            cur0 = conn.cursor()
            cur0.execute(
                "SELECT COUNT(*) FROM sys.columns WHERE object_id=OBJECT_ID('dbo.tasks') AND name='RowVer';"
            )
            if int((cur0.fetchone() or [0])[0] or 0) > 0:
                rowver_sql = ", t.RowVer"
        except Exception:
            pass
        cur = conn.cursor()
        cur.execute(
            f"""SELECT {cols_sql}, COALESCE(t.Private,0), COALESCE(t.CreatedBy,N''),
                CONVERT(VARCHAR(30), t.updated_at, 126) AS updated_at{rowver_sql}
            FROM dbo.tasks t WHERE t.TaskID=?;""",
            (task_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        db_cols = [d[0] for d in cur.description]
        d = {k: jval(v) for k, v in zip(db_cols, row)}
        d["Private"] = int(d.pop("Private", 0) or 0)
        d["CreatedBy"] = str(d.pop("CreatedBy", "") or "")
        return d

    def add_task_history(self, conn, task_id: str, user: str, event: str, details: str) -> None:
        try:
            conn.cursor().execute(
                "INSERT INTO dbo.task_history (ts, TaskID, [user], event, details) VALUES (?,?,?,?,?);",
                (utcnow_iso(), str(task_id or ""), str(user or "-"), str(event or ""), str(details or "")),
            )
        except Exception:
            pass

    def apply_notifications(self, d: Dict[str, Any], cfg: Dict[str, Any]) -> None:
        """Calcula Notificacoes e NotifEmoji (mesma lógica que db_fetch_all no Tkinter)."""
        emoji_bloqueado = str(cfg.get("emoji_bloqueado") or "🚫")
        emoji_new = str(cfg.get("emoji_new") or "🆕")
        emoji_atraso = str(cfg.get("emoji_atraso") or "⏰")
        today = dt.date.today()
        notif_parts: List[str] = []
        emoji_parts: List[str] = []
        blocked_count = int(d.get("blocked_count") or 0)
        if blocked_count > 0:
            notif_parts.append("Bloqueado")
            emoji_parts.append(emoji_bloqueado)
        if d.get("is_recent"):
            notif_parts.append("NEW")
            emoji_parts.append(emoji_new)
        estado = str(d.get("Estado") or "").strip()
        prazo_s = str(d.get("Prazo") or "")[:10]
        if prazo_s and not is_done_estado(estado):
            try:
                prazo_date = dt.date.fromisoformat(prazo_s)
                if prazo_date < today:
                    atraso_dias = (today - prazo_date).days
                    notif_parts.append(f"Atraso ({atraso_dias}d) {emoji_atraso}")
                    emoji_parts.append(emoji_atraso)
            except Exception:
                pass
        d["Notificacoes"] = "; ".join(notif_parts)
        d["NotifEmoji"] = " ".join(emoji_parts)
