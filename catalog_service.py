# -*- coding: utf-8 -*-
"""Catálogos partilhados — atalhos, contactos, índice de máquinas (CRUD + workflow)."""
from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any, Dict, List, Optional

from tasks_common import AppError, TasksDataAccess, can_edit_role, jval


MACHINE_WORKFLOW_FIELDS = (
    "manuals",
    "schematics",
    "spares",
    "folder",
    "notes",
    "loc_3d",
    "machine_plate",
    "conformity_decl",
)

MACHINE_FIELD_LABELS: Dict[str, str] = {
    "manuals": "Manuais",
    "schematics": "Esquemas",
    "spares": "Spares",
    "folder": "Pasta",
    "notes": "Notas",
    "loc_3d": "Localização 3D",
    "machine_plate": "Placa da máquina",
    "conformity_decl": "Declaração de conformidade",
}

FIELD_STATUS_DRAFT = "draft"
FIELD_STATUS_PENDING = "pending"
FIELD_STATUS_VALIDATED = "validated"


def _kind_for_target(target: str) -> str:
    t = str(target or "").strip()
    return "url" if t.lower().startswith(("http://", "https://", "mailto:")) else "path"


def _default_field_states() -> Dict[str, Dict[str, str]]:
    return {f: {"status": FIELD_STATUS_DRAFT, "updated_at": "", "updated_by": ""} for f in MACHINE_WORKFLOW_FIELDS}


def _parse_field_states(raw: Any) -> Dict[str, Dict[str, str]]:
    out = _default_field_states()
    if not raw:
        return out
    try:
        data = json.loads(str(raw)) if isinstance(raw, str) else raw
    except Exception:
        return out
    if not isinstance(data, dict):
        return out
    for field in MACHINE_WORKFLOW_FIELDS:
        st = data.get(field)
        if not isinstance(st, dict):
            continue
        status = str(st.get("status") or FIELD_STATUS_DRAFT).strip().lower()
        if status not in (FIELD_STATUS_DRAFT, FIELD_STATUS_PENDING, FIELD_STATUS_VALIDATED):
            status = FIELD_STATUS_DRAFT
        out[field] = {
            "status": status,
            "updated_at": str(st.get("updated_at") or ""),
            "updated_by": str(st.get("updated_by") or ""),
        }
    return out


def _compute_machine_status(states: Dict[str, Dict[str, str]]) -> str:
    if all(str((states.get(f) or {}).get("status") or "") == FIELD_STATUS_VALIDATED for f in MACHINE_WORKFLOW_FIELDS):
        return "validado"
    return "em_validacao"


def _is_admin(role: str) -> bool:
    return str(role or "").strip().lower() == "admin"


def _norm_person(s: str) -> str:
    return str(s or "").strip().lower()


def _can_submit_machine(username: str, display: str, responsible: str, role: str) -> bool:
    if _is_admin(role):
        return True
    resp = _norm_person(responsible)
    if not resp:
        return False
    return _norm_person(display) == resp or _norm_person(username) == resp


class CatalogService:
    def __init__(self, da: TasksDataAccess):
        self.da = da

    def _require_edit(self, role: str) -> None:
        if not can_edit_role(role):
            raise PermissionError("Sem permissão para editar catálogos")

    def _require_admin(self, role: str) -> None:
        if not _is_admin(role):
            raise PermissionError("Apenas admin")

    def ensure_machines_schema(self, conn) -> None:
        cur = conn.cursor()
        migrations = [
            ("responsible", "NVARCHAR(255) NOT NULL DEFAULT N''"),
            ("loc_3d", "NVARCHAR(MAX) NOT NULL DEFAULT N''"),
            ("machine_plate", "NVARCHAR(MAX) NOT NULL DEFAULT N''"),
            ("conformity_decl", "NVARCHAR(MAX) NOT NULL DEFAULT N''"),
            ("field_states", "NVARCHAR(MAX) NOT NULL DEFAULT N'{}'"),
        ]
        for col, typedef in migrations:
            try:
                cur.execute(
                    f"""
                    IF COL_LENGTH('dbo.machines_index','{col}') IS NULL
                        ALTER TABLE dbo.machines_index ADD {col} {typedef};
                    """
                )
            except Exception:
                try:
                    cur.execute(f"ALTER TABLE machines_index ADD COLUMN {col} TEXT DEFAULT '';")
                except Exception:
                    pass
        try:
            conn.commit()
        except Exception:
            pass

    def list_shortcuts(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            with self.da.connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT shortcut_id, label, target, description, favorite "
                    "FROM dbo.shortcuts ORDER BY favorite DESC, label;"
                )
                for sid, label, target, desc, fav in cur.fetchall():
                    t = str(target or "").strip()
                    out.append({
                        "shortcut_id": str(sid or ""),
                        "label": str(label or ""),
                        "target": t,
                        "description": str(desc or ""),
                        "favorite": bool(fav),
                        "kind": _kind_for_target(t),
                    })
        except Exception:
            pass
        return out

    def insert_shortcut(self, values: Dict[str, Any], role: str) -> str:
        self._require_edit(role)
        label = str(values.get("label") or "").strip()
        target = str(values.get("target") or "").strip()
        if not label and not target:
            raise AppError("Nome ou destino é obrigatório")
        sid = str(values.get("shortcut_id") or f"shortcut_{uuid.uuid4().hex[:10]}").strip()
        desc = str(values.get("description") or "").strip()
        fav = 1 if values.get("favorite") in (1, True, "1", "true", "True", "on") else 0
        with self.da.lock, self.da.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM dbo.shortcuts WHERE shortcut_id=?;", (sid,))
            if cur.fetchone():
                raise AppError(f"Atalho {sid} já existe")
            cur.execute(
                "INSERT INTO dbo.shortcuts(shortcut_id, label, target, description, favorite) VALUES(?,?,?,?,?);",
                (sid, label, target, desc, fav),
            )
            conn.commit()
        return sid

    def update_shortcut(self, shortcut_id: str, values: Dict[str, Any], role: str) -> None:
        self._require_edit(role)
        sid = str(shortcut_id or "").strip()
        if not sid:
            raise AppError("ID em falta")
        label = str(values.get("label") or "").strip()
        target = str(values.get("target") or "").strip()
        if not label and not target:
            raise AppError("Nome ou destino é obrigatório")
        desc = str(values.get("description") or "").strip()
        fav = 1 if values.get("favorite") in (1, True, "1", "true", "True", "on") else 0
        with self.da.lock, self.da.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE dbo.shortcuts SET label=?, target=?, description=?, favorite=? WHERE shortcut_id=?;",
                (label, target, desc, fav, sid),
            )
            if int(cur.rowcount or 0) == 0:
                raise AppError("Atalho não encontrado")
            conn.commit()

    def delete_shortcut(self, shortcut_id: str, role: str) -> None:
        self._require_edit(role)
        sid = str(shortcut_id or "").strip()
        with self.da.lock, self.da.connect() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM dbo.shortcuts WHERE shortcut_id=?;", (sid,))
            if int(cur.rowcount or 0) == 0:
                raise AppError("Atalho não encontrado")
            conn.commit()

    def list_contacts(self) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {"internal": [], "external": []}
        try:
            with self.da.connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT id, group_key, name, role, company, phone, email, notes "
                    "FROM dbo.contacts ORDER BY name;"
                )
                for cid, gk, name, role, company, phone, email, notes in cur.fetchall():
                    k = str(gk or "").strip().lower()
                    if k not in out:
                        continue
                    out[k].append({
                        "id": int(cid),
                        "group_key": k,
                        "name": str(name or ""),
                        "role": str(role or ""),
                        "company": str(company or ""),
                        "phone": str(phone or ""),
                        "email": str(email or ""),
                        "notes": str(notes or ""),
                    })
        except Exception:
            pass
        return out

    def insert_contact(self, values: Dict[str, Any], role: str) -> int:
        self._require_edit(role)
        gk = str(values.get("group_key") or values.get("group") or "internal").strip().lower()
        if gk not in ("internal", "external"):
            raise AppError("Grupo inválido (internal/external)")
        name = str(values.get("name") or "").strip()
        if not name:
            raise AppError("Nome é obrigatório")
        with self.da.lock, self.da.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO dbo.contacts(group_key, name, role, company, phone, email, notes) "
                "OUTPUT INSERTED.id VALUES (?,?,?,?,?,?,?);",
                (
                    gk, name,
                    str(values.get("role") or "").strip(),
                    str(values.get("company") or "").strip(),
                    str(values.get("phone") or "").strip(),
                    str(values.get("email") or "").strip(),
                    str(values.get("notes") or "").strip(),
                ),
            )
            new_id = int(cur.fetchone()[0])
            conn.commit()
            return new_id

    def update_contact(self, contact_id: int, values: Dict[str, Any], role: str) -> None:
        self._require_edit(role)
        cid = int(contact_id)
        name = str(values.get("name") or "").strip()
        if not name:
            raise AppError("Nome é obrigatório")
        gk = str(values.get("group_key") or values.get("group") or "").strip().lower()
        with self.da.lock, self.da.connect() as conn:
            cur = conn.cursor()
            if gk in ("internal", "external"):
                cur.execute(
                    "UPDATE dbo.contacts SET group_key=?, name=?, role=?, company=?, phone=?, email=?, notes=? WHERE id=?;",
                    (
                        gk, name,
                        str(values.get("role") or "").strip(),
                        str(values.get("company") or "").strip(),
                        str(values.get("phone") or "").strip(),
                        str(values.get("email") or "").strip(),
                        str(values.get("notes") or "").strip(),
                        cid,
                    ),
                )
            else:
                cur.execute(
                    "UPDATE dbo.contacts SET name=?, role=?, company=?, phone=?, email=?, notes=? WHERE id=?;",
                    (
                        name,
                        str(values.get("role") or "").strip(),
                        str(values.get("company") or "").strip(),
                        str(values.get("phone") or "").strip(),
                        str(values.get("email") or "").strip(),
                        str(values.get("notes") or "").strip(),
                        cid,
                    ),
                )
            if int(cur.rowcount or 0) == 0:
                raise AppError("Contacto não encontrado")
            conn.commit()

    def delete_contact(self, contact_id: int, role: str) -> None:
        self._require_edit(role)
        with self.da.lock, self.da.connect() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM dbo.contacts WHERE id=?;", (int(contact_id),))
            if int(cur.rowcount or 0) == 0:
                raise AppError("Contacto não encontrado")
            conn.commit()

    def _machine_row_to_dict(self, d: Dict[str, Any]) -> Dict[str, Any]:
        states = _parse_field_states(d.get("field_states"))
        return {
            "machine_id": str(d.get("machine_id") or ""),
            "name": str(d.get("name") or ""),
            "code": str(d.get("code") or ""),
            "area": str(d.get("area") or ""),
            "location": str(d.get("location") or ""),
            "manuals": str(d.get("manuals") or ""),
            "schematics": str(d.get("schematics") or ""),
            "spares": str(d.get("spares") or ""),
            "maintenance": str(d.get("maintenance") or ""),
            "folder": str(d.get("folder") or ""),
            "notes": str(d.get("notes") or ""),
            "responsible": str(d.get("responsible") or ""),
            "loc_3d": str(d.get("loc_3d") or ""),
            "machine_plate": str(d.get("machine_plate") or ""),
            "conformity_decl": str(d.get("conformity_decl") or ""),
            "field_states": states,
            "machine_status": _compute_machine_status(states),
            "workflow_fields": list(MACHINE_WORKFLOW_FIELDS),
            "field_labels": dict(MACHINE_FIELD_LABELS),
        }

    @staticmethod
    def _machine_dict(values: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "machine_id": str(values.get("machine_id") or ""),
            "name": str(values.get("name") or ""),
            "code": str(values.get("code") or ""),
            "area": str(values.get("area") or ""),
            "location": str(values.get("location") or ""),
            "manuals": str(values.get("manuals") or ""),
            "schematics": str(values.get("schematics") or ""),
            "spares": str(values.get("spares") or ""),
            "maintenance": str(values.get("maintenance") or ""),
            "folder": str(values.get("folder") or ""),
            "notes": str(values.get("notes") or ""),
            "responsible": str(values.get("responsible") or ""),
            "loc_3d": str(values.get("loc_3d") or ""),
            "machine_plate": str(values.get("machine_plate") or ""),
            "conformity_decl": str(values.get("conformity_decl") or ""),
        }

    def _fetch_machine_raw(self, conn, machine_id: str) -> Optional[Dict[str, Any]]:
        mid = str(machine_id or "").strip()
        if not mid:
            return None
        cur = conn.cursor()
        sql_full = (
            "SELECT machine_id, name, code, area, location, manuals, schematics, spares, maintenance, "
            "folder, notes, responsible, loc_3d, machine_plate, conformity_decl, field_states "
            "FROM dbo.machines_index WHERE machine_id=?;"
        )
        sql_legacy = (
            "SELECT machine_id, name, code, area, location, manuals, schematics, spares, maintenance, "
            "folder, notes FROM dbo.machines_index WHERE machine_id=?;"
        )
        row = None
        cols: List[str] = []
        for sql in (sql_full, sql_legacy):
            try:
                cur.execute(sql, (mid,))
                row = cur.fetchone()
                if row is None:
                    return None
                cols = [d[0] for d in cur.description]
                break
            except Exception:
                row = None
                cols = []
        if row is None:
            return None
        d = {k: jval(v) for k, v in zip(cols, row)}
        if "responsible" not in d:
            d["responsible"] = ""
        if "loc_3d" not in d:
            d["loc_3d"] = ""
        if "machine_plate" not in d:
            d["machine_plate"] = ""
        if "conformity_decl" not in d:
            d["conformity_decl"] = ""
        if "field_states" not in d:
            d["field_states"] = "{}"
        return d

    def list_machines(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            with self.da.connect() as conn:
                self.ensure_machines_schema(conn)
                cur = conn.cursor()
                sql_full = (
                    "SELECT machine_id, name, code, area, location, manuals, schematics, spares, maintenance, "
                    "folder, notes, responsible, loc_3d, machine_plate, conformity_decl, field_states "
                    "FROM dbo.machines_index ORDER BY name;"
                )
                sql_legacy = (
                    "SELECT machine_id, name, code, area, location, manuals, schematics, spares, maintenance, "
                    "folder, notes FROM dbo.machines_index ORDER BY name;"
                )
                rows = None
                cols: List[str] = []
                for sql in (sql_full, sql_legacy):
                    try:
                        cur.execute(sql)
                        rows = cur.fetchall() or []
                        cols = [d[0] for d in cur.description]
                        break
                    except Exception:
                        rows = None
                if rows is None:
                    return out
                for row in rows:
                    d = {k: jval(v) for k, v in zip(cols, row)}
                    if "responsible" not in d:
                        d["responsible"] = ""
                    if "loc_3d" not in d:
                        d["loc_3d"] = ""
                    if "machine_plate" not in d:
                        d["machine_plate"] = ""
                    if "conformity_decl" not in d:
                        d["conformity_decl"] = ""
                    if "field_states" not in d:
                        d["field_states"] = "{}"
                    out.append(self._machine_row_to_dict(d))
        except Exception:
            pass
        return out

    def insert_machine(self, values: Dict[str, Any], role: str, username: str = "", display: str = "") -> str:
        del username, display
        self._require_edit(role)
        v = self._machine_dict(values)
        if not v["name"]:
            raise AppError("Nome da máquina é obrigatório")
        mid = str(values.get("machine_id") or v["code"] or f"machine_{uuid.uuid4().hex[:10]}").strip()
        v["machine_id"] = mid
        states = _default_field_states()
        states_json = json.dumps(states, ensure_ascii=False)
        with self.da.lock, self.da.connect() as conn:
            self.ensure_machines_schema(conn)
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM dbo.machines_index WHERE machine_id=?;", (mid,))
            if cur.fetchone():
                raise AppError(f"Máquina {mid} já existe")
            try:
                cur.execute(
                    "INSERT INTO dbo.machines_index("
                    "machine_id,name,code,area,location,manuals,schematics,spares,maintenance,folder,notes,"
                    "responsible,loc_3d,machine_plate,conformity_decl,field_states"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);",
                    (
                        v["machine_id"], v["name"], v["code"], v["area"], v["location"],
                        v["manuals"], v["schematics"], v["spares"], v["maintenance"], v["folder"], v["notes"],
                        v["responsible"], v["loc_3d"], v["machine_plate"], v["conformity_decl"], states_json,
                    ),
                )
            except Exception:
                cur.execute(
                    "INSERT INTO dbo.machines_index(machine_id,name,code,area,location,manuals,schematics,spares,maintenance,folder,notes) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?);",
                    (
                        v["machine_id"], v["name"], v["code"], v["area"], v["location"],
                        v["manuals"], v["schematics"], v["spares"], v["maintenance"], v["folder"], v["notes"],
                    ),
                )
            conn.commit()
        return mid

    def update_machine(
        self,
        machine_id: str,
        values: Dict[str, Any],
        role: str,
        username: str = "",
        display: str = "",
    ) -> Dict[str, Any]:
        del username, display
        self._require_edit(role)
        mid = str(machine_id or "").strip()
        incoming = self._machine_dict(values)
        if not incoming["name"]:
            raise AppError("Nome da máquina é obrigatório")
        with self.da.lock, self.da.connect() as conn:
            self.ensure_machines_schema(conn)
            existing = self._fetch_machine_raw(conn, mid)
            if not existing:
                raise AppError("Máquina não encontrada")
            states = _parse_field_states(existing.get("field_states"))
            current = self._machine_dict(existing)
            admin = _is_admin(role)

            current["name"] = incoming["name"]
            current["code"] = incoming["code"]
            current["area"] = incoming["area"]
            current["location"] = incoming["location"]
            current["responsible"] = incoming["responsible"]

            for field in MACHINE_WORKFLOW_FIELDS:
                if field not in values:
                    continue
                new_val = str(values.get(field) or "").strip()
                status = str((states.get(field) or {}).get("status") or FIELD_STATUS_DRAFT)
                if status == FIELD_STATUS_VALIDATED and not admin:
                    raise PermissionError(f"Campo {MACHINE_FIELD_LABELS.get(field, field)} validado — apenas admin pode alterar")
                if status == FIELD_STATUS_PENDING and not admin:
                    raise AppError(f"Campo {MACHINE_FIELD_LABELS.get(field, field)} pendente — aguarde aprovação ou peça ao admin para reverter")
                current[field] = new_val

            states_json = json.dumps(states, ensure_ascii=False)
            cur = conn.cursor()
            try:
                cur.execute(
                    "UPDATE dbo.machines_index SET name=?, code=?, area=?, location=?, manuals=?, schematics=?, "
                    "spares=?, maintenance=?, folder=?, notes=?, responsible=?, loc_3d=?, machine_plate=?, "
                    "conformity_decl=?, field_states=? WHERE machine_id=?;",
                    (
                        current["name"], current["code"], current["area"], current["location"],
                        current["manuals"], current["schematics"], current["spares"], current["maintenance"],
                        current["folder"], current["notes"], current["responsible"], current["loc_3d"],
                        current["machine_plate"], current["conformity_decl"], states_json, mid,
                    ),
                )
            except Exception:
                cur.execute(
                    "UPDATE dbo.machines_index SET name=?, code=?, area=?, location=?, manuals=?, schematics=?, "
                    "spares=?, maintenance=?, folder=?, notes=? WHERE machine_id=?;",
                    (
                        current["name"], current["code"], current["area"], current["location"],
                        current["manuals"], current["schematics"], current["spares"], current["maintenance"],
                        current["folder"], current["notes"], mid,
                    ),
                )
            if int(cur.rowcount or 0) == 0:
                raise AppError("Máquina não encontrada")
            conn.commit()
            saved = self._fetch_machine_raw(conn, mid) or current
            return self._machine_row_to_dict(saved)

    def submit_machine_field(
        self,
        machine_id: str,
        field: str,
        role: str,
        username: str = "",
        display: str = "",
    ) -> Dict[str, Any]:
        self._require_edit(role)
        mid = str(machine_id or "").strip()
        fld = str(field or "").strip()
        if fld not in MACHINE_WORKFLOW_FIELDS:
            raise AppError("Campo inválido")
        with self.da.lock, self.da.connect() as conn:
            self.ensure_machines_schema(conn)
            existing = self._fetch_machine_raw(conn, mid)
            if not existing:
                raise AppError("Máquina não encontrada")
            row = self._machine_row_to_dict(existing)
            if not _can_submit_machine(username, display, row.get("responsible") or "", role):
                raise PermissionError("Apenas o responsável da máquina ou admin pode submeter")
            val = str(existing.get(fld) or "").strip()
            if not val:
                raise AppError(f"Preencha {MACHINE_FIELD_LABELS.get(fld, fld)} (ou N/A) antes de submeter")
            states = _parse_field_states(existing.get("field_states"))
            st = states.get(fld) or {}
            status = str(st.get("status") or FIELD_STATUS_DRAFT)
            if status == FIELD_STATUS_VALIDATED:
                raise AppError("Campo já validado")
            if status == FIELD_STATUS_PENDING:
                raise AppError("Campo já está pendente de aprovação")
            now = dt.datetime.now().isoformat(timespec="seconds")
            states[fld] = {
                "status": FIELD_STATUS_PENDING,
                "updated_at": now,
                "updated_by": str(display or username or "").strip(),
            }
            states_json = json.dumps(states, ensure_ascii=False)
            cur = conn.cursor()
            cur.execute("UPDATE dbo.machines_index SET field_states=? WHERE machine_id=?;", (states_json, mid))
            conn.commit()
            saved = self._fetch_machine_raw(conn, mid) or existing
            return self._machine_row_to_dict(saved)

    def approve_machine_field(self, machine_id: str, field: str, role: str, username: str = "", display: str = "") -> Dict[str, Any]:
        del username
        self._require_admin(role)
        mid = str(machine_id or "").strip()
        fld = str(field or "").strip()
        if fld not in MACHINE_WORKFLOW_FIELDS:
            raise AppError("Campo inválido")
        with self.da.lock, self.da.connect() as conn:
            self.ensure_machines_schema(conn)
            existing = self._fetch_machine_raw(conn, mid)
            if not existing:
                raise AppError("Máquina não encontrada")
            val = str(existing.get(fld) or "").strip()
            if not val:
                raise AppError(f"Campo {MACHINE_FIELD_LABELS.get(fld, fld)} vazio — use N/A se não aplicável")
            states = _parse_field_states(existing.get("field_states"))
            st = states.get(fld) or {}
            if str(st.get("status") or "") != FIELD_STATUS_PENDING:
                raise AppError("Só é possível aprovar campos pendentes")
            now = dt.datetime.now().isoformat(timespec="seconds")
            states[fld] = {
                "status": FIELD_STATUS_VALIDATED,
                "updated_at": now,
                "updated_by": str(display or "admin").strip(),
            }
            states_json = json.dumps(states, ensure_ascii=False)
            cur = conn.cursor()
            cur.execute("UPDATE dbo.machines_index SET field_states=? WHERE machine_id=?;", (states_json, mid))
            conn.commit()
            saved = self._fetch_machine_raw(conn, mid) or existing
            return self._machine_row_to_dict(saved)

    def revert_machine_field(self, machine_id: str, field: str, role: str, username: str = "", display: str = "") -> Dict[str, Any]:
        del username
        self._require_admin(role)
        mid = str(machine_id or "").strip()
        fld = str(field or "").strip()
        if fld not in MACHINE_WORKFLOW_FIELDS:
            raise AppError("Campo inválido")
        with self.da.lock, self.da.connect() as conn:
            self.ensure_machines_schema(conn)
            existing = self._fetch_machine_raw(conn, mid)
            if not existing:
                raise AppError("Máquina não encontrada")
            states = _parse_field_states(existing.get("field_states"))
            now = dt.datetime.now().isoformat(timespec="seconds")
            states[fld] = {
                "status": FIELD_STATUS_DRAFT,
                "updated_at": now,
                "updated_by": str(display or "admin").strip(),
            }
            states_json = json.dumps(states, ensure_ascii=False)
            cur = conn.cursor()
            cur.execute("UPDATE dbo.machines_index SET field_states=? WHERE machine_id=?;", (states_json, mid))
            conn.commit()
            saved = self._fetch_machine_raw(conn, mid) or existing
            return self._machine_row_to_dict(saved)

    def delete_machine(self, machine_id: str, role: str) -> None:
        self._require_admin(role)
        mid = str(machine_id or "").strip()
        with self.da.lock, self.da.connect() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM dbo.machines_index WHERE machine_id=?;", (mid,))
            if int(cur.rowcount or 0) == 0:
                raise AppError("Máquina não encontrada")
            conn.commit()
