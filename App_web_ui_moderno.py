# -*- coding: utf-8 -*-
"""
App Web UI — ponto de entrada v0.16.4.

Loader sobre bytecode (_app_web_ui_base.cpython-313.pyc) + patches incrementais.
Serviços em ficheiros *_service.py separados (não é monólito autónomo).
Restaure a versão .py completa via histórico OneDrive quando possível.
"""
from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import traceback
import uuid
import time
import getpass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

APP_VERSION = "0.17.11"
_BASE_DIR = Path(__file__).resolve().parent
# Robustez de imports para execucao em diferentes PCs/OneDrive.
# O bytecode base pode importar modulos "soltos" (ex.: excel_filters.py).
for _p in (
    _BASE_DIR.parent,
    _BASE_DIR,
):
    try:
        _sp = str(_p.resolve())
    except Exception:
        _sp = str(_p)
    if _sp not in sys.path:
        sys.path.insert(0, _sp)
# Bytecode v0.11.0 (fonte original perdido). Não usar __pycache__/App_web_ui_moderno*.pyc
# porque o loader sobrescreve esse nome ao importar.
_PYC = _BASE_DIR / "_app_web_ui_base.cpython-313.pyc"
if not _PYC.is_file():
    _PYC = _BASE_DIR / "__pycache__" / "_recovered" / "App_web_ui_moderno.cpython-313.pyc"

_loading_base = False
_SESSIONS: dict[str, dict[str, Any]] = {}
_SESSION_COOKIE = "webui_session"
_SESSION_IDLE_SEC = 45 * 60


def _parse_multipart_first_file(content_type: str, body: bytes) -> tuple[str, bytes]:
    """
    Parser mínimo multipart/form-data sem módulo cgi (Python 3.13+).
    Retorna (filename, data) do primeiro part com ficheiro.
    """
    ct = str(content_type or "")
    m = re.search(r'boundary="?([^";]+)"?', ct, flags=re.I)
    if not m:
        raise ValueError("Boundary multipart em falta")
    boundary = m.group(1).encode("utf-8", errors="ignore")
    if not boundary:
        raise ValueError("Boundary multipart inválida")
    marker = b"--" + boundary
    parts = body.split(marker)
    for part in parts:
        if not part:
            continue
        chunk = part.strip()
        if not chunk or chunk == b"--":
            continue
        if b"\r\n\r\n" in chunk:
            raw_headers, raw_data = chunk.split(b"\r\n\r\n", 1)
        elif b"\n\n" in chunk:
            raw_headers, raw_data = chunk.split(b"\n\n", 1)
        else:
            continue
        headers_txt = raw_headers.decode("utf-8", errors="replace")
        if "content-disposition" not in headers_txt.lower():
            continue
        fm = re.search(r'filename\*?=(?:"([^"]*)"|([^;\r\n]+))', headers_txt, flags=re.I)
        if not fm:
            continue
        filename = (fm.group(1) or fm.group(2) or "").strip().strip('"').strip("'")
        if not filename:
            continue
        data = raw_data
        if data.endswith(b"\r\n"):
            data = data[:-2]
        elif data.endswith(b"\n"):
            data = data[:-1]
        return os.path.basename(filename), data
    raise ValueError("Nenhum ficheiro encontrado no multipart")


class _MultipartField:
    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self.file = io.BytesIO(data)


class _MultipartForm:
    """Substituto mínimo de cgi.FieldStorage para Python 3.13+."""

    def __init__(self, fields: list[_MultipartField]):
        self.list = fields
        if len(fields) == 1:
            self.filename = fields[0].filename
            self.file = fields[0].file


def _read_multipart_no_cgi(handler: Any) -> _MultipartForm:
    ct = str((getattr(handler, "headers", None) and handler.headers.get("Content-Type")) or "").strip()
    try:
        clen = int(str((getattr(handler, "headers", None) and handler.headers.get("Content-Length")) or "0"))
    except Exception:
        clen = 0
    if clen <= 0:
        return _MultipartForm([])
    body = handler.rfile.read(clen)
    filename, data = _parse_multipart_first_file(ct, body)
    return _MultipartForm([_MultipartField(filename, data)])


_WIN_FOLDER_PICKER_PS = ""  # legacy inline script replaced by pick_folder_win.ps1


def _runtime_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return _BASE_DIR


def _pick_folder_dialog_win_modern(title: str, initial: str, log_fn: Any = None) -> str:
    """Seletor nativo Windows 10/11 (IFileOpenDialog + pastas)."""
    from files_service import normalize_folder_path

    ps1 = _runtime_base_dir() / "pick_folder_win.ps1"
    if not ps1.is_file():
        if log_fn:
            log_fn("pick_folder_dialog(win): pick_folder_win.ps1 em falta")
        return ""
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-STA",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ps1),
                "-Title",
                str(title or "Selecionar pasta"),
                "-InitialPath",
                str(initial or ""),
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
        lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        chosen = normalize_folder_path(lines[-1] if lines else "")
        if chosen:
            return chosen
        err = (proc.stderr or "").strip()
        if err and log_fn:
            log_fn(f"pick_folder_dialog(win): {err}")
        elif proc.returncode and log_fn:
            log_fn(f"pick_folder_dialog(win): exit {proc.returncode}")
    except subprocess.TimeoutExpired:
        if log_fn:
            log_fn("pick_folder_dialog(win): timeout")
    except Exception as ex:
        if log_fn:
            log_fn(f"pick_folder_dialog(win): {ex}")
    return ""


def _pick_folder_dialog_win_legacy(title: str, initial: str, log_fn: Any = None) -> str:
    """Fallback: FolderBrowserDialog (UI antiga, mas funcional)."""
    from files_service import normalize_folder_path

    init_ps = str(initial or "").replace("'", "''")
    title_ps = str(title or "Selecionar pasta").replace("'", "''")
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
        f"$d.Description = '{title_ps}'; "
        "$d.ShowNewFolderButton = $true; "
    )
    if init_ps:
        ps += f"if (Test-Path -LiteralPath '{init_ps}') {{ $d.SelectedPath = '{init_ps}' }}; "
    ps += (
        "$r = $d.ShowDialog(); "
        "if ($r -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=600,
        )
        lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        chosen = normalize_folder_path(lines[-1] if lines else "")
        if chosen:
            return chosen
        if (proc.stderr or "").strip() and log_fn:
            log_fn(f"pick_folder_dialog(legacy): {(proc.stderr or '').strip()}")
    except Exception as ex:
        if log_fn:
            log_fn(f"pick_folder_dialog(legacy): {ex}")
    return ""


def _pick_folder_dialog_safe(title: str, initial: str = "", log_fn: Any = None) -> str:
    """
    Seletor de pasta fora da thread HTTP.
    Windows: IFileOpenDialog (UI moderna). Fallback: Tkinter em subprocess.
    """
    from files_service import normalize_folder_path

    init = normalize_folder_path(initial)
    title_s = str(title or "Selecionar pasta").strip() or "Selecionar pasta"

    def _log(msg: str) -> None:
        if not log_fn:
            return
        try:
            log_fn(str(msg))
        except Exception:
            pass

    if sys.platform != "win32":
        return ""

    chosen = _pick_folder_dialog_win_modern(title_s, init, log_fn=_log)
    if chosen:
        return chosen

    chosen = _pick_folder_dialog_win_legacy(title_s, init, log_fn=_log)
    if chosen:
        return chosen

    py = sys.executable
    if py.lower().endswith("python.exe"):
        pyw = py[:-10] + "pythonw.exe"
        if os.path.isfile(pyw):
            py = pyw
    script = (
        "import json, os, sys\n"
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "root = tk.Tk()\n"
        "root.withdraw()\n"
        "try:\n"
        "    root.attributes('-topmost', True)\n"
        "except Exception:\n"
        "    pass\n"
        "initial = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "title = sys.argv[2] if len(sys.argv) > 2 else 'Selecionar pasta'\n"
        "init = initial if initial and os.path.isdir(initial) else os.path.expanduser('~')\n"
        "try:\n"
        "    chosen = filedialog.askdirectory(title=title, initialdir=init, mustexist=True)\n"
        "except TypeError:\n"
        "    chosen = filedialog.askdirectory(title=title, initialdir=init)\n"
        "print(json.dumps(chosen or ''))\n"
        "try:\n"
        "    root.destroy()\n"
        "except Exception:\n"
        "    pass\n"
    )
    try:
        proc = subprocess.run(
            [py, "-c", script, init, title_s],
            capture_output=True,
            text=True,
            timeout=600,
        )
        out = (proc.stdout or "").strip()
        if out:
            data = json.loads(out)
            chosen = normalize_folder_path(str(data or ""))
            if chosen:
                return chosen
    except Exception as ex:
        _log(f"pick_folder_dialog(py): {ex}")
    return ""


def _patch_pick_folder_dialog(base_mod: Any) -> None:
    _orig = getattr(base_mod, "pick_folder_dialog", None)
    log_fn = getattr(base_mod, "log", None)

    def pick_folder_dialog(title: str, initial: str = "") -> str:  # noqa: N802
        chosen = _pick_folder_dialog_safe(title, initial, log_fn=log_fn)
        if chosen:
            return chosen
        if sys.platform != "win32" and callable(_orig):
            try:
                return str(_orig(title, initial) or "")
            except Exception as ex:
                if log_fn:
                    log_fn(f"pick_folder_dialog: {ex}")
        return ""

    base_mod.pick_folder_dialog = pick_folder_dialog  # type: ignore[attr-defined]


_SCHED_SAVE_OLD = (
    "async async function saveSchedModal(){try{if(!canEditTasks())return;let acts=[];"
    "try{acts=JSON.parse($('sm_actions')?.value||'[]')}catch(e){toast('JSON de acoes invalido',true);return}"
    "const rec=$('sm_rec').value;const body={name:$('sm_name').value.trim(),recurrence:rec,"
    "interval_n:parseInt($('sm_int').value||'1',10),next_run_date:$('sm_next').value,create_mode:$('sm_mode').value,"
    "visibility:$('sm_vis').value,lead_days:parseInt($('sm_lead').value||'0',10),"
    "grace_days:parseInt($('sm_grace').value||'30',10),is_active:$('sm_active').value==='1',"
    "weekday_mask:rec==='weekly'?schedWeekdayMask():null,"
    "day_of_month:(rec==='monthly'||rec==='yearly')?parseInt($('sm_dom').value||'0',10)||null:null,"
    "month_of_year:rec==='yearly'?parseInt($('sm_moy').value||'0',10)||null:null,"
    "once_date:rec==='once'?($('sm_once').value||$('sm_next').value):null,"
    "generate_default_actions:!!$('sm_gda')?.checked,action_defaults:acts,"
    "task_defaults:{Projeto:$('sm_projeto').value.trim(),Responsavel:$('sm_resp').value.trim(),"
    "Assunto:$('sm_assunto').value.trim(),Milestone:$('sm_milestone')?.value.trim()||'',"
    "Prioridade:$('sm_prio')?.value||'Média',Private:$('sm_private')?.checked?1:0}};"
    "if(!body.name){toast('Nome obrigatorio',true);return}"
    "if(_schedEditId){await api('/api/scheduled/'+_schedEditId+'/update',{method:'POST',body:JSON.stringify(body)});"
    "toast('Guardado')}else{let j=await api('/api/scheduled/templates',{method:'POST',body:JSON.stringify(body)});"
    "toast('Criada #'+j.id)}closeSchedModal();loadScheduled()}catch(e){toast(e.message,true)}}"
)

_SCHED_HELPERS = (
    "function schedCollectBody(){const rec=$('sm_rec').value;return{name:$('sm_name').value.trim(),"
    "recurrence:rec,interval_n:parseInt($('sm_int').value||'1',10),next_run_date:$('sm_next').value,"
    "create_mode:$('sm_mode').value,visibility:$('sm_vis').value,"
    "lead_days:parseInt($('sm_lead').value||'0',10),grace_days:parseInt($('sm_grace').value||'30',10),"
    "is_active:$('sm_active').value==='1',weekday_mask:rec==='weekly'?schedWeekdayMask():null,"
    "day_of_month:(rec==='monthly'||rec==='yearly')?parseInt($('sm_dom').value||'0',10)||null:null,"
    "month_of_year:rec==='yearly'?parseInt($('sm_moy').value||'0',10)||null:null,"
    "once_date:rec==='once'?($('sm_once').value||$('sm_next').value):null,"
    "generate_default_actions:!!$('sm_gda')?.checked,"
    "task_defaults:{Projeto:$('sm_projeto').value.trim(),Responsavel:$('sm_resp').value.trim(),"
    "Assunto:$('sm_assunto').value.trim(),Milestone:$('sm_milestone')?.value.trim()||'',"
    "Prioridade:$('sm_prio')?.value||'Média',Private:$('sm_private')?.checked?1:0}}}"
    "function schedDefaultAction(){return{text:'Revisão',due_offset_days:0,owner:''}}"
    "function schedReadActionsForm(){const rows=[...($('sm_actions_rows')?.querySelectorAll('tr')||[])];"
    "return rows.map(tr=>{const text=String(tr.querySelector('.sm-act-text')?.value||'').trim();"
    "const owner=String(tr.querySelector('.sm-act-owner')?.value||'').trim();"
    "const off=parseInt(tr.querySelector('.sm-act-off')?.value||'0',10)||0;"
    "const o={text,due_offset_days:off};if(owner)o.owner=owner;return o}).filter(a=>a.text)}"
    "function schedSyncActionsJson(){const ta=$('sm_actions');if(ta){try{ta.value=JSON.stringify(schedReadActionsForm(),null,0)}catch(_){}}"
    "schedValidateActionsJson()}"
    "function schedAppendActionRow(a){const tb=$('sm_actions_rows');if(!tb)return;"
    "const tr=document.createElement('tr');"
    "tr.innerHTML='<td><input class=\"sm-act-text\" type=\"text\" placeholder=\"Texto da ação\" style=\"width:100%\"></td>'"
    "+'<td><input class=\"sm-act-owner\" type=\"text\" placeholder=\"Owner\" style=\"width:100%\"></td>'"
    "+'<td><input class=\"sm-act-off\" type=\"number\" value=\"0\" style=\"width:72px\"></td>'"
    "+'<td><button type=\"button\" class=\"btn\" onclick=\"schedRemoveActionRow(this)\">✕</button></td>';"
    "tr.querySelector('.sm-act-text').value=String(a?.text||a?.item_text||'');"
    "tr.querySelector('.sm-act-owner').value=String(a?.owner||'');"
    "tr.querySelector('.sm-act-off').value=String(Number(a?.due_offset_days||0));"
    "tr.querySelectorAll('input').forEach(inp=>inp.addEventListener('input',schedSyncActionsJson));"
    "tb.appendChild(tr)}"
    "function schedLoadActionsForm(acts){const tb=$('sm_actions_rows');if(!tb)return;"
    "const list=Array.isArray(acts)&&acts.length?acts:[schedDefaultAction()];tb.innerHTML='';"
    "list.forEach(a=>schedAppendActionRow(a));schedSyncActionsJson()}"
    "function schedAddActionRow(){schedAppendActionRow(schedDefaultAction());schedSyncActionsJson()}"
    "function schedRemoveActionRow(btn){btn?.closest('tr')?.remove();"
    "if(!$('sm_actions_rows')?.children?.length)schedAddActionRow();schedSyncActionsJson()}"
    "function schedParseActions(){if($('sm_actions_rows')){const fromForm=schedReadActionsForm();"
    "if($('sm_gda')?.checked||fromForm.length){for(let i=0;i<fromForm.length;i++){"
    "if(!String(fromForm[i].text||'').trim())throw new Error('Ação #'+(i+1)+': falta o texto')}"
    "return fromForm}return[]}"
    "const raw=($('sm_actions')?.value||'').trim();if(!raw)return[];"
    "let data;try{data=JSON.parse(raw)}catch(e){throw new Error('JSON inválido — '+e.message)}"
    "if(!Array.isArray(data))throw new Error('Ações default: tem de ser um array');"
    "for(let i=0;i<data.length;i++){const it=data[i];"
    "if(!it||typeof it!=='object')throw new Error('Ação #'+(i+1)+': cada entrada tem de ser um objeto');"
    "if(!String(it.text||it.item_text||'').trim())throw new Error('Ação #'+(i+1)+': falta o campo \"text\"')}"
    "return data}"
    "function schedValidateActionsJson(){const el=$('sm_actions_err'),ta=$('sm_actions');if(!el)return true;"
    "if(!$('sm_gda')?.checked){el.style.display='none';el.textContent='';ta?.classList.remove('invalid');return true}"
    "try{schedParseActions();el.style.display='none';el.textContent='';ta?.classList.remove('invalid');return true}"
    "catch(e){el.textContent=e.message;el.style.display='block';ta?.classList.add('invalid');return false}}"
    "async function schedPreview(){try{const el=$('sm_preview');if(!el)return;el.textContent='A calcular...';"
    "let body=schedCollectBody();"
    "if($('sm_gda')?.checked){"
    "if(!schedValidateActionsJson()){el.innerHTML='<span style=\"color:var(--danger)\">Corrija as ações default.</span>';return}"
    "body.action_defaults=schedParseActions()}"
    "body.count=12;const j=await api('/api/scheduled/preview',{method:'POST',body:JSON.stringify(body)});"
    "if(j.errors&&j.errors.length){el.innerHTML='<span style=\"color:var(--danger)\">'+esc(j.errors.join(' | '))+'</span>';return}"
    "if(!j.occurrences||!j.occurrences.length){el.textContent='Sem ocorrências calculadas.';return}"
    "el.innerHTML='<table style=\"width:100%;font-size:12px;border-collapse:collapse\">"
    "<thead><tr><th style=\"text-align:left;padding:2px 6px\">Data</th>"
    "<th style=\"text-align:left;padding:2px 6px\">Janela</th></tr></thead><tbody>'"
    "+j.occurrences.map(o=>'<tr><td style=\"padding:2px 6px\">'+esc(o.date)+'</td>"
    "<td style=\"padding:2px 6px\">'+esc(o.window_label||o.window)+'</td></tr>').join('')+'</tbody></table>'"
    "}catch(e){$('sm_preview').innerHTML='<span style=\"color:var(--danger)\">'+esc(e.message)+'</span>'}}"
    "async function saveSchedModal(){try{if(!canEditTasks())return;"
    "if(!schedValidateActionsJson()){toast($('sm_actions_err')?.textContent||'Ações default inválidas',true);return}"
    "let body=schedCollectBody();body.action_defaults=schedParseActions();"
    "if(!body.name){toast('Nome obrigatório',true);return}"
    "if(_schedEditId){await api('/api/scheduled/'+_schedEditId+'/update',{method:'POST',body:JSON.stringify(body)});"
    "toast('Guardado')}else{let j=await api('/api/scheduled/templates',{method:'POST',body:JSON.stringify(body)});"
    "toast('Criada #'+j.id)}unsavedClear();closeSchedModal();loadScheduled()}catch(e){toast(e.message,true)}}"
)

_SCHED_FILTERS_HTML = (
    '<section class="card filters" id="sched-filters" style="margin-bottom:12px">'
    '<b>🔎 Filtros</b>'
    '<div class="grid" style="margin-top:12px">'
    '<div class="field"><label>Pesquisar</label><input id="sch_f_q" placeholder="Nome, estado, tarefa..."></div>'
    '<label style="padding-top:30px"><input type="checkbox" id="sch_f_active" checked> Só activas</label>'
    '<label style="padding-top:30px"><input type="checkbox" id="sch_f_pending"> Só pendentes</label>'
    '<label style="padding-top:30px"><input type="checkbox" id="sch_f_failed"> Com falha</label>'
    '<div class="field"><label>Recorrência</label>'
    '<select id="sch_f_rec"><option value="Todos">Todos</option><option value="Semanal">Semanal</option>'
    '<option value="Mensal">Mensal</option><option value="Anual">Anual</option><option value="Uma vez">Uma vez</option>'
    "</select></div>"
    '<div class="field"><label>Modo</label>'
    '<select id="sch_f_mode"><option value="Todos">Todos</option><option value="Manual">Manual</option>'
    '<option value="Automática">Automática</option></select></div>'
    '<div class="field" style="align-self:end"><button class="btn" type="button" onclick="schedClearFilters()">Limpar filtros</button></div>'
    "</div></section>"
)

_SCHED_TOOLBAR_HTML = (
    '<div class="toolbar" id="sched-toolbar">'
    '<button class="btn primary" id="sch_new" onclick="schedNew()">＋ Nova</button>'
    '<button class="btn" id="sch_edit" onclick="schedEdit()" disabled>Editar</button>'
    '<button class="btn" id="sch_gen" onclick="schedGenerate()" disabled title="Força a criação da tarefa do ciclo actual">Criar tarefa deste ciclo</button>'
    '<button class="btn" id="sch_mat" onclick="schedMaterialize()" disabled title="Confirma um ciclo marcado como pendente (modo manual)">Confirmar pendente</button>'
    '<button class="btn" id="sch_toggle" onclick="schedToggleSel()" disabled>Ativar/Desativar</button>'
    '<button class="btn" id="sch_open_task" onclick="schedOpenTask()" disabled>Abrir tarefa</button>'
    "</div>"
    '<div id="sch_sel_detail" class="card sched-detail" style="display:none;padding:10px 14px;margin-bottom:8px">'
    '<div class="muted" style="font-size:11px;margin-bottom:4px">Estado completo</div>'
    '<div id="sch_sel_detail_text" style="font-size:13px;line-height:1.45"></div>'
    "</div>"
)

_SCHED_FILTERS_JS = (
    "let _schedRowsAll=[];let _schedFiltersLoaded=false;let _schedFiltersSaveTimer=null;"
    "function schedReadFilters(){return{"
    "q:($('sch_f_q')?.value||'').trim(),"
    "pending:!!$('sch_f_pending')?.checked,"
    "active:!!$('sch_f_active')?.checked,"
    "failed:!!$('sch_f_failed')?.checked,"
    "rec:$('sch_f_rec')?.value||'Todos',"
    "mode:$('sch_f_mode')?.value||'Todos'}}"
    "function schedApplyDefaultFilters(){"
    "if($('sch_f_active'))$('sch_f_active').checked=true;"
    "if($('sch_f_pending'))$('sch_f_pending').checked=false;"
    "if($('sch_f_failed'))$('sch_f_failed').checked=false}"
    "function schedApplyFiltersObj(o){if(!o||typeof o!=='object')return;"
    "if($('sch_f_q'))$('sch_f_q').value=o.q||'';"
    "if($('sch_f_pending'))$('sch_f_pending').checked=!!o.pending;"
    "if($('sch_f_active'))$('sch_f_active').checked=!!o.active;"
    "if($('sch_f_failed'))$('sch_f_failed').checked=!!o.failed;"
    "if($('sch_f_rec'))$('sch_f_rec').value=o.rec||'Todos';"
    "if($('sch_f_mode'))$('sch_f_mode').value=o.mode||'Todos'}"
    "function schedSaveFilters(){try{clearTimeout(_schedFiltersSaveTimer);"
    "_schedFiltersSaveTimer=setTimeout(async()=>{try{await api('/api/scheduled/prefs',{method:'POST',body:JSON.stringify(schedReadFilters())})}catch(_){}},280)}catch(_){}}"
    "async function schedRestoreFilters(){try{"
    "let j=await api('/api/scheduled/prefs');const o=(j&&j.prefs)||{};"
    "if(o&&typeof o==='object'&&Object.keys(o).length){schedApplyFiltersObj(o);_schedFiltersLoaded=true;return}"
    "schedApplyDefaultFilters();_schedFiltersLoaded=true}catch(_){schedApplyDefaultFilters();_schedFiltersLoaded=true}}"
    "function schedRecMatches(r,rec){if(rec==='Todos')return true;"
    "return String(r.recurrence_human||'').trim().startsWith(rec)}"
    "function schedApplyFilters(rows){const f=schedReadFilters();const q=f.q.toLowerCase();"
    "return (rows||[]).filter(r=>{"
    "if(f.pending&&String(r.pending_human||'')!=='Sim')return false;"
    "if(f.active&&(r.is_active===false||r.is_active===0))return false;"
    "if(f.failed&&!r.is_failed&&!String(r.state_human||'').startsWith('Falhou'))return false;"
    "if(!schedRecMatches(r,f.rec))return false;"
    "if(f.mode!=='Todos'&&String(r.mode_human||'')!==f.mode)return false;"
    "if(q){const blob=[r.name,r.state_human,r.state_short,r.last_task_id,r.visibility_human,r.pending_human].map(v=>String(v||'')).join(' ').toLowerCase();"
    "if(!blob.includes(q))return false}"
    "return true})}"
    "function schedStateBadge(r){const s=String(r?.state_short||r?.state_human||'').trim();"
    "let cls='sched-st-norm';if(!int(r?.is_active))cls='sched-st-off';"
    "else if(r?.is_failed||s.startsWith('Falhou'))cls='sched-st-fail';"
    "else if(String(r?.pending_human||'')==='Sim'||s.startsWith('Pendente'))cls='sched-st-pend';"
    "else if(s.startsWith('Atrasada'))cls='sched-st-late';"
    "else if(s.startsWith('Aguarda'))cls='sched-st-wait';"
    "else if(s.startsWith('Conclu'))cls='sched-st-done';"
    "return '<span class=\"sched-st '+cls+'\" title=\"'+esc(r?.state_human||s)+'\">'+esc(s||'—')+'</span>'}"
    "function int(v){return v!==false&&v!==0&&v!=='0'}"
    "function schedUpdateDetail(){const box=$('sch_sel_detail'),el=$('sch_sel_detail_text');if(!box||!el)return;"
    "const r=_schedRows.find(x=>x.id===_schedSel);if(!r){box.style.display='none';return}"
    "box.style.display='block';el.textContent=String(r.state_human||'—')}"
    "function schedFilterFailed(){if($('sch_f_failed'))$('sch_f_failed').checked=true;"
    "if($('sch_f_pending'))$('sch_f_pending').checked=false;schedSaveFilters();schedRefilter()}"
    "function schedOpenPending(){window._sched_open_pending=true;showPage('scheduled')}"
    "function schedRefilter(){_schedRows=schedApplyFilters(_schedRowsAll);"
    "if(!_schedRows.some(x=>x.id===_schedSel))_schedSel=null;renderScheduled()}"
    "function schedBindFilters(){if(window._schedFiltersBound)return;window._schedFiltersBound=true;"
    "['sch_f_q','sch_f_pending','sch_f_active','sch_f_failed','sch_f_rec','sch_f_mode'].forEach(id=>{const el=$(id);if(!el)return;"
    "const evt=id==='sch_f_q'?'input':'change';el.addEventListener(evt,()=>{schedSaveFilters();schedRefilter()})})}"
    "function schedClearFilters(){if($('sch_f_q'))$('sch_f_q').value='';"
    "if($('sch_f_pending'))$('sch_f_pending').checked=false;"
    "if($('sch_f_active'))$('sch_f_active').checked=true;"
    "if($('sch_f_failed'))$('sch_f_failed').checked=false;"
    "if($('sch_f_rec'))$('sch_f_rec').value='Todos';if($('sch_f_mode'))$('sch_f_mode').value='Todos';"
    "schedSaveFilters();schedRefilter()}"
)

_SCHED_RENDER_JS = (
    "function renderScheduled(){const tb=$('sch_rows');if(!tb)return;tb.innerHTML='';"
    "_schedRows.forEach(r=>{const tr=document.createElement('tr');if(_schedSel===r.id)tr.className='sel';"
    "tr.onclick=()=>{_schedSel=r.id;renderScheduled()};"
    "tr.innerHTML=`<td><b>${esc(r.name||'')}</b></td><td>${esc(r.recurrence_human||'')}</td>"
    "<td>${esc((r.next_run_date||'').slice(0,10))}</td><td>${schedStateBadge(r)}</td>"
    "<td>${esc(r.mode_human||'')}</td><td>${esc(r.visibility_human||'')}</td>"
    "<td>${esc(r.pending_human||'')}</td><td>${esc(r.last_task_id||'—')}</td>`;tb.appendChild(tr)});"
    "const r=_schedRows.find(x=>x.id===_schedSel);const ce=canEditTasks()&&r&&r.can_edit;"
    "['sch_edit','sch_gen','sch_toggle'].forEach(id=>{const el=$(id);if(el)el.disabled=!ce});"
    "if($('sch_mat'))$('sch_mat').disabled=!ce||String(r?.pending_human||'')!=='Sim';"
    "if($('sch_open_task'))$('sch_open_task').disabled=!r||!r.last_task_id;"
    "if($('sch_new'))$('sch_new').style.display=canEditTasks()?'inline-block':'none';"
    "schedUpdateDetail()}"
)

_SCHED_LOGS_HTML = (
    '<details class="card" id="sched-logs" style="margin-top:12px;padding:12px">'
    '<summary style="cursor:pointer;font-weight:600">📜 Últimas operações (técnico)</summary>'
    '<div style="display:flex;justify-content:flex-end;gap:8px;margin:10px 0 6px">'
    '<button class="btn" type="button" onclick="loadScheduledLogs()">Atualizar</button>'
    '<button class="btn" type="button" onclick="clearScheduledLogs()">Limpar</button>'
    "</div>"
    '<pre id="sch_logs" class="muted" style="max-height:170px;overflow:auto;white-space:pre-wrap">Sem operações.</pre>'
    "</details>"
)

_SCHED_CSS = (
    "#page-scheduled .sched-st{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:middle}"
    "#page-scheduled .sched-st-off{background:#f1f5f9;color:#64748b}"
    "#page-scheduled .sched-st-fail{background:#fee2e2;color:#991b1b}"
    "#page-scheduled .sched-st-pend{background:#fef3c7;color:#92400e}"
    "#page-scheduled .sched-st-late{background:#ffedd5;color:#9a3412}"
    "#page-scheduled .sched-st-wait{background:#e0f2fe;color:#075985}"
    "#page-scheduled .sched-st-done{background:#dcfce7;color:#166534}"
    "#page-scheduled .sched-st-norm{background:#eef2ff;color:#3730a3}"
    "#page-scheduled .sched-detail{border-color:#dbeafe;background:#f8fbff}"
    "#page-scheduled details#sched-logs summary{list-style:none}"
    "#page-scheduled details#sched-logs summary::-webkit-details-marker{display:none}"
    "#sched-modal.modal-bg{align-items:flex-start;padding:24px 16px;backdrop-filter:blur(2px)}"
    "#sched-modal .sched-modal{width:min(920px,96vw);max-height:calc(100vh - 48px);display:flex;flex-direction:column;overflow:hidden}"
    "#sched-modal .mh{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:18px 22px;border-bottom:1px solid var(--border)}"
    "#sched-modal .sched-mh-title b{display:block;font-size:18px;line-height:1.25}"
    "#sched-modal .sched-modal-sub{font-size:12px;margin-top:4px}"
    "#sched-modal .mb{flex:1;overflow:auto;padding:18px 22px 22px}"
    "#sched-modal .sched-mf{display:flex;align-items:center;justify-content:space-between;gap:10px;border-top:1px solid var(--border);border-bottom:0;padding:14px 22px}"
    "#sched-modal .sched-mf-right{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}"
    "#sched-modal .sm-wd-grid{display:flex;flex-wrap:wrap;gap:8px;margin-top:4px}"
    "#sched-modal .sm-wd-pill{display:inline-flex;cursor:pointer;user-select:none}"
    "#sched-modal .sm-wd-pill input{position:absolute;opacity:0;width:0;height:0}"
    "#sched-modal .sm-wd-pill span{display:inline-block;padding:7px 12px;border:1px solid #d7dde8;border-radius:999px;background:#fff;font-size:12px;font-weight:600;color:#475569;transition:all .15s ease}"
    "#sched-modal .sm-wd-pill input:checked+span{background:#0869d8;border-color:#0869d8;color:#fff;box-shadow:0 1px 2px rgba(8,105,216,.25)}"
    "#sched-modal .sm-wd-pill input:focus-visible+span{outline:2px solid #93c5fd;outline-offset:2px}"
    "#sched-modal .sched-preview{border:1px solid #e2e8f0;border-radius:10px;background:#f8fafc;padding:10px 12px;min-height:52px;font-size:12px;line-height:1.45;max-height:180px;overflow:auto}"
    "#sched-modal .sched-actions-box{margin-top:10px;padding:12px;border:1px solid #e2e8f0;border-radius:12px;background:#fafbfc}"
    "#sched-modal .sm-check-row{display:flex;align-items:center;gap:8px;font-size:13px;color:#374151;margin-bottom:8px}"
    "#sched-modal .sm-json-adv{margin-top:10px;font-size:12px}"
    "#sched-modal .sm-json-adv summary{cursor:pointer;color:#64748b;padding:4px 0}"
    "#sched-modal .sm-json-adv textarea{margin-top:6px;font-family:Consolas,monospace;font-size:11px}"
    "#sched-modal #sm_actions_rows input{padding:8px 10px;border:1px solid #d7dde8;border-radius:8px;font-size:12px;width:100%}"
    "#sched-modal #sm_actions_box table{width:100%;border-collapse:collapse;font-size:12px}"
    "#sched-modal #sm_actions_box th,#sched-modal #sm_actions_box td{padding:6px 8px;text-align:left;vertical-align:middle}"
    "#sched-modal #sm_actions_box th{font-size:11px;color:#64748b;font-weight:700;text-transform:uppercase;letter-spacing:.02em}"
    "@media(max-width:900px){#sched-modal .sched-modal{width:min(98vw,98vw)}#sched-modal .form{grid-template-columns:repeat(2,1fr)!important}}"
    "@media(max-width:640px){#sched-modal .form{grid-template-columns:1fr!important}#sched-modal .sched-mf{flex-direction:column;align-items:stretch}#sched-modal .sched-mf-right{justify-content:stretch}#sched-modal .sched-mf-right .btn{flex:1}}"
)

_SCHED_MODAL_HTML = (
    '<div class="modal-bg" id="sched-modal" style="display:none">'
    '<div class="modal sched-modal">'
    '<div class="mh">'
    '<div class="sched-mh-title"><b id="sched-modal-title">Programada</b>'
    '<div class="muted sched-modal-sub" id="sched-modal-sub">Template de recorrência</div></div>'
    '<button class="btn" type="button" onclick="closeSchedModal()" aria-label="Fechar">✕</button>'
    "</div>"
    '<div class="mb">'
    '<div class="tabs" id="sm_tabs">'
    '<button type="button" class="on" data-tab="rec" onclick="schedTab(\'rec\')">Recorrência</button>'
    '<button type="button" data-tab="task" onclick="schedTab(\'task\')">Tarefa gerada</button>'
    '<button type="button" data-tab="actions" onclick="schedTab(\'actions\')">Ações default</button>'
    "</div>"
    '<div id="sm_panel_rec" class="form">'
    '<div class="field span4"><label>Nome *</label>'
    '<input id="sm_name" type="text" placeholder="Ex.: Revisão semanal de KPIs"></div>'
    '<div class="field"><label>Recorrência</label>'
    '<select id="sm_rec" onchange="schedRecUI()">'
    '<option value="weekly">Semanal</option><option value="monthly">Mensal</option>'
    '<option value="yearly">Anual</option><option value="once">Uma vez</option>'
    "</select></div>"
    '<div class="field"><label>Intervalo</label><input id="sm_int" type="number" min="1" value="1"></div>'
    '<div class="field"><label>Próxima data</label><input id="sm_next" type="date"></div>'
    '<div class="field span4" id="sm_weekly_row" style="display:none"><label>Dias da semana</label>'
    '<div class="sm-wd-grid">'
    '<label class="sm-wd-pill"><input type="checkbox" id="sm_wd0"><span>Seg</span></label>'
    '<label class="sm-wd-pill"><input type="checkbox" id="sm_wd1"><span>Ter</span></label>'
    '<label class="sm-wd-pill"><input type="checkbox" id="sm_wd2"><span>Qua</span></label>'
    '<label class="sm-wd-pill"><input type="checkbox" id="sm_wd3"><span>Qui</span></label>'
    '<label class="sm-wd-pill"><input type="checkbox" id="sm_wd4"><span>Sex</span></label>'
    '<label class="sm-wd-pill"><input type="checkbox" id="sm_wd5"><span>Sáb</span></label>'
    '<label class="sm-wd-pill"><input type="checkbox" id="sm_wd6"><span>Dom</span></label>'
    "</div></div>"
    '<div class="field" id="sm_dom_row" style="display:none"><label>Dia do mês</label>'
    '<input id="sm_dom" type="number" min="1" max="31"></div>'
    '<div class="field" id="sm_moy_row" style="display:none"><label>Mês (1–12)</label>'
    '<input id="sm_moy" type="number" min="1" max="12"></div>'
    '<div class="field" id="sm_once_row" style="display:none"><label>Data única</label>'
    '<input id="sm_once" type="date"></div>'
    '<div class="field"><label>Modo</label><select id="sm_mode">'
    '<option value="MANUAL">Manual</option><option value="AUTO">Automática</option>'
    "</select></div>"
    '<div class="field"><label>Visibilidade</label><select id="sm_vis">'
    '<option value="PERSONAL">Pessoal</option><option value="SHARED">Partilhada</option>'
    "</select></div>"
    '<div class="field"><label>Lead (dias)</label><input id="sm_lead" type="number" min="0" value="0"></div>'
    '<div class="field"><label>Grace (dias)</label><input id="sm_grace" type="number" min="0" value="30"></div>'
    '<div class="field"><label>Ativa</label><select id="sm_active">'
    '<option value="1">Sim</option><option value="0">Não</option>'
    "</select></div>"
    '<div class="field span4"><label>Pré-visualização</label>'
    '<div id="sm_preview" class="sched-preview muted">Configure a recorrência e use «Pré-visualizar».</div>'
    "</div></div>"
    '<div id="sm_panel_task" class="form" style="display:none">'
    '<div class="field"><label>Projeto</label><input id="sm_projeto" type="text"></div>'
    '<div class="field"><label>Responsável</label><input id="sm_resp" type="text"></div>'
    '<div class="field span2"><label>Assunto</label><input id="sm_assunto" type="text"></div>'
    '<div class="field"><label>Milestone</label><input id="sm_milestone" type="text"></div>'
    '<div class="field"><label>Prioridade</label><select id="sm_prio">'
    '<option value="">(default)</option><option>Baixa</option>'
    '<option selected>Média</option><option>Alta</option>'
    "</select></div>"
    '<div class="field span4"><label class="sm-check-row" style="margin:0">'
    '<input type="checkbox" id="sm_private"> Tarefa privada</label></div>'
    "</div>"
    '<div id="sm_panel_actions" style="display:none">'
    '<label class="sm-check-row"><input type="checkbox" id="sm_gda" onchange="schedValidateActionsJson()"> '
    "Gerar ações default na tarefa criada</label>"
    '<div id="sm_actions_box" class="sched-actions-box">'
    '<div class="toolbar" style="margin:0 0 8px">'
    '<button type="button" class="btn primary" onclick="schedAddActionRow()">＋ Ação</button>'
    "</div>"
    '<div class="act-wrap table-wrap"><table><thead><tr>'
    "<th>Texto</th><th>Owner</th><th>Offset (dias)</th><th></th>"
    "</tr></thead><tbody id=\"sm_actions_rows\"></tbody></table></div>"
    '<details class="sm-json-adv"><summary>JSON avançado</summary>'
    '<textarea id="sm_actions" rows="2" onblur="schedSyncActionsJson()">[]</textarea>'
    "</details></div>"
    '<div id="sm_actions_err" style="color:var(--danger);display:none;font-size:12px;margin-top:8px"></div>'
    "</div></div>"
    '<div class="mf sched-mf">'
    '<button class="btn" type="button" onclick="schedPreview()">Pré-visualizar</button>'
    '<div class="sched-mf-right">'
    '<button class="btn" type="button" onclick="closeSchedModal()">Cancelar</button>'
    '<button class="btn primary" type="button" onclick="saveSchedModal()">Guardar</button>'
    "</div></div></div></div>"
)

_SCHED_MODAL_JS = (
    "function schedTab(id){const tabs=['rec','task','actions'];"
    "tabs.forEach(t=>{const p=$('sm_panel_'+t);if(!p)return;"
    "if(t===id){p.style.display=(t==='actions')?'block':'grid'}else p.style.display='none'});"
    "document.querySelectorAll('#sm_tabs [data-tab]').forEach(b=>b.classList.toggle('on',b.dataset.tab===id))}"
    "function schedRecUI(){const r=$('sm_rec')?.value||'monthly';"
    "const show=(id,on)=>{const el=$(id);if(el)el.style.display=on?'':'none'};"
    "show('sm_weekly_row',r==='weekly');show('sm_dom_row',r==='monthly'||r==='yearly');"
    "show('sm_moy_row',r==='yearly');show('sm_once_row',r==='once')}"
)

_SCHED_LOGS_JS = (
    "async function loadScheduledLogs(){try{const el=$('sch_logs');if(!el)return;el.textContent='A carregar...';"
    "let j=await api('/api/scheduled/logs?limit=120');const rows=j.rows||[];"
    "if(!rows.length){el.textContent='Sem operações.';return}"
    "el.textContent=rows.map(r=>{const st=r.ok?'OK':'ERRO';"
    "return '['+String(r.ts||'')+'] ['+st+'] '+String(r.action||'')+' · '+String(r.user||'')+'\\n'+String(r.message||'')}).join('\\n\\n')}"
    "catch(e){toast(e.message,true);const el=$('sch_logs');if(el)el.textContent='Erro ao carregar logs: '+e.message}}"
    "async function clearScheduledLogs(){try{await api('/api/scheduled/logs/clear',{method:'POST',body:'{}'});"
    "const el=$('sch_logs');if(el)el.textContent='Sem operações.';toast('Logs limpos')}"
    "catch(e){toast(e.message,true)}}"
)


_TASK_GANTT_ASSETS = (
    '<link rel="stylesheet" href="/web/vendor/frappe-gantt/frappe-gantt.css">'
    '<script defer src="/web/vendor/frappe-gantt/frappe-gantt.umd.js"></script>'
)

_TASK_GANTT_CSS = (
    "#task_gantt_modal .modal{width:min(1460px,97vw);max-height:92vh;box-shadow:0 18px 38px rgba(2,6,23,.22)}"
    "#task_gantt_modal .modal.is-max{width:99vw !important;max-height:98vh !important;height:98vh}"
    "#task_gantt_modal .modal.is-max .mc{height:calc(98vh - 58px)}"
    "#task_gantt_modal.modal-bg{backdrop-filter:blur(1.5px)}"
    "#task_gantt_modal .mh{position:sticky;top:0;z-index:2;background:#fff;border-bottom:1px solid #e5e7eb}"
    "#task_gantt_modal .mc{padding-top:8px;overflow:hidden}"
    "#task-gantt-toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px;position:sticky;top:0;z-index:1;background:#fff;padding:4px 0 8px}"
    "#task-gantt-toolbar .btn{padding:5px 10px;font-size:12px}"
    "#task-gantt-toolbar .btn:hover{filter:brightness(.97)}"
    "#task-gantt-toolbar .btn:active{transform:translateY(1px)}"
    "#task-gantt-toolbar .btn:focus-visible{outline:2px solid #93c5fd;outline-offset:1px}"
    "#task-gantt-meta{margin-left:auto;font-size:12px;color:#64748b}"
    "#task-gantt-legend{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:-2px 0 8px}"
    "#task-gantt-legend .lg{display:inline-flex;align-items:center;gap:6px;padding:2px 8px;border:1px solid #dbe3ef;border-radius:999px;background:#f8fbff;font-size:11px;color:#334155}"
    "#task-gantt-legend .dot{width:8px;height:8px;border-radius:999px;display:inline-block}"
    "#task-gantt-legend .d-todo{background:#64748b}"
    "#task-gantt-legend .d-progress{background:#2563eb}"
    "#task-gantt-legend .d-blocked{background:#ea580c}"
    "#task-gantt-legend .d-overdue{background:#dc2626}"
    "#task-gantt-legend .d-done{background:#16a34a}"
    "#task-gantt{min-height:460px;border:1px solid #e5e7eb;border-radius:8px;padding:8px;overflow:auto;background:#fff}"
    "#task-gantt::-webkit-scrollbar{height:10px;width:10px}"
    "#task-gantt::-webkit-scrollbar-thumb{background:#cbd5e1;border-radius:999px}"
    "#task-gantt::-webkit-scrollbar-track{background:#f1f5f9}"
    "#task-gantt .gantt-container{--g-row-color:#f8fafc;--g-border-color:#d7e2ee;--g-tick-color:#e8eef5;--g-tick-color-thick:#d3deea;--g-today-highlight:#0f172a}"
    "#task-gantt .gantt .bar-label{font-size:12px;font-weight:600;text-shadow:0 1px 0 rgba(255,255,255,.25)}"
    "#task-gantt .gantt-container .lower-text{font-size:11px;color:#64748b}"
    "#task-gantt .gantt-container .upper-text{font-size:13px;font-weight:600;color:#1f2937}"
    "#task-gantt .gantt .current-highlight{width:2px}"
    "#task-gantt .gantt .current-date-highlight{font-weight:700}"
    "#task-gantt .gantt .bar-wrapper .bar{rx:4;ry:4}"
    "#task-gantt .gantt .bar-wrapper:hover .bar{filter:brightness(.97)}"
    "#task-gantt .gantt .bar-wrapper:hover .bar-label{fill:#0f172a}"
    "#task-gantt .gantt .row-line{stroke:#dbe5f0}"
    "#task-gantt .gantt .tick{stroke:#e8eef5}"
    "#task-gantt .gantt .tick.thick{stroke:#d3deea}"
    "#task-gantt-undated{margin-top:12px;border:1px dashed #d3deea;border-radius:8px;padding:8px 10px;background:#f8fafc}"
    "#task-gantt-undated b{display:block;margin-bottom:4px}"
    "#task-gantt-undated ul{margin:8px 0 0 16px;padding:0;max-height:112px;overflow:auto}"
    "#task-gantt-undated li{margin:4px 0;line-height:1.35}"
    "#task-gantt .bar-progress{opacity:.9}"
    "#task-gantt .tg-done .bar{fill:#22c55e}"
    "#task-gantt .tg-done .bar-progress{fill:#16a34a}"
    "#task-gantt .tg-progress .bar{fill:#3b82f6}"
    "#task-gantt .tg-progress .bar-progress{fill:#2563eb}"
    "#task-gantt .tg-blocked .bar{fill:#f97316}"
    "#task-gantt .tg-blocked .bar-progress{fill:#ea580c}"
    "#task-gantt .tg-overdue .bar{fill:#ef4444}"
    "#task-gantt .tg-overdue .bar-progress{fill:#dc2626}"
    "#task-gantt .tg-todo .bar{fill:#94a3b8}"
    "#task-gantt .tg-todo .bar-progress{fill:#64748b}"
    "@media(max-width:1366px){#task_gantt_modal .modal{width:min(1440px,98vw);max-height:94vh}#task-gantt{min-height:430px}}"
    "@media(max-width:1100px){#task_gantt_modal .modal{width:min(1080px,99vw)}#task-gantt-toolbar{gap:6px}#task-gantt-toolbar .btn{padding:4px 8px;font-size:11px}#task-gantt{min-height:390px}}"
    "@media(max-width:900px){#task-gantt-meta{width:100%;margin-left:0}#task-gantt-undated ul{max-height:88px}}"
)

_TASK_GANTT_JS = (
    "let _taskGanttObj=null,_taskGanttData=null,_taskGanttView='Week',_taskGanttCanEdit=false,_taskGanttBusy=false;"
    "function _taskGanttEnsureModal(){if($('task_gantt_modal'))return;"
    "document.body.insertAdjacentHTML('beforeend',"
    "'<div class=\"modal-bg\" id=\"task_gantt_modal\" style=\"display:none\">"
    "<div class=\"modal\"><div class=\"mh\"><h3 id=\"task_gantt_title\" style=\"margin:0\">Gantt</h3>"
    "<div style=\"display:flex;gap:6px\"><button class=\"btn\" id=\"task_gantt_expand_btn\" onclick=\"taskGanttToggleExpand()\">Expandir</button><button class=\"btn\" onclick=\"taskGanttClose()\">✕</button></div></div>"
    "<div class=\"mc\">"
    "<div id=\"task-gantt-toolbar\">"
    "<button class=\"btn\" onclick=\"taskGanttSetView(\\'Day\\')\">Dia</button>"
    "<button class=\"btn\" onclick=\"taskGanttSetView(\\'Week\\')\">Semana</button>"
    "<button class=\"btn\" onclick=\"taskGanttSetView(\\'Month\\')\">Mês</button>"
    "<button class=\"btn\" onclick=\"taskGanttToday()\">Hoje</button>"
    "<button class=\"btn\" onclick=\"taskGanttRefresh()\">Atualizar</button>"
    "<button class=\"btn\" onclick=\"taskGanttClose()\">Fechar</button>"
    "<span id=\"task-gantt-meta\"></span>"
    "</div>"
    "<div id=\"task-gantt-legend\"></div>"
    "<div id=\"task-gantt\"></div>"
    "<section id=\"task-gantt-undated\"><b>Sem data</b><div id=\"task-gantt-undated-body\" class=\"muted\">—</div></section>"
    "</div></div></div>');}"
    "function _taskGanttStatusClass(it){const st=String(it.status||'').trim().toLowerCase();"
    "const done=!!(it&&((it.done===true)||(it.is_done===true)));"
    "if(done||st.includes('conclu'))return 'tg-done';"
    "if(st.includes('bloque'))return 'tg-blocked';"
    "if(st.includes('progres')||st.includes('curso'))return 'tg-progress';"
    "if(it&&it.is_overdue)return 'tg-overdue';return 'tg-todo'}"
    "function _taskGanttRenderLegend(items){const el=$('task-gantt-legend');if(!el)return;const arr=(items||[]);"
    "const c={todo:0,progress:0,blocked:0,overdue:0,done:0};arr.forEach(it=>{const k=_taskGanttStatusClass(it);if(k==='tg-done')c.done++;else if(k==='tg-progress')c.progress++;else if(k==='tg-blocked')c.blocked++;else if(k==='tg-overdue')c.overdue++;else c.todo++});"
    "el.innerHTML='<span class=\"lg\"><span class=\"dot d-todo\"></span>A fazer: '+c.todo+'</span>'"
    "+'<span class=\"lg\"><span class=\"dot d-progress\"></span>Em progresso: '+c.progress+'</span>'"
    "+'<span class=\"lg\"><span class=\"dot d-blocked\"></span>Bloqueada: '+c.blocked+'</span>'"
    "+'<span class=\"lg\"><span class=\"dot d-overdue\"></span>Atrasada: '+c.overdue+'</span>'"
    "+'<span class=\"lg\"><span class=\"dot d-done\"></span>Concluída: '+c.done+'</span>'}"
    "function _taskGanttToRows(items){return(items||[]).map(it=>({"
    "id:String(it.id||''),name:String(it.name||''),start:String(it.start||''),end:String(it.end||''),"
    "progress:Number(it.progress||0),dependencies:String(it.dependencies||''),custom_class:_taskGanttStatusClass(it),_raw:it}))}"
    "function _taskGanttFmtDate(v){try{const d=(v instanceof Date)?v:new Date(v);if(isNaN(d.getTime()))return '';"
    "const m=String(d.getMonth()+1).padStart(2,'0'),dd=String(d.getDate()).padStart(2,'0');return d.getFullYear()+'-'+m+'-'+dd}catch(_){return ''}}"
    "async function _taskGanttOnDateChange(t,start,end){try{if(!_taskGanttCanEdit||_taskGanttBusy)return;"
    "const r=(t&&t._raw)||{};const aid=Number(r.action_id||0);if(!aid)return;"
    "const s=_taskGanttFmtDate(start),e=_taskGanttFmtDate(end);if(!s||!e){toast('Datas inválidas',true);return}"
    "_taskGanttBusy=true;"
    "await api('/api/actions/'+aid+'/gantt-update',{method:'POST',body:JSON.stringify({start_date:s,due_date:e})});"
    "if(r){r.start=s;r.end=e;r.start_date=s;r.due_date=e}"
    "toast('Datas atualizadas');"
    "}catch(err){toast(err.message||'Falha ao guardar datas',true);setTimeout(()=>taskGanttRefresh(),50)}"
    "finally{_taskGanttBusy=false}}"
    "function _taskGanttRenderUndated(rows){const box=$('task-gantt-undated-body');if(!box)return;"
    "if(!rows||!rows.length){box.innerHTML='<span class=\"muted\">Sem itens sem data.</span>';return}"
    "box.innerHTML='<ul>'+rows.map(r=>`<li><b>${esc(r.kind||'ITEM')}</b> · ${esc(r.name||'—')} "
    "(${esc(r.status||'—')})</li>`).join('')+'</ul>'}"
    "function _taskGanttRender(){const host=$('task-gantt');if(!host)return;host.innerHTML='';"
    "if(!window.Gantt){host.innerHTML='<p class=\"muted\">Frappe Gantt não carregado.</p>';return}"
    "const srcRows=((_taskGanttData&&_taskGanttData.items)||[]);_taskGanttRenderLegend(srcRows);const rows=_taskGanttToRows(srcRows);"
    "if(!rows.length){host.innerHTML='<p class=\"muted\">Sem barras para renderizar.</p>';return}"
    "_taskGanttObj=new Gantt('#task-gantt',rows,{view_mode:_taskGanttView||'Week',readonly:(!_taskGanttCanEdit),language:'pt',"
    "date_change:(task,start,end)=>{_taskGanttOnDateChange(task,start,end)},"
    "custom_popup_html:t=>{const r=(t&&t._raw)||{};return `<div style=\"padding:8px 10px;min-width:220px\">"
    "<b>${esc(r.name||'')}</b><br><span class=\"muted\">${esc(r.start||'—')} → ${esc(r.end||'—')}</span><br>"
    "<span>Status: ${esc(r.status||'—')}</span><br><span>Responsável: ${esc(r.owner||'—')}</span></div>`}})}"
    "function taskGanttSetView(v){_taskGanttView=String(v||'Week');_taskGanttRender()}"
    "function taskGanttToday(){_taskGanttView='Day';_taskGanttRender()}"
    "function taskGanttToggleExpand(){const m=document.querySelector('#task_gantt_modal .modal');if(!m)return;const b=$('task_gantt_expand_btn');"
    "const on=m.classList.toggle('is-max');if(b)b.textContent=on?'Restaurar':'Expandir'}"
    "async function taskGanttRefresh(){if(!_detailTid)return;await detailOpenTaskGantt()}"
    "function taskGanttClose(){const bg=$('task_gantt_modal');if(!bg)return;bg.style.display='none';"
    "const m=bg.querySelector('.modal');if(m)m.classList.remove('is-max');const b=$('task_gantt_expand_btn');if(b)b.textContent='Expandir'}"
    "async function detailOpenTaskGantt(){try{if(!_detailTid)return;"
    "_taskGanttEnsureModal();const j=await api('/api/tasks/'+encodeURIComponent(_detailTid)+'/gantt-data');"
    "_taskGanttData=j||{};if($('task_gantt_title'))$('task_gantt_title').textContent='Gantt — '+String(j.task_id||_detailTid);"
    "_taskGanttCanEdit=!!(j&&j.permissions&&j.permissions.can_edit);"
    "if($('task-gantt-meta'))$('task-gantt-meta').textContent=_taskGanttCanEdit?'Edição ativa (drag/resize)':'Somente leitura';"
    "_taskGanttRenderUndated(j.undated_items||[]);_taskGanttRender();const bg=$('task_gantt_modal');if(bg)bg.style.display='flex';const b=$('task_gantt_expand_btn');if(b)b.textContent='Expandir'}"
    "catch(e){toast(e.message,true)}}"
    "function taskGanttEnsureButton(){const h=document.querySelector('#td_sec_actions h3');if(!h)return;"
    "if(document.getElementById('td_btn_task_gantt'))return;"
    "const b=document.createElement('button');b.id='td_btn_task_gantt';b.className='btn';b.type='button';"
    "b.style.marginLeft='10px';b.textContent='📊 Gantt da tarefa';b.onclick=detailOpenTaskGantt;h.appendChild(b)}"
    "if(!window._taskGanttHooked&&typeof loadTaskDetail==='function'){window._taskGanttHooked=true;"
    "const _orig=loadTaskDetail;loadTaskDetail=async function(){const r=await _orig.apply(this,arguments);"
    "setTimeout(taskGanttEnsureButton,0);return r}}"
    "setTimeout(taskGanttEnsureButton,0);"
)


def _load_base():
    global _loading_base
    if _loading_base:
        raise RuntimeError("Recursão ao carregar bytecode base")
    _loading_base = True
    try:
        if not _PYC.is_file():
            raise FileNotFoundError(
                f"Bytecode em falta: {_PYC}\n"
                "Restaure App_web_ui_moderno.py via histórico OneDrive ou copie "
                "__pycache__/_recovered/App_web_ui_moderno.cpython-313.pyc para _app_web_ui_base.cpython-313.pyc."
            )
        spec = importlib.util.spec_from_file_location("_app_web_base", _PYC)
        if spec is None or spec.loader is None:
            raise ImportError(f"Não foi possível carregar {_PYC}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        _loading_base = False


def _patch_html(html: str) -> str:
    if _SCHED_SAVE_OLD not in html:
        raise RuntimeError("Bytecode HTML inesperado — não foi possível aplicar patch programadas v0.11.1")
    html = html.replace(_SCHED_SAVE_OLD, _SCHED_HELPERS, 1)
    html = re.sub(r"UI_BUILD='[\d.]+'", f"UI_BUILD='{APP_VERSION}'", html, count=1)
    html = html.replace(
        '<textarea id="sm_actions" rows="3" style="width:100%;font-family:Consolas,monospace;font-size:12px">',
        '<textarea id="sm_actions" rows="3" onblur="schedValidateActionsJson()" '
        'style="width:100%;font-family:Consolas,monospace;font-size:12px">',
        1,
    )
    html = html.replace(
        '[{"text":"Revisao","due_offset_days":0,"owner":""}]</textarea></div></div></div><div class="mf">',
        '[{"text":"Revisao","due_offset_days":0,"owner":""}]</textarea></div>'
        '<div style="grid-column:1/-1"><div id="sm_actions_err" style="color:var(--danger);display:none;'
        'font-size:12px;margin-top:4px"></div></div>'
        '<div style="grid-column:1/-1;margin-top:4px"><div id="sm_preview" class="muted" '
        'style="font-size:12px;max-height:160px;overflow:auto"></div></div>'
        '</div></div><div class="mf">',
        1,
    )
    html = html.replace(
        '<div class="mf"><button class="btn" onclick="closeSchedModal()">Cancelar</button>'
        '<button class="btn primary" onclick="saveSchedModal()">Guardar</button></div>',
        '<div class="mf"><button class="btn" onclick="closeSchedModal()">Cancelar</button>'
        '<button class="btn" onclick="schedPreview()">Pre-visualizar</button>'
        '<button class="btn primary" onclick="saveSchedModal()">Guardar</button></div>',
        1,
    )
    return _patch_html_stability(
        _patch_html_machines(
            _patch_html_project_advanced(
                _patch_html_auth(
                    _patch_html_admin_heavy(
                        _patch_html_system_lists(
                            _patch_html_diagnostics(
                                _patch_html_task_detail(
                                    _patch_html_achievements(
                                        _patch_html_board(
                                            _patch_html_scheduled(
                                                _patch_html_my_day(_patch_html_notes(_patch_html_tasks(html)))
                                            )
                                        )
                                    )
                                )
                            )
                        )
                    )
                )
            )
        )
    )


_NOTES_PAGE = (
    '<div id="page-notes" class="page"><div class="title"><div>'
    "<h1>Notas</h1>"
    '<div class="muted" id="notes_sub">Notas técnicas por utilizador. Use NOTA:, ATENÇÃO: e PASSO:.</div>'
    "</div>"
    '<button class="btn primary" onclick="saveNotes(false)">Guardar</button></div>'
    '<section class="card" style="padding:16px">'
    '<div class="rtbar" style="margin-bottom:10px">'
    '<button type="button" onclick="rtCmd(\'bold\',\'notes_rt\')"><b>B</b></button>'
    '<button type="button" onclick="rtCmd(\'italic\',\'notes_rt\')"><i>I</i></button>'
    '<button type="button" onclick="rtInsert(\'notes_rt\',\'note\')">Nota</button>'
    '<button type="button" onclick="rtInsert(\'notes_rt\',\'warn\')">Atencao</button>'
    '<button type="button" onclick="rtLink(\'notes_rt\')">Link</button>'
    '<button type="button" onclick="rtStamp(\'notes_rt\')">Data/Hora</button>'
    '<button type="button" onclick="rtClear(\'notes_rt\')">Limpar</button>'
    "</div>"
    '<div id="notes_rt" class="rtbox" contenteditable="true" style="min-height:420px"></div>'
    '<div class="muted" id="notes_status" style="margin-top:10px;font-size:12px"></div>'
    "</section></div>"
)

_NOTES_JS = (
    "let _notesDirty=false,_notesAutosaveTimer=null,_notesSavedAt='';"
    "function _bindNotesEditor(){const el=$('notes_rt');if(!el||el._notesBound)return;"
    "el._notesBound=true;el.addEventListener('input',scheduleNotesAutosave)}"
    "function updateNotesStatus(){const el=$('notes_status');if(!el)return;"
    "const path=el.dataset.path||'';const st=_notesDirty?'Não guardado':"
    "(_notesSavedAt?'Guardado às '+_notesSavedAt:'Guardado');"
    "el.textContent=(path?'Ficheiro: '+path+' · ':'')+'Estado: '+st}"
    "function scheduleNotesAutosave(){_notesDirty=true;updateNotesStatus();"
    "if(_notesAutosaveTimer)clearTimeout(_notesAutosaveTimer);"
    "_notesAutosaveTimer=setTimeout(()=>saveNotes(true),900)}"
    "async function loadNotes(){try{let j=await api('/api/notes');"
    "setRtHtml('notes_rt',j.content||'');const st=$('notes_status');"
    "if(st){st.dataset.path=j.path||'';_notesDirty=false;_notesSavedAt='';updateNotesStatus()}"
    "const sub=$('notes_sub');if(sub&&j.username)sub.textContent="
    "'Notas tecnicas - '+j.username+'. Use NOTA:, ATENCAO: e PASSO:.';_bindNotesEditor()"
    "}catch(e){toast(e.message,true)}}"
    "async function saveNotes(silent){try{const content=getRtHtml('notes_rt');"
    "let j=await api('/api/notes',{method:'PUT',body:JSON.stringify({content})});"
    "_notesDirty=false;_notesSavedAt=j.saved_at||new Date().toLocaleTimeString('pt-PT',{hour:'2-digit',minute:'2-digit'});unsavedClear();"
    "updateNotesStatus();if(!silent)toast('Notas guardadas')}catch(e){toast(e.message,true)}}"
)


_MY_DAY_PAGE = (
    '<div id="page-myday" class="page"><div class="title"><div>'
    "<h1>O Meu Dia</h1>"
    '<div class="muted" id="myday_sub">Visão pessoal diária. Dados por utilizador autenticado.</div>'
    "</div>"
    '<button class="btn" onclick="loadMyDay()">Atualizar</button></div>'
    '<section class="card md-hero"><div class="md-hero-title"><h2 id="md_greet">Bom dia!</h2>'
    '<div class="muted">Aqui está o que precisa da sua atenção hoje.</div></div></section>'
    '<section class="kpis md-kpis">'
    '<div class="kpi md-kpi"><div class="ico">📋</div><div><div class="muted">Minhas tarefas</div><div class="v" id="md_k_total">0</div><button class="md-link-btn" onclick="showPage(\'tasks\')">Ver detalhes →</button></div></div>'
    '<div class="kpi md-kpi"><div class="ico">⏰</div><div><div class="muted">Atrasadas</div><div class="v" id="md_k_overdue">0</div><button class="md-link-btn" onclick="showPage(\'tasks\')">Ver detalhes →</button></div></div>'
    '<div class="kpi md-kpi"><div class="ico">🔒</div><div><div class="muted">Bloqueadas</div><div class="v" id="md_k_blocked">0</div><button class="md-link-btn" onclick="showPage(\'tasks\')">Ver detalhes →</button></div></div>'
    '<div class="kpi md-kpi"><div class="ico">📆</div><div><div class="muted">Prazo em 7 dias</div><div class="v" id="md_k_due7">0</div><button class="md-link-btn" onclick="showPage(\'tasks\')">Ver detalhes →</button></div></div>'
    '<div class="kpi md-kpi"><div class="ico">🚧</div><div><div class="muted">Em progresso</div><div class="v" id="md_k_progress">0</div><button class="md-link-btn" onclick="showPage(\'tasks\')">Ver detalhes →</button></div></div>'
    '<div class="kpi md-kpi"><div class="ico">🔥</div><div><div class="muted">Alta prioridade</div><div class="v" id="md_k_highprio">0</div><button class="md-link-btn" onclick="showPage(\'tasks\')">Ver detalhes →</button></div></div>'
    "</section>"
    '<div class="md-grid">'
    '<section class="card md-card md-card-main"><div class="md-head"><h3>Requer atenção imediata</h3><button class="md-link-btn" onclick="showPage(\'tasks\')">Ver todas</button></div>'
    '<div class="muted" style="margin-bottom:10px">Itens críticos: atrasadas, bloqueadas, alta prioridade ou prazo iminente.</div>'
    '<div id="md_immediate" class="md-list"><div class="muted">A carregar...</div></div></section>'
    '<section class="card md-card"><div class="md-head"><h3>Prazos próximos 7 dias</h3><button class="md-link-btn" onclick="showPage(\'tasks\')">Ver calendário</button></div>'
    '<div class="muted" style="margin-bottom:10px">Tarefas abertas com prazo entre hoje e os próximos 7 dias.</div>'
    '<div id="md_due7_list" class="md-list"><div class="muted">A carregar...</div></div></section>'
    '<section class="card md-card"><div class="md-head"><h3>Em desenvolvimento</h3><button class="md-link-btn" onclick="showPage(\'tasks\')">Ver todas</button></div>'
    '<div class="muted" style="margin-bottom:10px">Tarefas em progresso a decorrer neste momento.</div>'
    '<div id="md_dev_list" class="md-list"><div class="muted">A carregar...</div></div></section>'
    '<section class="card md-card"><div class="md-head"><h3>Top prioridades</h3><button class="md-link-btn" onclick="showPage(\'tasks\')">Ver todas</button></div>'
    '<div class="muted" style="margin-bottom:10px">Tarefas abertas com maior prioridade de acompanhamento.</div>'
    '<div id="md_topprio_list" class="md-list"><div class="muted">A carregar...</div></div></section>'
    '<section class="card md-card"><div class="md-head"><h3>Estou envolvido</h3><button class="md-link-btn" onclick="showPage(\'tasks\')">Ver todas</button></div>'
    '<div class="muted" style="margin-bottom:10px">Tarefas onde participa como worker (não responsável principal).</div>'
    '<div id="md_involved_list" class="md-list"><div class="muted">A carregar...</div></div></section>'
    '<section class="card md-card"><div class="md-head"><h3>Resultados da semana</h3><button class="md-link-btn" onclick="showPage(\'tasks\')">Ver todas</button></div>'
    '<div class="muted" id="md_week_sub" style="margin-bottom:10px">Resumo de tarefas com prazo nesta semana.</div>'
    '<div id="md_week_list" class="md-list"><div class="muted">A carregar...</div></div></section>'
    "</div>"
    '<section class="card md-quick"><div class="md-head"><h3>Ações rápidas</h3></div>'
    '<div class="md-quick-grid">'
    '<button class="btn" onclick="showPage(\'tasks\')">Abrir tarefas</button>'
    '<button class="btn" onclick="showPage(\'dashboard\')">Abrir dashboard</button>'
    '<button class="btn" onclick="showPage(\'ach\')">Abrir conquistas</button>'
    '<button class="btn" onclick="showPage(\'board\')">Abrir board</button>'
    '<button class="btn" onclick="schedOpenPending()">Programadas pendentes</button>'
    '<button class="btn" onclick="loadMyDay()">Atualizar estado</button>'
    "</div></section></div>"
)

_MY_DAY_JS = (
    "function _mdNum(id,v){const el=$(id);if(el)el.textContent=String(v??0)}"
    "function _mdEsc(v){return esc(String(v??''))}"
    "function _mdDate(v){const s=String(v||'').slice(0,10);return s||'—'}"
    "function _mdSetGreeting(){const el=$('md_greet');if(!el)return;const h=(new Date()).getHours();"
    "let t='Bom dia';if(h>=12&&h<19)t='Boa tarde';else if(h>=19||h<5)t='Boa noite';"
    "const n=String(user?.display_name||user?.username||'').trim();el.textContent=t+(n?(', '+n+'!'):'!')}"
    "function _mdDueText(n){const d=Number(n);if(!Number.isFinite(d))return 'Sem prazo';if(d<0)return 'Atrasada';if(d===0)return 'Hoje';if(d===1)return 'Amanhã';return 'Em '+d+' dias'}"
    "function _mdDueBadge(n){const d=Number(n);let cls='';if(Number.isFinite(d)){if(d<=1)cls='bad';else if(d<=3)cls='warn'}"
    "return `<span class=\"pill md-due ${cls}\">${_mdEsc(_mdDueText(n))}</span>`}"
    "function _mdPrioWeight(p){const s=String(p||'').trim().toLowerCase();if(s.startsWith('alta'))return 0;if(s.startsWith('m'))return 1;return 2}"
    "function _mdPrioBadge(p){const s=String(p||'—').trim()||'—';const w=_mdPrioWeight(s);const cls=w===0?'bad':(w===1?'warn':'');"
    "return `<span class=\"pill md-prio ${cls}\">${_mdEsc(s)}</span>`}"
    "function _mdStateBadge(st){const s=String(st||'').trim();"
    "const cls=s.toLowerCase().includes('bloque')?'bad':(s.toLowerCase().includes('progres')?'warn':'');"
    "return `<span class=\"md-state ${cls}\">${_mdEsc(s||'—')}</span>`}"
    "function _mdOpenTask(tid){if(!tid)return;showPage('tasks');setTimeout(()=>openTaskDetail(tid),0)}"
    "function _mdRenderDue7(j){const box=$('md_due7_list');if(!box)return;const rows=Array.isArray(j?.due_next_7)?j.due_next_7:[];"
    "if(!rows.length){box.innerHTML='<div class=\"muted\">Sem prazos nos próximos 7 dias.</div>';return}"
    "box.innerHTML='<table class=\"md-table compact\"><thead><tr><th>Tarefa</th><th>Estado</th><th>Prazo</th><th>Janela</th></tr></thead><tbody>'"
    "+rows.map(r=>{const tid=String(r.task_id||'');const title=_mdEsc(r.title||tid||'—');const due=_mdDate(r.due_date);"
    "const st=_mdStateBadge(r.status||'');const dueB=_mdDueBadge(r.days_to_due);"
    "return `<tr data-tid=\"${_mdEsc(tid)}\" onclick=\"_mdOpenTask(this.dataset.tid)\" title=\"Abrir detalhe\">"
    "<td><b>${title}</b><div class=\"muted\">${_mdEsc(tid)} · ${_mdEsc(r.project||'Sem projeto')}</div></td>"
    "<td>${st}</td><td>${_mdEsc(due)}</td><td>${dueB}</td></tr>`}).join('')+'</tbody></table>'}"
    "function _mdRenderTopPrio(j){const box=$('md_topprio_list');if(!box)return;const rows=Array.isArray(j?.top_priorities)?j.top_priorities:[];"
    "if(!rows.length){box.innerHTML='<div class=\"muted\">Sem prioridades a destacar.</div>';return}"
    "box.innerHTML='<table class=\"md-table compact\"><thead><tr><th>Tarefa</th><th>Prio.</th><th>Estado</th><th>Prazo</th></tr></thead><tbody>'"
    "+rows.map(r=>{const tid=String(r.task_id||'');const title=_mdEsc(r.title||tid||'—');const due=_mdDate(r.due_date);"
    "const st=_mdStateBadge(r.status||'');const pb=_mdPrioBadge(r.priority||'—');"
    "return `<tr data-tid=\"${_mdEsc(tid)}\" onclick=\"_mdOpenTask(this.dataset.tid)\" title=\"Abrir detalhe\">"
    "<td><b>${title}</b><div class=\"muted\">${_mdEsc(tid)} · ${_mdEsc(r.project||'Sem projeto')}</div></td>"
    "<td>${pb}</td><td>${st}</td><td>${_mdEsc(due)}</td></tr>`}).join('')+'</tbody></table>'}"
    "function _mdMoney(v){const n=Number(v||0);return n.toLocaleString('pt-PT',{minimumFractionDigits:2,maximumFractionDigits:2})+' €'}"
    "function _mdRenderInvolved(j){const box=$('md_involved_list');if(!box)return;const rows=Array.isArray(j?.involved)?j.involved:[];"
    "if(!rows.length){box.innerHTML='<div class=\"muted\">Sem tarefas em colaboração no momento.</div>';return}"
    "box.innerHTML='<table class=\"md-table compact\"><thead><tr><th>Tarefa</th><th>Projeto</th><th>Estado</th></tr></thead><tbody>'"
    "+rows.map(r=>{const tid=String(r.task_id||'');const title=_mdEsc(r.title||tid||'—');const due=_mdDate(r.due_date);"
    "const st=_mdStateBadge(r.status||'');const owner=_mdEsc(r.owner||'—');"
    "return `<tr data-tid=\"${_mdEsc(tid)}\" onclick=\"_mdOpenTask(this.dataset.tid)\" title=\"Abrir detalhe\">"
    "<td><b>${title}</b><div class=\"muted\">${_mdEsc(tid)} · ${owner}</div></td>"
    "<td>${_mdEsc(r.project||'Sem projeto')}<div class=\"muted\">Prazo: ${_mdEsc(due)}</div></td><td>${st}</td></tr>`}).join('')+'</tbody></table>'}"
    "function _mdRenderDev(j){const box=$('md_dev_list');if(!box)return;const rows=Array.isArray(j?.in_development)?j.in_development:[];"
    "if(!rows.length){box.innerHTML='<div class=\"muted\">Sem tarefas em progresso no momento.</div>';return}"
    "const pct=Math.max(0,Math.min(100,Number(j?.dev_progress_pct||0)));"
    "const txt=String(rows.length)+' tarefas em progresso';"
    "box.innerHTML=`<div class=\"md-dev-hero\">"
    "<div class=\"md-donut\" style=\"--p:${pct}\"><span>${pct}%</span></div>"
    "<div><div class=\"md-dev-n\">${txt}</div><div class=\"muted\">Visão geral das iniciativas em curso.</div></div></div>`"
    "+'<table class=\"md-table compact\"><thead><tr><th>Tarefa</th><th>Estado</th><th>Prazo</th></tr></thead><tbody>'"
    "+rows.map(r=>{const tid=String(r.task_id||'');const title=_mdEsc(r.title||tid||'—');const due=_mdDate(r.due_date);"
    "const owner=_mdEsc(r.owner||'—');const dueB=_mdDueBadge(r.days_to_due);const st=_mdStateBadge(r.status||'');"
    "return `<tr data-tid=\"${_mdEsc(tid)}\" onclick=\"_mdOpenTask(this.dataset.tid)\" title=\"Abrir detalhe\">"
    "<td><b>${title}</b><div class=\"muted\">${_mdEsc(tid)} · ${_mdEsc(r.project||'Sem projeto')}</div></td>"
    "<td>${st}</td><td>${_mdEsc(due)} · ${dueB}<div class=\"muted\">${owner}</div></td></tr>`}).join('')+'</tbody></table>'}"
    "function _mdRenderWeek(j){const box=$('md_week_list');if(!box)return;const rows=Array.isArray(j?.week_results)?j.week_results:[];"
    "const d=Number(j?.week_done||0),o=Number(j?.week_open||0),rc=Number(j?.week_recovered||0),im=Number(j?.week_impact||0);"
    "const sub=$('md_week_sub');if(sub){sub.textContent='Resumo de tarefas com prazo nesta semana. Concluídas: '+d+' · Em aberto: '+o}"
    "const doneRows=rows.filter(r=>!!r.done);"
    "box.innerHTML=`<div class=\"md-week-kpis\">"
    "<div><b>Concluídas</b><span>${d}</span></div>"
    "<div><b>Recuperadas</b><span>${rc}</span></div>"
    "<div><b>Impacto €</b><span>${_mdMoney(im)}</span></div></div>`;"
    "if(!doneRows.length){box.innerHTML+='<div class=\"muted\">Sem conclusões nesta semana.</div>';return}"
    "box.innerHTML+='<table class=\"md-table compact\"><thead><tr><th>Tarefa</th><th>Estado</th><th>Resultado</th></tr></thead><tbody>'"
    "+doneRows.map(r=>{const tid=String(r.task_id||'');const title=_mdEsc(r.title||tid||'—');const due=_mdDate(r.due_date);"
    "const st=_mdStateBadge(r.status||'');const rb=(r.done?'<span class=\"pill md-ok\">Concluída</span>':'<span class=\"pill md-open\">Aberta</span>');"
    "return `<tr data-tid=\"${_mdEsc(tid)}\" onclick=\"_mdOpenTask(this.dataset.tid)\" title=\"Abrir detalhe\">"
    "<td><b>${title}</b><div class=\"muted\">${_mdEsc(tid)} · ${_mdEsc(r.project||'Sem projeto')}</div></td>"
    "<td>${st}</td><td>${rb}<div class=\"muted\">Prazo: ${_mdEsc(due)}</div></td></tr>`}).join('')+'</tbody></table>'}"
    "function renderMyDay(j){const k=j?.kpis||{};_mdNum('md_k_total',k.my_tasks);_mdNum('md_k_overdue',k.overdue);"
    "_mdNum('md_k_blocked',k.blocked);_mdNum('md_k_due7',k.due_7d);_mdNum('md_k_progress',k.in_progress);_mdNum('md_k_highprio',k.high_priority_open);"
    "_mdSetGreeting();"
    "const sub=$('myday_sub');if(sub&&j?.generated_at)sub.textContent='Visão pessoal diária. Dados por utilizador autenticado. Atualizado às '+_mdEsc(j.generated_at);"
    "const box=$('md_immediate');if(!box)return;const rows=Array.isArray(j?.immediate)?j.immediate:[];"
    "if(!rows.length){box.innerHTML='<div class=\"muted\">Sem itens críticos neste momento.</div>';return}"
    "box.innerHTML='<table class=\"md-table\"><thead><tr><th>Tarefa</th><th>Estado</th><th>Prazo</th><th>Motivo</th></tr></thead><tbody>'"
    "+rows.map(r=>{const tid=String(r.task_id||'');const title=_mdEsc(r.title||tid||'—');const due=_mdDate(r.due_date);"
    "const reason=_mdEsc(r.reason||'Atenção');const st=_mdStateBadge(r.status||'');"
    "return `<tr data-tid=\"${_mdEsc(tid)}\" onclick=\"_mdOpenTask(this.dataset.tid)\" title=\"Abrir detalhe\">"
    "<td><b>${title}</b><div class=\"muted\">${_mdEsc(tid)} · ${_mdEsc(r.project||'Sem projeto')}</div></td>"
    "<td>${st}</td><td>${_mdEsc(due)}</td><td><span class=\"pill md-reason\">${reason}</span></td></tr>`}).join('')+'</tbody></table>'}"
    "async function loadMyDay(){try{const j=await api('/api/my-day/summary');renderMyDay(j);_mdRenderDue7(j);_mdRenderTopPrio(j);_mdRenderInvolved(j);_mdRenderDev(j);_mdRenderWeek(j);"
    "if(page==='myday')$('upd').textContent='Última atualização: '+new Date().toLocaleTimeString('pt-PT')}"
    "catch(e){toast(e.message,true);const box=$('md_immediate');if(box)box.innerHTML='<div class=\"muted\">Erro ao carregar.</div>';"
    "const box2=$('md_due7_list');if(box2)box2.innerHTML='<div class=\"muted\">Erro ao carregar.</div>';"
    "const box3=$('md_topprio_list');if(box3)box3.innerHTML='<div class=\"muted\">Erro ao carregar.</div>';"
    "const box4=$('md_involved_list');if(box4)box4.innerHTML='<div class=\"muted\">Erro ao carregar.</div>';"
    "const box5=$('md_dev_list');if(box5)box5.innerHTML='<div class=\"muted\">Erro ao carregar.</div>';"
    "const box6=$('md_week_list');if(box6)box6.innerHTML='<div class=\"muted\">Erro ao carregar.</div>'}}"
)

_MY_DAY_CSS = (
    "#page-myday .md-kpis{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin-bottom:12px}"
    "#page-myday .md-grid{display:grid;grid-template-columns:2fr 1fr 1fr;gap:12px;align-items:stretch}"
    "#page-myday .md-hero{padding:14px 16px;margin-bottom:10px;background:linear-gradient(180deg,#ffffff 0%,#f8fbff 100%)}"
    "#page-myday .md-hero-title h2{margin:0 0 3px;font-size:26px;line-height:1.1}"
    "#page-myday .md-kpi{align-items:flex-start;gap:10px}"
    "#page-myday .md-kpi .ico{width:30px;height:30px;display:flex;align-items:center;justify-content:center;border-radius:999px;background:#f1f5f9;border:1px solid #e2e8f0}"
    "#page-myday .md-kpi .v{font-size:24px;font-weight:700;line-height:1.1}"
    "#page-myday .md-link-btn{border:0;background:transparent;color:#0869d8;font-size:11.5px;font-weight:600;padding:0;cursor:pointer;margin-top:4px}"
    "#page-myday .md-link-btn:hover{text-decoration:underline;filter:brightness(.95)}"
    "#page-myday .md-head .md-link-btn{margin-top:0}"
    "#page-myday .md-card{padding:12px;min-height:190px;display:flex;flex-direction:column}"
    "#page-myday .md-card-main{min-height:320px}"
    "#page-myday .md-head{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:10px}"
    "#page-myday .md-head h3{margin:0;font-size:15px}"
    "#page-myday .pill{font-size:11px;padding:2px 8px;border-radius:999px;background:#dbeafe;color:#1d4ed8;border:1px solid #bfdbfe}"
    "#page-myday .pill.soon{background:#f1f5f9;border-color:#e2e8f0;color:#475569}"
    "#page-myday .md-placeholder{display:flex;align-items:center;justify-content:center;height:115px;border:1px dashed #cbd5e1;border-radius:9px;color:#64748b;background:#f8fafc}"
    "#page-myday .md-list{max-height:260px;overflow:auto;overflow-x:hidden;flex:1}"
    "#page-myday .md-card-main .md-list{max-height:320px}"
    "#page-myday .md-table{width:100%;border-collapse:collapse;font-size:11.5px;line-height:1.3;table-layout:fixed}"
    "#page-myday .md-table th,#page-myday .md-table td{padding:6px;border-bottom:1px solid #eef2f7;text-align:left;vertical-align:top;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
    "#page-myday .md-table th{font-size:11px;color:#475569;font-weight:700;background:#fcfdff;position:sticky;top:0;z-index:1}"
    "#page-myday .md-table tbody tr:nth-child(even){background:#fcfdff}"
    "#page-myday .md-table th:first-child,#page-myday .md-table td:first-child{width:52%;white-space:normal}"
    "#page-myday .md-table.compact th:first-child,#page-myday .md-table.compact td:first-child{width:60%}"
    "#page-myday .md-table.compact td:not(:first-child),#page-myday .md-table.compact th:not(:first-child){white-space:normal}"
    "#page-myday .md-dev-hero{display:flex;align-items:center;gap:10px;border:1px solid #e2e8f0;background:#f8fbff;border-radius:10px;padding:8px;margin-bottom:8px}"
    "#page-myday .md-dev-n{font-weight:700;color:#0f172a}"
    "#page-myday .md-donut{--p:0;position:relative;width:62px;height:62px;border-radius:999px;background:conic-gradient(#2563eb calc(var(--p)*1%),#e2e8f0 0);display:grid;place-items:center;flex:0 0 auto}"
    "#page-myday .md-donut::after{content:'';position:absolute;inset:7px;background:#fff;border-radius:999px}"
    "#page-myday .md-donut span{position:relative;z-index:1;font-size:11px;font-weight:700;color:#1e3a8a}"
    "#page-myday .md-week-kpis{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-bottom:8px}"
    "#page-myday .md-week-kpis>div{border:1px solid #e2e8f0;border-radius:9px;background:#f8fafc;padding:6px 8px}"
    "#page-myday .md-week-kpis b{display:block;font-size:10px;color:#64748b;font-weight:700}"
    "#page-myday .md-week-kpis span{display:block;font-size:16px;font-weight:700;color:#0f172a;line-height:1.2;margin-top:2px}"
    "#page-myday .md-table tbody tr{cursor:pointer}"
    "#page-myday .md-table tbody tr:hover{background:#f8fbff}"
    "#page-myday .md-state{font-size:11px;padding:2px 6px;border-radius:999px;background:#e2e8f0;color:#334155}"
    "#page-myday .md-state.warn{background:#fef3c7;color:#92400e}"
    "#page-myday .md-state.bad{background:#fee2e2;color:#b91c1c}"
    "#page-myday .md-reason{background:#fff7ed;border-color:#fed7aa;color:#9a3412}"
    "#page-myday .md-due{background:#eff6ff;border-color:#dbeafe;color:#1d4ed8}"
    "#page-myday .md-due.warn{background:#fef3c7;border-color:#fde68a;color:#92400e}"
    "#page-myday .md-due.bad{background:#fee2e2;border-color:#fecaca;color:#b91c1c}"
    "#page-myday .md-prio{background:#eff6ff;border-color:#dbeafe;color:#1d4ed8}"
    "#page-myday .md-prio.warn{background:#fef3c7;border-color:#fde68a;color:#92400e}"
    "#page-myday .md-prio.bad{background:#fee2e2;border-color:#fecaca;color:#b91c1c}"
    "#page-myday .md-ok{background:#dcfce7;border-color:#bbf7d0;color:#166534}"
    "#page-myday .md-open{background:#eff6ff;border-color:#dbeafe;color:#1d4ed8}"
    "#page-myday .md-quick{margin-top:12px;padding:12px}"
    "#page-myday .md-quick-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px}"
    "#page-myday .md-quick .btn{font-size:12px}"
    "@media(max-width:1600px){#page-myday .md-kpis{grid-template-columns:repeat(3,minmax(0,1fr))}#page-myday .md-grid{grid-template-columns:1fr 1fr}}"
    "@media(max-width:1600px){#page-myday .md-quick-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}"
    "@media(max-width:1100px){#page-myday .md-kpis{grid-template-columns:repeat(2,minmax(0,1fr))}#page-myday .md-grid{grid-template-columns:1fr}#page-myday .md-card-main{min-height:280px}#page-myday .md-hero-title h2{font-size:22px}#page-myday .md-quick-grid{grid-template-columns:repeat(2,minmax(0,1fr))}#page-myday .md-week-kpis{grid-template-columns:1fr}}"
)


def _patch_html_my_day(html: str) -> str:
    html = html.replace(
        '<button type="button" data-page="notes" id="nav-notes" onclick="showPage(\'notes\')">'
        '<span>📝</span><span class="txt">Notas</span></button>',
        '<button type="button" data-page="myday" id="nav-myday" onclick="showPage(\'myday\')">'
        '<span>🎯</span><span class="txt">O Meu Dia</span></button>'
        '<button type="button" data-page="notes" id="nav-notes" onclick="showPage(\'notes\')">'
        '<span>📝</span><span class="txt">Notas</span></button>',
        1,
    )
    html = html.replace('<div id="page-system" class="page">', _MY_DAY_PAGE + '<div id="page-system" class="page">', 1)
    html = html.replace(
        "const ids=['home','ach','tasks','task-detail','dashboard','board','project','scheduled','notes','admin','system','shortcuts','contacts','machines'];",
        "const ids=['home','ach','tasks','task-detail','dashboard','board','project','scheduled','myday','notes','admin','system','shortcuts','contacts','machines'];",
        1,
    )
    html = html.replace(
        "else if(p==='notes')loadNotes().catch(e=>toast(e.message,true));"
        "else if(p==='admin')loadAdminCenter().catch(e=>toast(e.message,true));"
        "else if(p==='system')loadSystem().catch(e=>toast(e.message,true));",
        "else if(p==='myday')loadMyDay().catch(e=>toast(e.message,true));"
        "else if(p==='notes')loadNotes().catch(e=>toast(e.message,true));"
        "else if(p==='admin')loadAdminCenter().catch(e=>toast(e.message,true));"
        "else if(p==='system')loadSystem().catch(e=>toast(e.message,true));",
        1,
    )
    marker = "async function loadSystem(){"
    if marker not in html:
        marker = "function loadSystem(){"
    if "function loadMyDay()" not in html:
        html = html.replace(marker, _MY_DAY_JS + marker, 1)
    return html


def _patch_html_machines(html: str) -> str:
    if ".mc-badge{" not in html:
        html = html.replace(
            "</style>",
            ".mc-badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600}"
            ".mc-badge.ok{background:#e8f7ee;color:#146c2e}"
            ".mc-badge.warn{background:#fff4e5;color:#9a5b00}"
            ".mc-badge.muted{background:#eef1f6;color:#5b6472}"
            ".mc-field-row{border:1px solid var(--line,#e5e9f0);border-radius:10px;padding:10px 12px;margin-bottom:10px}"
            ".mc-field-actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}"
            ".mc-field-row label{font-size:12px;font-weight:600;color:var(--muted,#5b6472)}"
            ".mc-field-val{margin-top:4px;word-break:break-word}"
            "</style>",
            1,
        )
    html = html.replace(
        "<thead><tr><th>Nome</th><th>Código</th><th>Área</th><th>Localização</th></tr></thead><tbody id=\"mc_rows\">",
        "<thead><tr><th>Nome</th><th>Código</th><th>Área</th><th>Responsável</th><th>Status</th></tr></thead><tbody id=\"mc_rows\">",
        1,
    )
    html = html.replace(
        '<div class="field"><label>Localização</label><input id="mc_f_location"></div><div class="field"><label>Manuais</label><input id="mc_f_manuals"></div>'
        '<div class="field"><label>Esquemas</label><input id="mc_f_schematics"></div><div class="field"><label>Spares</label><input id="mc_f_spares"></div>'
        '<div class="field"><label>Manutenção</label><input id="mc_f_maintenance"></div><div class="field"><label>Pasta</label><input id="mc_f_folder"></div>'
        '<div class="field"><label>Notas</label><textarea id="mc_f_notes" rows="2"></textarea></div>',
        '<div class="field"><label>Localização</label><input id="mc_f_location"></div>'
        '<div class="field"><label>Responsável</label><input id="mc_f_responsible" list="mc_pessoal_dl" placeholder="Lista Pessoal"></div>'
        '<datalist id="mc_pessoal_dl"></datalist>'
        '<div class="field"><label>Manuais</label><input id="mc_f_manuals" placeholder="URL ou N/A"></div>'
        '<div class="field"><label>Esquemas</label><input id="mc_f_schematics" placeholder="URL ou N/A"></div>'
        '<div class="field"><label>Spares</label><input id="mc_f_spares" placeholder="URL ou N/A"></div>'
        '<div class="field"><label>Localização 3D</label><input id="mc_f_loc_3d" placeholder="URL ou N/A"></div>'
        '<div class="field"><label>Placa da máquina</label><input id="mc_f_machine_plate" placeholder="URL ou N/A"></div>'
        '<div class="field"><label>Declaração de conformidade</label><input id="mc_f_conformity_decl" placeholder="URL ou N/A"></div>'
        '<div class="field"><label>Pasta</label><input id="mc_f_folder" placeholder="URL ou N/A"></div>'
        '<div class="field"><label>Notas</label><textarea id="mc_f_notes" rows="2" placeholder="URL ou N/A"></textarea></div>',
        1,
    )
    _old_mc = (
        "async function loadMachines(){try{let j=await api('/api/machines');_mcRows=j.rows||[];_mcSel=null;mcCancelForm();renderMachines()}catch(e){toast(e.message,true)}}"
        "function renderMachines(){const q=($('mc_q')?.value||'').toLowerCase();const tb=$('mc_rows');if(!tb)return;tb.innerHTML='';"
        "_mcRows.filter(r=>!q||[r.name,r.code,r.area,r.location].join(' ').toLowerCase().includes(q)).forEach(r=>{const tr=document.createElement('tr');"
        "if(_mcSel===r.machine_id)tr.className='sel';tr.onclick=()=>{_mcSel=r.machine_id;renderMachines();renderMachineDetail()};"
        "tr.innerHTML=`<td><b>${esc(r.name)}</b></td><td>${esc(r.code)}</td><td>${esc(r.area)}</td><td>${esc(r.location)}</td>`;tb.appendChild(tr)});renderMachineDetail()}"
        "function renderMachineDetail(){const r=_mcRows.find(x=>x.machine_id===_mcSel);const box=$('mc_det'),title=$('mc_det_title'),btn=$('mc_open_folder');"
        "const ce=canEditCatalog();if(!box)return;if(!r){box.innerHTML='<p class=\"muted\">Selecione uma máquina</p>';if(title)title.textContent='Detalhe';"
        "if(btn)btn.disabled=true;if($('mc_edit'))$('mc_edit').disabled=true;if($('mc_del'))$('mc_del').disabled=true;return}"
        "if(title)title.textContent=r.name||'Detalhe';box.innerHTML=[['Código',r.code],['Área',r.area],['Localização',r.location],"
        "['Manuais',r.manuals],['Esquemas',r.schematics],['Spares',r.spares],['Manutenção',r.maintenance],['Pasta',r.folder],['Notas',r.notes]]"
        ".map(([l,v])=>`<div><label>${esc(l)}</label><div>${esc(v||'—')}</div></div>`).join('');"
        "if(btn)btn.disabled=!r.folder;if($('mc_edit'))$('mc_edit').disabled=!ce;if($('mc_del'))$('mc_del').disabled=!mcCanDelete()}"
        "function mcCancelForm(){const f=$('mc_form');if(f)f.style.display='none'}"
        "function mcFillForm(r){$('mc_f_id').value=r?.machine_id||'';$('mc_f_name').value=r?.name||'';$('mc_f_code').value=r?.code||'';"
        "$('mc_f_area').value=r?.area||'';$('mc_f_location').value=r?.location||'';$('mc_f_manuals').value=r?.manuals||'';"
        "$('mc_f_schematics').value=r?.schematics||'';$('mc_f_spares').value=r?.spares||'';$('mc_f_maintenance').value=r?.maintenance||'';"
        "$('mc_f_folder').value=r?.folder||'';$('mc_f_notes').value=r?.notes||''}"
        "function mcNew(){if(!canEditCatalog())return;mcFillForm(null);$('mc_form').style.display='block'}"
        "function mcEditSel(){const r=_mcRows.find(x=>x.machine_id===_mcSel);if(!r||!canEditCatalog())return;mcFillForm(r);$('mc_form').style.display='block'}"
        "async function mcSave(){try{if(!canEditCatalog())return;const p={name:$('mc_f_name').value,code:$('mc_f_code').value,area:$('mc_f_area').value,"
        "location:$('mc_f_location').value,manuals:$('mc_f_manuals').value,schematics:$('mc_f_schematics').value,spares:$('mc_f_spares').value,"
        "maintenance:$('mc_f_maintenance').value,folder:$('mc_f_folder').value,notes:$('mc_f_notes').value};const id=($('mc_f_id').value||'').trim();"
        "if(id){await api('/api/machines/'+encodeURIComponent(id),{method:'PUT',body:JSON.stringify(p)})}else{let j=await api('/api/machines',{method:'POST',body:JSON.stringify(p)});"
        "_mcSel=j.machine_id}toast('Máquina guardada');mcCancelForm();loadMachines()}catch(e){toast(e.message,true)}}"
        "async function mcDelSel(){try{if(!_mcSel||!mcCanDelete()||!confirm('Apagar ficha de máquina?'))return;"
        "await api('/api/machines/'+encodeURIComponent(_mcSel)+'/delete',{method:'POST',body:'{}'});_mcSel=null;toast('Máquina apagada');loadMachines()}catch(e){toast(e.message,true)}}"
        "async function openMachineFolder(){const r=_mcRows.find(x=>x.machine_id===_mcSel);if(r&&r.folder)await openShortcutRow({target:r.folder})}"
    )
    _new_mc = (
        "let _mcPessoal=[];const _MC_FIELDS=['manuals','schematics','spares','folder','notes','loc_3d','machine_plate','conformity_decl'];"
        "const _MC_LABELS={manuals:'Manuais',schematics:'Esquemas',spares:'Spares',folder:'Pasta',notes:'Notas',loc_3d:'Localização 3D',"
        "machine_plate:'Placa da máquina',conformity_decl:'Declaração de conformidade'};"
        "function mcStatusLbl(s){return s==='validado'?'Validado':'Em validação'}"
        "function mcFieldStatusLbl(s){return s==='validated'?'Validado':(s==='pending'?'Pendente':'Rascunho')}"
        "function mcFieldBadge(st){const s=(st&&st.status)||'draft';const cls=s==='validated'?'ok':(s==='pending'?'warn':'muted');"
        "return `<span class=\"mc-badge ${cls}\">${esc(mcFieldStatusLbl(s))}</span>`}"
        "function mcCanSubmit(r){if(!r||!user)return false;if(user.role==='admin')return true;const resp=String(r.responsible||'').trim().toLowerCase();"
        "if(!resp)return false;const dn=String(user.display_name||'').trim().toLowerCase();const un=String(user.username||'').trim().toLowerCase();"
        "return dn===resp||un===resp}"
        "function mcCanDelete(){return !!(user&&user.role==='admin')}"
        "function mcIsLink(v){return /^https?:\\/\\//i.test(String(v||'').trim())}"
        "function mcValHtml(v){const s=String(v||'').trim();if(!s)return '—';if(mcIsLink(s))return `<a href=\"${esc(s)}\" target=\"_blank\" rel=\"noopener\">${esc(s)}</a>`;return esc(s)}"
        "async function mcLoadPessoal(){try{const j=await api('/api/lookups/tasks');_mcPessoal=j.pessoal||j.users||[]}"
        "catch(e){try{const j2=await api('/api/lookups');_mcPessoal=j2.pessoal||[]}catch(e2){_mcPessoal=[]}}}"
        "function mcFillPessoalDatalist(){const dl=$('mc_pessoal_dl');if(!dl)return;dl.innerHTML='';(_mcPessoal||[]).forEach(p=>{const o=document.createElement('option');"
        "o.value=String(p);dl.appendChild(o)})}"
        "async function loadMachines(){try{await mcLoadPessoal();mcFillPessoalDatalist();let j=await api('/api/machines');_mcRows=j.rows||[];_mcSel=null;"
        "mcCancelForm();if($('mc_del'))$('mc_del').style.display=mcCanDelete()?'inline-block':'none';renderMachines()}catch(e){toast(e.message,true)}}"
        "function renderMachines(){const q=($('mc_q')?.value||'').toLowerCase();const tb=$('mc_rows');if(!tb)return;tb.innerHTML='';"
        "_mcRows.filter(r=>!q||[r.name,r.code,r.area,r.location,r.responsible,r.machine_status].join(' ').toLowerCase().includes(q)).forEach(r=>{"
        "const tr=document.createElement('tr');if(_mcSel===r.machine_id)tr.className='sel';tr.onclick=()=>{_mcSel=r.machine_id;renderMachines();renderMachineDetail()};"
        "const stCls=r.machine_status==='validado'?'ok':'warn';tr.innerHTML=`<td><b>${esc(r.name)}</b></td><td>${esc(r.code)}</td><td>${esc(r.area)}</td>"
        "<td>${esc(r.responsible||'—')}</td><td><span class=\"mc-badge ${stCls}\">${esc(mcStatusLbl(r.machine_status))}</span></td>`;tb.appendChild(tr)});renderMachineDetail()}"
        "function mcFieldActions(r,field){const st=(r.field_states&&r.field_states[field])||{status:'draft'};const s=st.status||'draft';let h='';"
        "if(s==='draft'&&mcCanSubmit(r)&&String(r[field]||'').trim())h+=`<button type=\"button\" class=\"btn\" onclick=\"mcSubmitField('${field}')\">Submeter</button>`;"
        "if(s==='pending'&&user&&user.role==='admin')h+=`<button type=\"button\" class=\"btn primary\" onclick=\"mcApproveField('${field}')\">Aprovar</button>`;"
        "if((s==='pending'||s==='validated')&&user&&user.role==='admin')h+=`<button type=\"button\" class=\"btn\" onclick=\"mcRevertField('${field}')\">Reverter p/ rascunho</button>`;"
        "return h}"
        "function renderMachineDetail(){const r=_mcRows.find(x=>x.machine_id===_mcSel);const box=$('mc_det'),title=$('mc_det_title'),btn=$('mc_open_folder');"
        "const ce=canEditCatalog();if(!box)return;if(!r){box.innerHTML='<p class=\"muted\">Selecione uma máquina</p>';if(title)title.textContent='Detalhe';"
        "if(btn)btn.disabled=true;if($('mc_edit'))$('mc_edit').disabled=true;if($('mc_del'))$('mc_del').disabled=true;return}"
        "if(title)title.textContent=r.name||'Detalhe';let html='<div class=\"meta\"><div><label>Código</label><div>'+esc(r.code||'—')+'</div></div>"
        "<div><label>Área</label><div>'+esc(r.area||'—')+'</div></div><div><label>Localização</label><div>'+esc(r.location||'—')+'</div></div>"
        "<div><label>Responsável</label><div>'+esc(r.responsible||'—')+'</div></div>"
        "<div><label>Status geral</label><div><span class=\"mc-badge '+(r.machine_status==='validado'?'ok':'warn')+'\">'+esc(mcStatusLbl(r.machine_status))+'</span></div></div></div>';"
        "_MC_FIELDS.forEach(f=>{const lbl=_MC_LABELS[f]||f;const st=(r.field_states&&r.field_states[f])||{status:'draft'};"
        "html+=`<div class=\"mc-field-row\"><div style=\"display:flex;justify-content:space-between;gap:8px;align-items:center\">"
        "<label>${esc(lbl)}</label>${mcFieldBadge(st)}</div><div class=\"mc-field-val\">${mcValHtml(r[f])}</div>"
        "<div class=\"mc-field-actions\">${mcFieldActions(r,f)}</div></div>`});box.innerHTML=html;"
        "if(btn)btn.disabled=!r.folder;if($('mc_edit'))$('mc_edit').disabled=!ce;if($('mc_del'))$('mc_del').disabled=!mcCanDelete()}"
        "function mcCancelForm(){const f=$('mc_form');if(f)f.style.display='none'}"
        "function mcFillForm(r){$('mc_f_id').value=r?.machine_id||'';$('mc_f_name').value=r?.name||'';$('mc_f_code').value=r?.code||'';"
        "$('mc_f_area').value=r?.area||'';$('mc_f_location').value=r?.location||'';$('mc_f_responsible').value=r?.responsible||'';"
        "$('mc_f_manuals').value=r?.manuals||'';$('mc_f_schematics').value=r?.schematics||'';$('mc_f_spares').value=r?.spares||'';"
        "$('mc_f_loc_3d').value=r?.loc_3d||'';$('mc_f_machine_plate').value=r?.machine_plate||'';$('mc_f_conformity_decl').value=r?.conformity_decl||'';"
        "$('mc_f_folder').value=r?.folder||'';$('mc_f_notes').value=r?.notes||''}"
        "function mcNew(){if(!canEditCatalog())return;mcFillForm(null);$('mc_form').style.display='block'}"
        "function mcEditSel(){const r=_mcRows.find(x=>x.machine_id===_mcSel);if(!r||!canEditCatalog())return;mcFillForm(r);$('mc_form').style.display='block'}"
        "async function mcSave(){try{if(!canEditCatalog())return;const p={name:$('mc_f_name').value,code:$('mc_f_code').value,area:$('mc_f_area').value,"
        "location:$('mc_f_location').value,responsible:$('mc_f_responsible').value,manuals:$('mc_f_manuals').value,schematics:$('mc_f_schematics').value,"
        "spares:$('mc_f_spares').value,loc_3d:$('mc_f_loc_3d').value,machine_plate:$('mc_f_machine_plate').value,conformity_decl:$('mc_f_conformity_decl').value,"
        "folder:$('mc_f_folder').value,notes:$('mc_f_notes').value};const id=($('mc_f_id').value||'').trim();"
        "if(id){await api('/api/machines/'+encodeURIComponent(id),{method:'PUT',body:JSON.stringify(p)})}else{let j=await api('/api/machines',{method:'POST',body:JSON.stringify(p)});"
        "_mcSel=j.machine_id}toast('Máquina guardada');mcCancelForm();loadMachines()}catch(e){toast(e.message,true)}}"
        "async function mcSubmitField(field){try{if(!_mcSel)return;await api('/api/machines/'+encodeURIComponent(_mcSel)+'/fields/'+encodeURIComponent(field)+'/submit',{method:'POST',body:'{}'});"
        "toast('Submetido para aprovação');loadMachines()}catch(e){toast(e.message,true)}}"
        "async function mcApproveField(field){try{if(!_mcSel)return;await api('/api/machines/'+encodeURIComponent(_mcSel)+'/fields/'+encodeURIComponent(field)+'/approve',{method:'POST',body:'{}'});"
        "toast('Campo validado');loadMachines()}catch(e){toast(e.message,true)}}"
        "async function mcRevertField(field){try{if(!_mcSel||!confirm('Reverter campo para rascunho?'))return;"
        "await api('/api/machines/'+encodeURIComponent(_mcSel)+'/fields/'+encodeURIComponent(field)+'/revert',{method:'POST',body:'{}'});"
        "toast('Campo em rascunho');loadMachines()}catch(e){toast(e.message,true)}}"
        "async function mcDelSel(){try{if(!_mcSel||!mcCanDelete()||!confirm('Apagar ficha de máquina?'))return;"
        "await api('/api/machines/'+encodeURIComponent(_mcSel)+'/delete',{method:'POST',body:'{}'});_mcSel=null;toast('Máquina apagada');loadMachines()}catch(e){toast(e.message,true)}}"
        "async function openMachineFolder(){const r=_mcRows.find(x=>x.machine_id===_mcSel);if(r&&r.folder)await openShortcutRow({target:r.folder})}"
    )
    if _old_mc in html:
        html = html.replace(_old_mc, _new_mc, 1)
    else:
        _mc_start = html.find("async function loadMachines()")
        _mc_end = html.find("async function loadAll()")
        if _mc_start >= 0 and _mc_end > _mc_start:
            html = html[:_mc_start] + _new_mc + html[_mc_end:]
    return html


def _patch_html_notes(html: str) -> str:
    html = html.replace(
        '<button type="button" data-page="system" id="nav-system"',
        '<button type="button" data-page="admin" id="nav-admin" style="display:none" onclick="showPage(\'admin\')">'
        '<span>🛠️</span><span class="txt">Admin</span></button>'
        '<button type="button" data-page="system" id="nav-system"',
        1,
    )
    html = html.replace(
        '<button type="button" data-page="system" id="nav-system"',
        '<button type="button" data-page="notes" id="nav-notes" onclick="showPage(\'notes\')">'
        '<span>📝</span><span class="txt">Notas</span></button>'
        '<button type="button" data-page="system" id="nav-system"',
        1,
    )
    html = html.replace('<div id="page-system" class="page">', _NOTES_PAGE + _ADMIN_CENTER_PAGE + '<div id="page-system" class="page">', 1)
    html = html.replace(
        "const ids=['home','ach','tasks','task-detail','dashboard','board','project','scheduled','system','shortcuts','contacts','machines'];",
        "const ids=['home','ach','tasks','task-detail','dashboard','board','project','scheduled','notes','admin','system','shortcuts','contacts','machines'];",
        1,
    )
    html = html.replace(
        "else if(p==='system')loadSystem().catch(e=>toast(e.message,true));",
        "else if(p==='notes')loadNotes().catch(e=>toast(e.message,true));"
        "else if(p==='admin')loadAdminCenter().catch(e=>toast(e.message,true));"
        "else if(p==='system')loadSystem().catch(e=>toast(e.message,true));",
        1,
    )
    marker = "async function loadSystem(){"
    if marker not in html:
        marker = "function loadSystem(){"
    html = html.replace(marker, _NOTES_JS + _ADMIN_CENTER_JS + marker, 1)
    return _patch_html_init(html)


_HOME_KPIS_SECTION = (
    '<section class="kpis"><div class="kpi kpi-click" onclick="homeGoTasks()" title="Abrir tarefas">'
    '<div class="ico">📋</div><div><div class="muted">Tarefas abertas</div><div class="v" id="hk_open">0</div></div></div>'
    '<div class="kpi kpi-click" onclick="homeGoTasks(\'overdue\')" title="Ver atrasadas">'
    '<div class="ico">⏰</div><div><div class="muted">Atrasadas</div><div class="v" id="hk_overdue">0</div></div></div>'
    '<div class="kpi kpi-click" onclick="showPage(\'ach\')" title="Abrir conquistas">'
    '<div class="ico">🏆</div><div><div class="muted">Conquistas</div><div class="v" id="hk_ach">0</div></div></div>'
    '<div class="kpi"><div class="ico">✅</div><div><div class="muted">Validadas</div><div class="v" id="hk_valid">0</div></div></div>'
    '<div class="kpi"><div class="ico">📈</div><div><div class="muted">Impacto €</div><div class="v" id="hk_impact">€0</div></div></div>'
    '<div class="kpi kpi-click" onclick="schedOpenPending()" title="Programadas pendentes">'
    '<div class="ico">📅</div><div><div class="muted">Prog. pendentes</div><div class="v" id="hk_sched">0</div></div></div>'
    "</section>"
)


def _patch_html_init(html: str) -> str:
    html = html.replace(_HOME_KPIS_SECTION, "", 1)
    html = html.replace(
        "function loadHome(){try{let j=await api('/api/home/summary');$('home_greeting').textContent='Bem-vindo, '+(user.display_name||user.username||'Utilizador')+' 👋';const tk=j.tasks||{};$('hk_open').textContent=tk.open??0;$('hk_overdue').textContent=tk.overdue??0;const ak=j.achievements||{};$('hk_ach').textContent=ak.total??0;$('hk_valid').textContent=ak.validated??0;$('hk_impact').textContent=money(ak.impact_total||0);const sk=j.scheduled||{};if($('hk_sched'))$('hk_sched').textContent=sk.pending??0;if(page==='home')$('upd').textContent='Última atualização: '+new Date().toLocaleTimeString('pt-PT')}catch(e){toast(e.message,true)}}",
        "function loadHome(){try{const g=$('home_greeting');if(g)g.textContent='Bem-vindo, '+(user.display_name||user.username||'Utilizador')+' 👋';if(page==='home'&&$('upd'))$('upd').textContent='Última atualização: '+new Date().toLocaleTimeString('pt-PT')}catch(e){toast(e.message,true)}}",
        1,
    )
    html = html.replace(
        "init().catch(e=>toast(e.message,true));",
        "function _startApp(){init().catch(e=>{try{if(typeof loadingHide==='function')loadingHide()}catch(_){ }toast(e.message||String(e),true);});}"
        "if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',_startApp);"
        "else _startApp();",
        1,
    )
    # Tile Notas na home
    html = html.replace(
        "{icon:'📅',title:'Programadas',desc:'Templates e recorrência',page:'scheduled',on:true},",
        "{icon:'📝',title:'Notas',desc:'Notas tecnicas por utilizador',page:'notes',on:true},"
        "{icon:'📅',title:'Programadas',desc:'Templates e recorrência',page:'scheduled',on:true},",
        1,
    )
    return html


_FOLDER_SECTION = (
    '<section class="sec"><h3>Pasta</h3>'
    '<div class="ro" id="td_folder_view">—</div>'
    '<div id="td_folder_edit" style="display:none">'
    '<textarea id="td_pasta_edit" style="width:100%;min-height:52px;border:1px solid #d7dde8;'
    'border-radius:9px;padding:10px;font:inherit" placeholder="tasks_files/Task_..."></textarea>'
    '<div class="toolbar" style="margin-top:8px;flex-wrap:wrap">'
    '<button type="button" class="btn" onclick="detailCreateFolder()">Criar pasta</button>'
    '<button type="button" class="btn" onclick="openTaskFolderFromDetail()">Abrir pasta</button>'
    '<button type="button" class="btn" onclick="openOnedriveRoot()">Raiz OneDrive</button>'
    '<button type="button" class="btn" onclick="pickOnedriveRoot()">Configurar OneDrive</button>'
    '<span class="muted" id="td_folder_status" style="align-self:center;padding-left:6px"></span>'
    "</div></div></section>"
)

_FOLDER_JS = (
    "async function detailCreateFolder(){try{if(!_detailTid)return;"
    "let j=await api('/api/tasks/'+encodeURIComponent(_detailTid)+'/folder/create',{method:'POST',body:'{}'});"
    "if(j.rel&&$('td_pasta_edit'))$('td_pasta_edit').value=j.rel;"
    "toast(j.message||'Pasta criada');detailRefreshFolderStatus()}"
    "catch(e){toast(e.message,true)}}"
    "async function detailRefreshFolderStatus(){const st=$('td_folder_status');if(!st||!_detailTid)return;"
    "try{let j=await api('/api/tasks/'+encodeURIComponent(_detailTid)+'/folder/info');"
    "if(j.rel&&$('td_pasta_edit')&&!$('td_pasta_edit').value.trim())$('td_pasta_edit').value=j.rel;"
    "st.textContent=j.exists?'Pasta OK':'Pasta ainda nao criada';"
    "st.style.color=j.exists?'#166534':'#92400e'}catch(e){st.textContent=''}}"
    "function renderDetailFolder(t,fo,edit){const v=$('td_folder_view'),box=$('td_folder_edit');if(!v)return;"
    "const p=t.Pasta||fo.rel||'',ex=!!fo.exists;"
    "if(edit&&box){v.style.display='none';box.style.display='block';"
    "if($('td_pasta_edit'))$('td_pasta_edit').value=p;detailRefreshFolderStatus()}"
    "else{if(box)box.style.display='none';v.style.display='block';"
    "v.textContent=(p||'—')+(ex?' · OK':' · nao criada')}}"
)

_TASK_COL_PREFS_JS = (
    "const _TASK_COL_LABELS={TaskID:'TaskID',Tarefa:'Tarefa',DescricaoNotas:'Descrição',Milestone:'Milestone',Assunto:'Assunto',"
    "DataRegisto:'Registo',InicioPrevisto:'Início',Responsavel:'Responsável',Workers:'Workers',Estado:'Estado',Prioridade:'Prio.',"
    "Notificacoes:'Notificações',NotifEmoji:'Notif.',Prazo:'Prazo',DataConclusao:'Conclusão',Projeto:'Projeto',Linha:'Linha',Maquina:'Máquina',Pasta:'Pasta',"
    "ResultadoInicial:'Resultado inicial',ResultadoFinal:'Resultado final',Links:'Links'};"
    "const _TASK_COL_DEFAULT=['TaskID','Tarefa','NotifEmoji','Notificacoes','Milestone','Assunto','DataRegisto','Prazo','Responsavel','Workers','Estado','DataConclusao','Prioridade'];"
    "let _taskColsAll=[..._TASK_COL_DEFAULT],_taskColsVisible=[..._TASK_COL_DEFAULT],_taskColsReady=false,_taskColWidths={},_taskColResize=null,_taskColSaveTimer=null,_taskColMeasureEl=null;"
    "const _TASK_COL_FLEX=new Set(['Tarefa','Assunto','DescricaoNotas','Milestone','Workers','Notificacoes','Projeto','Linha','Pasta','Links','ResultadoInicial','ResultadoFinal']);"
    "function _taskColLabel(c){return _TASK_COL_LABELS[c]||String(c||'')}"
    "function _taskColsNormalize(cols){const base=Array.isArray(_taskColsAll)&&_taskColsAll.length?_taskColsAll:_TASK_COL_DEFAULT;"
    "const out=[];(Array.isArray(cols)?cols:[]).forEach(c=>{const k=String(c||'').trim();if(k&&base.includes(k)&&!out.includes(k))out.push(k)});"
    "return out.length?out:[...base]}"
    "function _taskColSortable(c){return ['TaskID','Tarefa','Milestone','Assunto','DataRegisto','Prazo','DataConclusao','Responsavel','Estado','Prioridade','Notificacoes','InicioPrevisto','Projeto','Linha','Maquina'].includes(c)}"
    "async function taskColQuickFilter(c,ev){try{if(ev){ev.preventDefault();ev.stopPropagation()}await openExcelFiltersModal();"
    "if($('ef_col'))$('ef_col').value=String(c||'');if(typeof syncExcelDatePanel==='function')syncExcelDatePanel();"
    "if(typeof loadExcelFilterValues==='function')await loadExcelFilterValues()}catch(e){toast(e.message,true)}}"
    "function taskColsRenderHeader(){const tb=$('trows');if(!tb)return;const table=tb.closest('table');const tr=table?.querySelector('thead tr');if(!tr)return;"
    "const cols=_taskColsNormalize(_taskColsVisible);tr.innerHTML=cols.map(c=>_taskColThHtml(c)).join('');_taskColsApplyLayout()}"
    "async function taskColsEnsureLoaded(){if(_taskColsReady){taskColsRenderHeader();return}"
    "try{let j=await api('/api/tasks/columns/prefs');const all=Array.isArray(j.available_columns)&&j.available_columns.length?j.available_columns:_TASK_COL_DEFAULT;"
    "_taskColsAll=[...new Set(all.map(x=>String(x||'').trim()).filter(x=>x))];_taskColsVisible=_taskColsNormalize(j.columns);"
    "_taskColWidths=_taskColNormalizeWidths(j.widths,_taskColsVisible)}"
    "catch(_){_taskColsAll=[..._TASK_COL_DEFAULT];_taskColsVisible=[..._TASK_COL_DEFAULT]}"
    "_taskColsReady=true;taskColsRenderHeader()}"
    "function _taskDate(v){return esc(String(v||'').slice(0,10))}"
    "function _taskColCell(r,c){if(c==='TaskID'){const prv=(r&&((r.Private??r.private)))||0;const priv=n0(prv)?'🔒 ':'';return `<td data-col=\"${c}\" title=\"${esc(r.TaskID)}\">${priv}${esc(fmtTid(r.TaskID))}</td>`}"
    "if(c==='Tarefa')return `<td data-col=\"${c}\"><b>${esc(r.Tarefa||'')}</b></td>`;"
    "if(c==='Estado')return `<td data-col=\"${c}\" class=\"status-cell\" title=\"Clique para mudar estado\">${teBadge(r.Estado)}</td>`;"
    "if(c==='Prioridade')return `<td data-col=\"${c}\">${tpBadge(r.Prioridade)}</td>`;"
    "if(c==='DataRegisto'||c==='Prazo'||c==='InicioPrevisto'||c==='DataConclusao')return `<td data-col=\"${c}\">${_taskDate(r[c])}</td>`;"
    "return `<td data-col=\"${c}\">${esc((r&&r[c]!=null)?r[c]:'')}</td>`}"
    "function _taskColsEnsureModal(){if($('task-cols-modal'))return;"
    "document.body.insertAdjacentHTML('beforeend','<div class=\"modal-bg\" id=\"task-cols-modal\" style=\"display:none\">"
    "<div class=\"modal\" style=\"width:min(880px,94vw)\"><div class=\"mh\"><h3>Personalizar colunas (SQL)</h3>"
    "<button class=\"btn\" type=\"button\" onclick=\"closeTaskColsModal()\">✕</button></div>"
    "<div class=\"mc\" style=\"display:grid;grid-template-columns:1fr auto 1fr;gap:10px\">"
    "<div><div class=\"muted\" style=\"margin-bottom:6px\">Visíveis (ordem)</div><select id=\"tc_vis\" size=\"14\" style=\"width:100%\"></select></div>"
    "<div style=\"display:flex;flex-direction:column;gap:6px;justify-content:center\">"
    "<button class=\"btn\" type=\"button\" onclick=\"taskColsMoveToHidden()\">→</button>"
    "<button class=\"btn\" type=\"button\" onclick=\"taskColsMoveToVisible()\">←</button>"
    "<button class=\"btn\" type=\"button\" onclick=\"taskColsMoveUp()\">▲</button>"
    "<button class=\"btn\" type=\"button\" onclick=\"taskColsMoveDown()\">▼</button></div>"
    "<div><div class=\"muted\" style=\"margin-bottom:6px\">Ocultas</div><select id=\"tc_hid\" size=\"14\" style=\"width:100%\"></select></div>"
    "</div><div class=\"mf\"><button class=\"btn\" type=\"button\" onclick=\"taskColsResetDefault()\">Restaurar padrão</button>"
    "<button class=\"btn\" type=\"button\" onclick=\"closeTaskColsModal()\">Cancelar</button>"
    "<button class=\"btn primary\" type=\"button\" onclick=\"saveTaskColsModal()\">Guardar</button></div></div></div>')}"
    "function _tcOpt(v){return `<option value=\"${esc(v)}\">${esc(_taskColLabel(v))}</option>`}"
    "function _tcRead(id){return Array.from($(id)?.options||[]).map(o=>o.value)}"
    "function _tcFill(){const vis=$('tc_vis'),hid=$('tc_hid');if(!vis||!hid)return;const v=_taskColsNormalize(_taskColsVisible);"
    "const h=(_taskColsAll||[]).filter(c=>!v.includes(c));vis.innerHTML=v.map(_tcOpt).join('');hid.innerHTML=h.map(_tcOpt).join('')}"
    "function openTaskColsModal(){_taskColsEnsureModal();_tcFill();$('task-cols-modal').style.display='flex'}"
    "function closeTaskColsModal(){const m=$('task-cols-modal');if(m)m.style.display='none'}"
    "function _tcMove(from,to){const a=$(from),b=$(to);if(!a||!b)return;const i=a.selectedIndex;if(i<0)return;const o=a.options[i];"
    "b.add(new Option(o.text,o.value));a.remove(i);if(a.options.length)a.selectedIndex=Math.min(i,a.options.length-1)}"
    "function taskColsMoveToHidden(){_tcMove('tc_vis','tc_hid')}"
    "function taskColsMoveToVisible(){_tcMove('tc_hid','tc_vis')}"
    "function taskColsMoveUp(){const s=$('tc_vis');if(!s)return;const i=s.selectedIndex;if(i<=0)return;const o=s.options[i];s.remove(i);s.add(o,i-1);s.selectedIndex=i-1}"
    "function taskColsMoveDown(){const s=$('tc_vis');if(!s)return;const i=s.selectedIndex;if(i<0||i>=s.options.length-1)return;const o=s.options[i];s.remove(i);s.add(o,i+1);s.selectedIndex=i+1}"
    "function taskColsResetDefault(){const vis=$('tc_vis'),hid=$('tc_hid');if(!vis||!hid)return;const d=_taskColsNormalize(_TASK_COL_DEFAULT);"
    "vis.innerHTML=d.map(_tcOpt).join('');hid.innerHTML=(_taskColsAll||[]).filter(c=>!d.includes(c)).map(_tcOpt).join('')}"
    "async function saveTaskColsModal(){const cols=_tcRead('tc_vis');if(!cols.length){toast('Tem de manter pelo menos uma coluna visível',true);return}"
    "try{await api('/api/tasks/columns/prefs',{method:'POST',body:JSON.stringify({columns:cols,widths:_taskColWidths})});_taskColsVisible=_taskColsNormalize(cols);"
    "closeTaskColsModal();renderTasks();toast('Colunas guardadas (SQL)')}catch(e){toast(e.message,true)}}"
    "function _renderTasksBySqlCols(){const canEdit=(typeof canEditTasks==='function')?canEditTasks():(String(user?.role||'').trim().toLowerCase()==='edit'||String(user?.role||'').trim().toLowerCase()==='admin');taskColsRenderHeader();"
    "let tb=$('trows');if(!tb)return;tb.innerHTML='';const cols=_taskColsNormalize(_taskColsVisible);"
    "const _pageRows=(typeof tasksPagerSlice==='function')?tasksPagerSlice(taskRows):taskRows;"
    "_pageRows.forEach(r=>{let tr=document.createElement('tr');const _acc=(typeof _taskRowAccent==='function')?_taskRowAccent(r):'';"
    "if(taskSel===r.TaskID)tr.className='sel';if(_acc)tr.classList.add(_acc);if(r.is_overdue)tr.classList.add('row-overdue');"
    "if((r.blocked_count||0)>0)tr.classList.add('row-blocked');if(r.is_recent)tr.classList.add('row-recent');"
    "tr.onclick=()=>{taskSel=r.TaskID;renderTasks()};tr.ondblclick=()=>openTaskDetail(r.TaskID);"
    "tr.innerHTML=cols.map(c=>_taskColCell(r,c)).join('');const stCell=tr.querySelector('.status-cell');"
    "if(stCell&&canEdit)stCell.onclick=(e)=>{e.stopPropagation();quickSetStatus(r.TaskID,r.Estado)};tb.appendChild(tr)});"
    "if(typeof tasksUpdatePager==='function')tasksUpdatePager();"
    "const has=!!taskSel;if($('tb_edit'))$('tb_edit').disabled=!has||!canEdit;if($('tb_dup'))$('tb_dup').disabled=!has||!canEdit;"
    "if($('tb_del'))$('tb_del').disabled=!has||!canEdit;if($('tb_folder'))$('tb_folder').disabled=!has;if($('tb_link'))$('tb_link').disabled=!has;"
    "_taskColsApplyLayout();if(!_taskColHasSavedWidths())_taskColsAutoFit('auto')}"
)

_TASK_COL_WIDTH_JS = (
    "function _taskColMinWidth(c){const m={TaskID:72,NotifEmoji:44,Tarefa:96,Estado:84,Prioridade:68,DataRegisto:84,Prazo:84,InicioPrevisto:84};return m[c]||72}"
    "function _taskColMaxWidth(c){const m={TaskID:180,NotifEmoji:70,Tarefa:640,Estado:180,Prioridade:140,Notificacoes:420};return m[c]||420}"
    "function _taskColNormalizeWidths(raw,cols){const out={};const vis=_taskColsNormalize(cols);"
    "if(raw&&typeof raw==='object'){vis.forEach(c=>{const w=parseInt(raw[c],10);if(w>=40&&w<=900)out[c]=w})}return out}"
    "function _taskColHasSavedWidths(){return _taskColsNormalize(_taskColsVisible).some(c=>Number(_taskColWidths[c]||0)>0)}"
    "function _taskColMeasure(text,bold){if(!_taskColMeasureEl){_taskColMeasureEl=document.createElement('span');"
    "_taskColMeasureEl.style.cssText='position:fixed;left:-9999px;top:0;visibility:hidden;white-space:nowrap;font:13px/1.25 Segoe UI,system-ui,sans-serif;padding:0 8px'}"
    "_taskColMeasureEl.style.fontWeight=bold?'700':'400';_taskColMeasureEl.textContent=String(text||'').slice(0,160);"
    "document.body.appendChild(_taskColMeasureEl);const w=_taskColMeasureEl.offsetWidth+22;document.body.removeChild(_taskColMeasureEl);return w}"
    "function _taskColCellText(r,c){if(!r)return '';if(c==='TaskID')return String(fmtTid(r.TaskID)||r.TaskID||'');"
    "if(c==='Estado')return String(r.Estado||'Não iniciado');if(c==='Prioridade')return String(r.Prioridade||'');"
    "const v=r[c];return v==null?'':String(v)}"
    "function _taskColThHtml(c){const w=Number(_taskColWidths[c]||0);const ws=w>0?` style=\"width:${w}px;min-width:${w}px;max-width:${w}px\"`:'';"
    "const sort=_taskColSortable(c)?' sortable':'';const click=_taskColSortable(c)?` onclick=\"sortTasks('${c}')\"`:'';"
    "return `<th class=\"task-col-th${sort}\" data-col=\"${c}\"${ws}${click}>${esc(_taskColLabel(c))}`"
    "+`<span class=\"th-filter-arrow\" onclick=\"taskColQuickFilter('${c}',event)\" title=\"Filtrar coluna\">▾</span>`"
    "+`<span class=\"col-resizer\" onmousedown=\"taskColStartResize(event,'${c}')\" title=\"Ajustar largura\"></span></th>`}"
    "function _taskColsApplyLayout(){const tb=$('trows');if(!tb)return;const table=tb.closest('table');if(!table)return;"
    "const cols=_taskColsNormalize(_taskColsVisible);let cg=table.querySelector('colgroup#task-cols-cg');"
    "if(!cg){cg=document.createElement('colgroup');cg.id='task-cols-cg';const thead=table.querySelector('thead');if(thead)table.insertBefore(cg,thead);else table.insertBefore(cg,table.firstChild)}"
    "cg.innerHTML=cols.map(c=>{const w=Number(_taskColWidths[c]||0);return w>0?`<col data-col=\"${c}\" style=\"width:${w}px\">`: `<col data-col=\"${c}\">`}).join('');"
    "const tr=table.querySelector('thead tr');if(tr){Array.from(tr.children).forEach((th,i)=>{const c=cols[i];if(!c)return;const w=Number(_taskColWidths[c]||0);"
    "if(w>0){th.style.width=w+'px';th.style.minWidth=w+'px';th.style.maxWidth=w+'px'}else{th.style.width='';th.style.minWidth='';th.style.maxWidth=''}})}}"
    "function _taskColsAutoFit(mode){const cols=_taskColsNormalize(_taskColsVisible);if(!cols.length)return;"
    "const rows=(taskRows||[]).slice(0,250);const next={};cols.forEach(c=>{let max=_taskColMeasure(_taskColLabel(c),true);"
    "rows.forEach(r=>{const t=_taskColCellText(r,c);if(!t) return;const bold=(c==='Tarefa');max=Math.max(max,_taskColMeasure(t,bold))});"
    "next[c]=Math.max(_taskColMinWidth(c),Math.min(_taskColMaxWidth(c),Math.round(max)))});"
    "const wrap=($('trows')||{}).closest?($('trows').closest('.table-wrap')||$('trows').closest('section')):null;"
    "const avail=Math.max(640,(wrap&&wrap.clientWidth?wrap.clientWidth:0)-16);let sum=cols.reduce((a,c)=>a+(next[c]||0),0);"
    "if(sum<avail){let extra=avail-sum;const flex=cols.filter(c=>_TASK_COL_FLEX.has(c));let loops=0;"
    "while(extra>2&&flex.length&&loops<24){const share=Math.ceil(extra/flex.length);flex.forEach(c=>{if(extra<=0)return;"
    "const add=Math.min(share,extra,_taskColMaxWidth(c)-(next[c]||0));if(add>0){next[c]+=add;extra-=add;sum+=add}});loops++}}"
    "_taskColWidths=next;_taskColsApplyLayout();if(mode==='auto'||mode===true)_taskColsSaveWidthsDebounced();if(mode===true)toast('Colunas auto-ajustadas')}"
    "function taskColsAutoFit(){_taskColsAutoFit(true)}"
    "function taskColStartResize(ev,col){ev.preventDefault();ev.stopPropagation();const th=ev.target.closest('th');if(!th)return;"
    "const startX=ev.clientX;const startW=th.offsetWidth||Number(_taskColWidths[col]||120);"
    "_taskColResize={col,startX,startW};document.body.style.cursor='col-resize';document.body.style.userSelect='none';"
    "window.addEventListener('mousemove',taskColDoResize);window.addEventListener('mouseup',taskColEndResize,{once:true})}"
    "function taskColDoResize(ev){if(!_taskColResize)return;const dx=ev.clientX-_taskColResize.startX;const col=_taskColResize.col;"
    "const w=Math.max(_taskColMinWidth(col),Math.min(_taskColMaxWidth(col),Math.round(_taskColResize.startW+dx)));"
    "_taskColWidths[col]=w;_taskColsApplyLayout()}"
    "function taskColEndResize(){if(!_taskColResize){window.removeEventListener('mousemove',taskColDoResize);return}"
    "_taskColResize=null;document.body.style.cursor='';document.body.style.userSelect='';window.removeEventListener('mousemove',taskColDoResize);"
    "_taskColsSaveWidthsDebounced()}"
    "function _taskColsSaveWidthsDebounced(){if(_taskColSaveTimer)clearTimeout(_taskColSaveTimer);"
    "_taskColSaveTimer=setTimeout(()=>{_taskColsSaveWidths().catch(()=>{})},650)}"
    "async function _taskColsSaveWidths(){const cols=_taskColsNormalize(_taskColsVisible);if(!cols.length)return;"
    "const widths={};cols.forEach(c=>{const w=Number(_taskColWidths[c]||0);if(w>0)widths[c]=w});"
    "await api('/api/tasks/columns/prefs',{method:'POST',body:JSON.stringify({columns:cols,widths})})}"
)


def _patch_html_tasks(html: str) -> str:
    html = html.replace(
        '<label style="padding-top:30px"><input type="checkbox" id="tf_blocked"> Só bloqueadas</label><button class="btn" onclick="clearTaskFilters()">Limpar filtros</button>',
        '<label style="padding-top:30px"><input type="checkbox" id="tf_blocked"> Só bloqueadas</label>'
        '<label style="padding-top:30px"><input type="checkbox" id="tf_show_done" onchange="loadTasks()"> Concluídas</label>'
        '<button class="btn" onclick="clearTaskFilters()">Limpar filtros</button>',
        1,
    )
    html = html.replace(
        "blocked:$('tf_blocked')?.checked,involved:$('tf_involved')?.value",
        "blocked:$('tf_blocked')?.checked,show_done:$('tf_show_done')?.checked,involved:$('tf_involved')?.value",
        1,
    )
    html = html.replace(
        "if($('tf_blocked'))$('tf_blocked').checked=!!o.blocked;if(o.involved&&$('tf_involved'))",
        "if($('tf_blocked'))$('tf_blocked').checked=!!o.blocked;if($('tf_show_done'))$('tf_show_done').checked=!!o.show_done;if(o.involved&&$('tf_involved'))",
        1,
    )
    html = html.replace(
        "if($('tf_blocked'))$('tf_blocked').checked=false;if($('tf_involved'))$('tf_involved').value='';",
        "if($('tf_blocked'))$('tf_blocked').checked=false;if($('tf_show_done'))$('tf_show_done').checked=false;if($('tf_involved'))$('tf_involved').value='';",
        1,
    )
    html = html.replace(
        "if($('tf_blocked')?.checked)p.set('blocked_only','1');const im=$('tf_involved')",
        "if($('tf_blocked')?.checked)p.set('blocked_only','1');if($('tf_show_done')?.checked)p.set('show_done','1');const im=$('tf_involved')",
        1,
    )
    html = html.replace(
        '<input id="tf_q" placeholder="TaskID, tarefa, descrição...">',
        '<input id="tf_q" placeholder="TaskID, tarefa, responsável, workers...">',
        1,
    )
    html = html.replace(
        "function canEditTasks(){return user.role==='edit'||user.role==='admin'}",
        "function roleNorm(r){const v=String(r||'').trim().toLowerCase();"
        "if(v==='editor'||v==='write'||v==='escrita')return 'edit';"
        "if(v==='viewer'||v==='leitura')return 'read';"
        "if(v==='administrator'||v==='owner')return 'admin';"
        "if(v==='edit'||v==='read'||v==='admin')return v;return 'read'}"
        "function canEditTasks(){const r=roleNorm(user?.role);return r==='edit'||r==='admin'}",
        1,
    )
    html = html.replace(
        '<th></th><th class="sortable" onclick="sortTasks(\'TaskID\')">TaskID</th><th class="sortable" onclick="sortTasks(\'Tarefa\')">Tarefa</th><th>Notif.</th><th class="sortable" onclick="sortTasks(\'Milestone\')">Milestone</th><th class="sortable" onclick="sortTasks(\'Assunto\')">Assunto</th><th class="sortable" onclick="sortTasks(\'DataRegisto\')">Registo</th><th class="sortable" onclick="sortTasks(\'Prazo\')">Prazo</th><th class="sortable" onclick="sortTasks(\'Responsavel\')">Responsável</th><th>Workers</th><th class="sortable" onclick="sortTasks(\'Estado\')">Estado</th><th class="sortable" onclick="sortTasks(\'Prioridade\')">Prio.</th>',
        '<th></th><th class="sortable" onclick="sortTasks(\'TaskID\')">TaskID</th><th class="sortable" onclick="sortTasks(\'Tarefa\')">Tarefa</th><th>Notif.</th><th class="sortable" onclick="sortTasks(\'Notificacoes\')">Notificações</th><th class="sortable" onclick="sortTasks(\'Milestone\')">Milestone</th><th class="sortable" onclick="sortTasks(\'Assunto\')">Assunto</th><th class="sortable" onclick="sortTasks(\'DataRegisto\')">Registo</th><th class="sortable" onclick="sortTasks(\'Prazo\')">Prazo</th><th class="sortable" onclick="sortTasks(\'Responsavel\')">Responsável</th><th>Workers</th><th class="sortable" onclick="sortTasks(\'Estado\')">Estado</th><th class="sortable" onclick="sortTasks(\'Prioridade\')">Prio.</th>',
        1,
    )
    html = html.replace(
        "function teBadge(e){",
        "function _mineUserTokens(){"
        "const out=[];"
        "const add=v=>{const s=String(v||'').trim();if(!s)return;const k=s.toLowerCase();if(!out.some(x=>x.k===k))out.push({k,v:s})};"
        "add(user?.username);add(user?.display_name);"
        "return out}"
        "function _mineRowMatch(r){"
        "const toks=_mineUserTokens();if(!toks.length)return true;"
        "const resp=String(r?.Responsavel||'').trim().toLowerCase();"
        "const workers=String(r?.Workers||'').split(',').map(x=>String(x||'').trim().toLowerCase()).filter(x=>x);"
        "return toks.some(t=>resp===t.k||workers.includes(t.k))}"
        "function _mineApplyRows(rows){"
        "if(!$('tf_mine')?.checked)return rows||[];"
        "return (rows||[]).filter(r=>_mineRowMatch(r))}"
        "function _mineKpis(rows){"
        "const all=rows||[];"
        "const done=all.filter(r=>String(r?.Estado||'').trim().toLowerCase()==='concluído').length;"
        "const open=Math.max(0,all.length-done);"
        "const overdue=all.filter(r=>!!r?.is_overdue).length;"
        "const blocked=all.filter(r=>Number(r?.blocked_count||0)>0).length;"
        "return {total:all.length,open,done,overdue,blocked}}"
        "function teBadge(e){",
        1,
    )
    html = html.replace(
        "function _renderTasksBySqlCols(){const canEdit=(typeof canEditTasks==='function')?canEditTasks():(String(user?.role||'').trim().toLowerCase()==='edit'||String(user?.role||'').trim().toLowerCase()==='admin');taskColsRenderHeader();",
        "function _renderTasksBySqlCols(){const canEdit=(typeof canEditTasks==='function')?canEditTasks():(String(user?.role||'').trim().toLowerCase()==='edit'||String(user?.role||'').trim().toLowerCase()==='admin');taskColsRenderHeader();",
        1,
    )
    html = html.replace(
        '<button class="btn" id="tb_view" onclick="viewTaskSel()" disabled>Ver detalhe</button>'
        '<button class="btn" id="tb_edit" onclick="editTaskSel()" disabled>Editar</button>',
        '<button class="btn" id="tb_edit" onclick="editTaskSel()" disabled>Editar</button>',
        1,
    )
    html = html.replace(
        '<button class="btn" onclick="openExcelFiltersModal()">Filtros Excel</button>',
        '<button class="btn" onclick="openExcelFiltersModal()">Filtros Excel</button>'
        '<button class="btn" onclick="openTaskColsModal()">Colunas</button>'
        '<button class="btn" onclick="taskColsAutoFit()">Auto-ajustar</button>',
        1,
    )
    html = html.replace(
        '<label><input type="checkbox" id="td_act_hide_done" onchange="renderDetailActions()"> Ocultar concluidos</label>',
        '<select id="td_act_sort" onchange="renderDetailActions()" style="padding:8px;border-radius:8px;border:1px solid #d7dde8">'
        '<option value="ord">Ordem</option><option value="due">Prazo</option><option value="status">Estado</option>'
        '<option value="owner">Owner</option><option value="delay">Atraso</option></select>'
        '<label><input type="checkbox" id="td_act_hide_done" onchange="renderDetailActions()"> Ocultar concluidos</label>',
        1,
    )
    html = html.replace(
        '</div><div class="act-wrap"><table><thead><tr><th></th><th>Tipo</th><th>Texto</th><th>Owner</th><th>Prazo</th><th>Estado</th></tr></thead><tbody id="td_actions"></tbody></table></div>',
        '</div><div class="td-act-chips">'
        '<button class="btn" onclick="detailSetQuickFilter(\'all\')">Todas</button>'
        '<button class="btn" onclick="detailSetQuickFilter(\'action\')">Ações</button>'
        '<button class="btn" onclick="detailSetQuickFilter(\'check\')">Checks</button>'
        '<button class="btn" onclick="detailSetQuickFilter(\'overdue\')">Atrasadas</button>'
        '<button class="btn" onclick="detailSetQuickFilter(\'blocked\')">Bloqueadas</button>'
        '<button class="btn" onclick="detailSetQuickFilter(\'risk\')">Em risco</button>'
        '<button class="btn" onclick="detailSetQuickFilter(\'done\')">Concluídas</button>'
        '</div><div class="act-wrap"><table><tbody id="td_actions"></tbody></table></div>',
        1,
    )
    html = html.replace(
        '<div class="field" id="td_item_owner_f"><label>Owner</label><input id="td_item_owner"></div>',
        '<div class="field" id="td_item_owner_f"><label>Owner</label>'
        '<input id="td_item_owner" list="td_owner_users_dl" placeholder="Selecionar utilizador">'
        '<datalist id="td_owner_users_dl"></datalist>'
        '</div>',
        1,
    )
    html = html.replace(
        '<div class="field span2" id="td_item_workers_f"><label>Workers</label><input id="td_item_workers"></div>',
        '<div class="field span2" id="td_item_workers_f"><label>Workers (múltiplos)</label>'
        '<input id="td_item_workers" list="td_workers_users_dl" placeholder="Ex.: Ana, Bruno ou novo nome">'
        '<datalist id="td_workers_users_dl"></datalist>'
        '<div id="td_workers_quick" class="muted" style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap"></div>'
        '</div>',
        1,
    )
    html = html.replace(
        "function editTaskSel(){if(taskSel)openTaskModal(taskSel,'edit')}",
        "function editTaskSel(){if(taskSel)openTaskDetail(taskSel)}",
        1,
    )
    html = html.replace(
        "function viewTaskSel(){if(taskSel)openTaskDetail(taskSel)}",
        "function viewTaskSel(){editTaskSel()}",
        1,
    )
    html = html.replace(
        '<label>Estado</label><select id="tm_estado"></select>',
        '<label>Estado *</label><select id="tm_estado"></select>',
        1,
    )
    html = html.replace(
        '<label>Prioridade</label><select id="tm_prio"></select>',
        '<label>Prioridade *</label><select id="tm_prio"></select>',
        1,
    )
    html = html.replace(
        '<label>Milestone</label><input id="tm_milestone" list="dl_milestone">',
        '<label>Milestone *</label><input id="tm_milestone" list="dl_milestone">',
        1,
    )
    html = html.replace(
        '<label>Projeto</label><input id="tm_projeto" list="dl_projeto">',
        '<label>Projeto *</label><input id="tm_projeto" list="dl_projeto">',
        1,
    )
    html = html.replace(
        '<label>Linha</label><input id="tm_linha" list="dl_linha">',
        '<label>Linha *</label><input id="tm_linha" list="dl_linha">',
        1,
    )
    html = html.replace(
        '<label>Máquina</label><input id="tm_maquina" list="dl_maquina">',
        '<label>Máquina *</label><input id="tm_maquina" list="dl_maquina">',
        1,
    )
    html = html.replace(
        '<label>Prazo</label><input type="date" id="tm_prazo">',
        '<label>Prazo *</label><input type="date" id="tm_prazo">',
        1,
    )
    html = html.replace(
        '<label>Data registo</label><input type="date" id="tm_data_reg">',
        '<label>Data registo</label><input type="date" id="tm_data_reg" readonly>',
        1,
    )
    html = html.replace(
        '<label>Data registo</label><input type="date" id="td_f_data_reg" value="${esc((t.DataRegisto||\'\').slice(0,10))}">',
        '<label>Data registo</label><input type="date" id="td_f_data_reg" readonly value="${esc((t.DataRegisto||\'\').slice(0,10))}">',
        1,
    )
    html = html.replace(
        '<div class="field"><label>Data registo</label><input type="date" id="td_f_data_reg" readonly value="${esc((t.DataRegisto||\'\').slice(0,10))}"></div>'
        '<div class="field"><label>Início previsto</label>',
        '<div class="field"><label>Data registo</label><input type="date" id="td_f_data_reg" readonly value="${esc((t.DataRegisto||\'\').slice(0,10))}"></div>'
        '<div class="field"><label>Data conclusão</label><input type="date" id="td_f_data_conc" readonly '
        'value="${esc((t.DataConclusao||\'\').slice(0,10))}"></div>'
        '<div class="field"><label>Início previsto</label>',
        1,
    )
    html = html.replace(
        '<button class="btn" id="td_edit_btn" onclick="editTaskFromDetail()" style="display:none">Editar</button>',
        "",
        1,
    )
    html = html.replace(
        "$('td_title').textContent=t.Tarefa||'Tarefa';",
        "$('td_title').textContent=((j.can_edit&&canEditTasks())?'Editar — ':'')+(t.Tarefa||'Tarefa');",
        1,
    )
    html = html.replace(
        "_detailItemSel=null;renderDetailActions();",
        "if($('td_act_hide_done'))$('td_act_hide_done').checked=false;_detailItemSel=null;renderDetailActions();",
        1,
    )
    html = html.replace(
        "const eb=$('td_edit_btn');if(eb)eb.style.display=j.can_edit?'inline-block':'none';",
        "",
        1,
    )
    html = html.replace(
        "toast('Tarefa guardada');loadTaskDetail(_detailTid);loadTasks()}",
        "unsavedClear();toast('Tarefa guardada');closeTaskDetail()}",
        1,
    )
    html = html.replace(
        "async function openTaskDetail(tid){try{if(!tid)return;_detailTid=tid;await ensureTaskLookups();",
        "async function openTaskDetail(tid){try{if(!tid)return;"
        "const _retPage=String(typeof page!=='undefined'&&page?page:'');"
        "if(_retPage&&_retPage!=='task-detail'){_detailReturnPage=(_retPage==='board')?'board':'tasks'}"
        "_detailTid=tid;await ensureTaskLookups();",
        1,
    )
    html = html.replace(
        "function closeTaskDetail(){_detailTid=null;showPage('tasks')}",
        "function closeTaskDetail(){const ret=(_detailReturnPage==='board')?'board':'tasks';"
        "_detailTid=null;_detailReturnPage='tasks';showPage(ret)}",
        1,
    )
    html = html.replace(
        "if($('tb_view'))$('tb_view').disabled=!has;if($('tb_edit'))$('tb_edit').disabled=!has||!canEdit;",
        "if($('tb_edit'))$('tb_edit').disabled=!has||!canEdit;",
        1,
    )
    html = html.replace(
        "async function loadTasks(){try{saveTaskFilters();let j=await api('/api/tasks?'+tqs());taskRows=j.rows||[];if(_taskSortCol)sortTasks(_taskSortCol,true);else renderTasks();$('tk_total').textContent=j.kpis.total;$('tk_open').textContent=j.kpis.open;$('tk_done').textContent=j.kpis.done;$('tk_overdue').textContent=j.kpis.overdue;$('tk_blocked').textContent=j.kpis.blocked;if(page==='tasks')$('upd').textContent='Última atualização: '+new Date().toLocaleTimeString('pt-PT')}catch(e){toast(e.message,true)}}",
        "async function loadTasks(){try{saveTaskFilters();const p=tqs();if($('tf_mine')?.checked)p.delete('only_mine');"
        "let j=await api('/api/tasks?'+p);"
        "await taskColsEnsureLoaded();"
        "let _rows=(j.rows||[]);taskRows=_mineApplyRows(_rows);"
        "if(_taskSortCol)sortTasks(_taskSortCol,true);else renderTasks();"
        "const k=($('tf_mine')?.checked)?_mineKpis(taskRows):(j.kpis||{});"
        "$('tk_total').textContent=k.total??0;$('tk_open').textContent=k.open??0;$('tk_done').textContent=k.done??0;"
        "$('tk_overdue').textContent=k.overdue??0;$('tk_blocked').textContent=k.blocked??0;"
        "if(page==='tasks')$('upd').textContent='Última atualização: '+new Date().toLocaleTimeString('pt-PT')}catch(e){toast(e.message,true)}}",
        1,
    )
    html = html.replace(
        "let _detailTid=null,_detailData=null;",
        _TASK_COL_PREFS_JS
        + _TASK_COL_WIDTH_JS
        + "renderTasks=_renderTasksBySqlCols;let _detailTid=null,_detailData=null,_detailReturnPage='tasks';",
        1,
    )
    html = re.sub(
        r"function detailAllItems\(\)\{.*?function renderDetailAttachments\(\)\{",
        "let _tdActQuick='all';let _detailFormKind='ACTION';"
        "function detailSetQuickFilter(v){_tdActQuick=String(v||'all');renderDetailActions()}"
        "function _toBool(v){const s=String(v??'').trim().toLowerCase();if(s===''||s==='0'||s==='false'||s==='no'||s==='off'||s==='null'||s==='none')return false;"
        "if(s==='1'||s==='true'||s==='yes'||s==='on')return true;return !!v}"
        "function _detailHasRealActionMeta(o){const meta=!!String(o.owner||o.workers||o.start_date||o.due_date||o.evidence||o.blocked_reason||'').trim();"
        "if(meta)return true;const st=String(o.status||'').trim().toLowerCase();return !!(st&&st!=='não iniciado'&&st!=='nao iniciado'&&st!=='concluído'&&st!=='concluido');}"
        "function tdEnsureItemKindField(){const frm=$('td_item_form');if(!frm)return;let el=$('td_item_kind');if(!el){el=document.createElement('input');el.type='hidden';el.id='td_item_kind';frm.insertBefore(el,frm.firstChild)}}"
        "function _detailNorm(it,kind){const o=Object.assign({},it||{});let k=String(o.kind||kind||'').trim().toUpperCase();"
        "const hasActionMeta=_detailHasRealActionMeta(o);"
        "if(k!=='ACTION'&&k!=='CHECK')k=hasActionMeta?'ACTION':'CHECK';"
        "else if(k==='CHECK'&&hasActionMeta)k='ACTION';"
        "o.kind=k;o.item_text=String(o.item_text||o.action_text||o.text||'').trim();"
        "o.id=(o.id??o.item_id??o.action_id??o.item_uuid??'');"
        "o.owner=String(o.owner||'').trim();o.workers=String(o.workers||'').trim();o.status=String(o.status||'').trim();"
        "o.start_date=String(o.start_date||'').trim();o.due_date=String(o.due_date||'').trim();o.evidence=String(o.evidence||'').trim();"
        "o.blocked_reason=String(o.blocked_reason||'').trim();o.action_notes=String(o.action_notes||o.notes||'').trim();const _stDone=String(o.status||'').toLowerCase();o.done=(_toBool(o.done)||_toBool(o.is_done)||_stDone.includes('conclu'));"
        "return o}"
        "function detailAllItems(){const items=[];const seen=new Set();"
        "const push=(arr,fallback)=>{(arr||[]).forEach(raw=>{const o=_detailNorm(raw,fallback);const id=String(o.id||o.item_uuid||'').trim();"
        "const key=id?('id:'+id):('txt:'+o.kind+'|'+o.item_text+'|'+o.start_date+'|'+o.due_date);if(seen.has(key))return;seen.add(key);items.push(o)})};"
        "push(_detailData?.actions,'ACTION');push(_detailData?.checklist,'CHECK');return items}"
        "function detailCounters(){const items=detailAllItems();const t=new Date();t.setHours(0,0,0,0);const now=t.getTime();"
        "const c={total:items.length,actions:0,checks:0,done:0,overdue:0,blocked:0,risk:0};"
        "items.forEach(a=>{if(a.kind==='ACTION')c.actions++;else c.checks++;if(a.done)c.done++;if(String(a.status||'').toLowerCase()==='bloqueado')c.blocked++;"
        "const d=String(a.due_date||'').slice(0,10);if(d&&!a.done){const dt=new Date(d+'T00:00:00');if(!isNaN(dt)){const dd=Math.floor((dt.getTime()-now)/86400000);if(dd<0)c.overdue++;else if(dd<=7)c.risk++}}});return c}"
        "function computeActionUiState(a){if(a.done)return {state:'done',badge:'Concluída',delay:0};const st=String(a.status||'').toLowerCase();const isBlocked=st.includes('bloque');if(isBlocked)return {state:'blocked',badge:'Bloqueada',delay:0};"
        "const d=String(a.due_date||'').slice(0,10);if(!d)return {state:'normal',badge:'Sem prazo',delay:0};const t=new Date();t.setHours(0,0,0,0);const dt=new Date(d+'T00:00:00');if(isNaN(dt))return {state:'normal',badge:'Sem prazo',delay:0};"
        "const dd=Math.floor((dt.getTime()-t.getTime())/86400000);if(dd<0)return {state:'overdue',badge:'Atraso '+Math.abs(dd)+'d',delay:Math.abs(dd)};if(dd===0)return {state:'risk',badge:'Vence hoje',delay:0};if(dd<=7)return {state:'risk',badge:'Vence em '+dd+'d',delay:0};if(st.includes('progres')||st.includes('curso'))return {state:'progress',badge:'Em progresso',delay:0};return {state:'normal',badge:'No prazo',delay:0}}"
        "function _detailMatchQuick(a,u){if(u==='all')return true;if(u==='action'||u==='actions')return a.kind==='ACTION';if(u==='check'||u==='checks')return a.kind==='CHECK';if(u==='overdue')return a._ui.state==='overdue';if(u==='blocked')return a._ui.state==='blocked';if(u==='risk')return a._ui.state==='risk';if(u==='done'||u==='completed'||u==='concluidas')return a._ui.state==='done';return true}"
        "function detailSelectItem(id){_detailItemSel=id;renderDetailActions()}"
        "function detailItemMenu(id,e){if(e){e.preventDefault();e.stopPropagation()}const a=detailAllItems().find(x=>x.id===id);if(!a)return;"
        "const act=(a.kind==='ACTION')?'1 Editar | 2 Duplicar | 3 Evidência | 4 Dependências | 5 Gantt | 6 Apagar':'1 Editar | 2 Apagar';"
        "const r=prompt('Ação: '+act,'1');if(!r)return;const k=String(r||'').trim();_detailItemSel=id;"
        "if(k==='1')return detailEditItem();if(a.kind==='ACTION'&&k==='2')return detailDuplicateItem(id);if(a.kind!=='ACTION'&&k==='2')return detailDelItem();"
        "if(a.kind==='ACTION'&&k==='3'){detailEditItem();setTimeout(()=>{try{$('td_item_evidence')?.focus()}catch(_){ }},50);return}"
        "if(a.kind==='ACTION'&&k==='4'){detailEditDeps(id);return}"
        "if(a.kind==='ACTION'&&k==='5'){if(typeof detailOpenTaskGantt==='function')detailOpenTaskGantt();return}"
        "if((a.kind==='ACTION'&&k==='6')||(a.kind!=='ACTION'&&k==='2'))return detailDelItem()}"
        "async function detailEditDeps(id){try{const aid=Number(id||0);if(!aid){toast('Ação inválida',true);return}"
        "const acts=detailAllItems().filter(x=>String(x.kind||'')==='ACTION'&&Number(x.id||0)>0&&Number(x.id)!==aid);"
        "if(!acts.length){toast('Sem outras ações para dependências',true);return}"
        "const cur=await api('/api/actions/'+encodeURIComponent(aid)+'/deps');"
        "const curDeps=(cur.deps||[]).map(d=>({depends_on:Number(d.depends_on||0),dep_type:String(d.dep_type||'FS').toUpperCase(),lag_days:parseInt(d.lag_days||0,10)||0})).filter(d=>d.depends_on>0);"
        "const mid='td_deps_modal';const old=$(mid);if(old)old.remove();"
        "document.body.insertAdjacentHTML('beforeend','<div class=\"modal-bg\" id=\"'+mid+'\" style=\"display:flex\">'+"
        "'<div class=\"modal\" style=\"width:min(900px,96vw)\"><div class=\"mh\"><h3 style=\"margin:0\">Dependências — Ação '+aid+'</h3><button class=\"btn\" id=\"td_deps_close\">✕</button></div>'+"
        "'<div class=\"mc\"><div class=\"muted\" style=\"margin-bottom:8px\">Predecessor · Tipo · Lag (dias)</div><div id=\"td_deps_rows\"></div><div id=\"td_deps_err\" class=\"muted\" style=\"margin:8px 0;color:#b91c1c\"></div>'+"
        "'<div style=\"display:flex;justify-content:flex-end;gap:8px\"><button class=\"btn\" id=\"td_deps_add\">+ Linha</button><button class=\"btn primary\" id=\"td_deps_save\">Guardar</button><button class=\"btn\" id=\"td_deps_cancel\">Cancelar</button></div></div></div></div>');"
        "const rowsBox=$('td_deps_rows'),errEl=$('td_deps_err');const close=()=>{const m=$(mid);if(m)m.remove()};"
        "$('td_deps_close')?.addEventListener('click',close);$('td_deps_cancel')?.addEventListener('click',close);"
        "const mkRow=(src)=>{const row=document.createElement('div');row.dataset.depRow='1';row.style.display='grid';row.style.gridTemplateColumns='minmax(260px,1fr) 90px 100px 80px';row.style.gap='6px';row.style.marginBottom='6px';"
        "const pred=document.createElement('select');pred.className='inp';const opt0=document.createElement('option');opt0.value='';opt0.textContent='Selecione predecessor';pred.appendChild(opt0);"
        "acts.forEach(a=>{const o=document.createElement('option');o.value=String(a.id);o.textContent=String(a.id)+' - '+String(a.item_text||'').slice(0,90);pred.appendChild(o)});"
        "const typ=document.createElement('select');typ.className='inp';['FS','SS','FF','SF'].forEach(t=>{const o=document.createElement('option');o.value=t;o.textContent=t;typ.appendChild(o)});"
        "const lag=document.createElement('input');lag.className='inp';lag.type='number';lag.min='-30';lag.max='365';lag.step='1';lag.value=String(parseInt(src?.lag_days||0,10)||0);"
        "const del=document.createElement('button');del.type='button';del.className='btn';del.textContent='Remover';del.onclick=()=>row.remove();"
        "pred.value=src&&src.depends_on?String(src.depends_on):'';typ.value=String(src?.dep_type||'FS').toUpperCase();"
        "row.appendChild(pred);row.appendChild(typ);row.appendChild(lag);row.appendChild(del);"
        "row._get=()=>({depends_on:Number(pred.value||0),dep_type:String(typ.value||'FS').toUpperCase(),lag_days:parseInt(lag.value||'0',10)||0});rowsBox.appendChild(row)};"
        "(curDeps.length?curDeps:[{depends_on:0,dep_type:'FS',lag_days:0}]).forEach(mkRow);"
        "$('td_deps_add')?.addEventListener('click',()=>mkRow({depends_on:0,dep_type:'FS',lag_days:0}));"
        "$('td_deps_save')?.addEventListener('click',async()=>{try{if(errEl)errEl.textContent='';const validIds=new Set(acts.map(a=>Number(a.id||0)).filter(n=>n>0));"
        "const deps=[];const seen=new Set();for(const r of Array.from(rowsBox.querySelectorAll('[data-dep-row=\"1\"]'))){const d=(typeof r._get==='function')?r._get():null;if(!d||!d.depends_on)continue;"
        "if(!validIds.has(d.depends_on)){if(errEl)errEl.textContent='Predecessor fora da tarefa: '+d.depends_on;return}if(d.depends_on===aid){if(errEl)errEl.textContent='Uma ação não pode depender de si mesma.';return}"
        "if(!['FS','SS','FF','SF'].includes(d.dep_type)){if(errEl)errEl.textContent='Tipo inválido: '+d.dep_type;return}"
        "if(d.lag_days<-30||d.lag_days>365){if(errEl)errEl.textContent='Lag fora do intervalo (-30..365).';return}"
        "const k=d.depends_on+'|'+d.dep_type+'|'+d.lag_days;if(seen.has(k))continue;seen.add(k);deps.push(d)}"
        "await api('/api/actions/'+encodeURIComponent(aid)+'/deps',{method:'POST',body:JSON.stringify({deps})});"
        "toast('Dependências guardadas');if(typeof tdRefreshActionDepsInfo==='function')tdRefreshActionDepsInfo();close();if(typeof loadTaskDetail==='function'&&_detailTid)loadTaskDetail(_detailTid)}"
        "catch(ex){if(errEl)errEl.textContent=String(ex&&ex.message||ex||'Falha ao guardar')}})}"
        "catch(e){toast(e.message,true)}}"
        "function detailDuplicateItem(id){const a=detailAllItems().find(x=>x.id===id);if(!a)return;$('td_item_id').value='';$('td_item_text').value=String(a.item_text||'')+' (cópia)';"
        "$('td_item_owner').value=a.owner||'';$('td_item_status').value=a.status||'Não iniciado';$('td_item_start').value=(a.start_date||'').slice(0,10);$('td_item_due').value=(a.due_date||'').slice(0,10);"
        "$('td_item_workers').value=a.workers||'';$('td_item_evidence').value=a.evidence||'';$('td_item_blocked').value=a.blocked_reason||'';detailShowItemForm(a.kind||'ACTION')}"
        "function updateDetailActionStats(){const el=$('td_act_stats');if(!el)return;const c=detailCounters();el.innerHTML="
        "`<span class=\"chip\">Total: ${c.total}</span><span class=\"chip\">Ações: ${c.actions}</span><span class=\"chip\">Checks: ${c.checks}</span><span class=\"chip\">Concluídas: ${c.done}</span><span class=\"chip\">Atrasadas: ${c.overdue}</span><span class=\"chip\">Bloqueadas: ${c.blocked}</span><span class=\"chip\">Em risco: ${c.risk}</span>`}"
        "function renderDetailActions(){const tb=$('td_actions');if(!tb)return;const q=($('td_act_q')?.value||'').toLowerCase();const hide=$('td_act_hide_done')?.checked;const hideDone=!!hide&&_tdActQuick!=='done';const kindF=$('td_act_kind')?.value||_detailActKind||'';const sort=$('td_act_sort')?.value||'ord';"
        "const items=detailAllItems().map(a=>{a._ui=computeActionUiState(a);return a}).filter(a=>{if(kindF&&String(a.kind)!==kindF)return false;if(hideDone&&a.done)return false;if(!_detailMatchQuick(a,_tdActQuick))return false;if(q&&!String(a.item_text||'').toLowerCase().includes(q))return false;return true});"
        "items.sort((a,b)=>{if(sort==='owner')return String(a.owner||'').localeCompare(String(b.owner||''));if(sort==='status')return String(a.status||'').localeCompare(String(b.status||''));"
        "if(sort==='due'){const da=String(a.due_date||'');const db=String(b.due_date||'');return da.localeCompare(db)}if(sort==='delay')return Number(b._ui.delay||0)-Number(a._ui.delay||0);return Number(a.ord||0)-Number(b.ord||0)});"
        "const c=detailCounters();const has=items.length>0;tb.innerHTML='';"
        "if($('td_prog_lbl'))$('td_prog_lbl').textContent=(c.total>0)?`(${c.total} itens)`:'(sem itens)';"
        "if(!has){const tr=document.createElement('tr');tr.innerHTML=`<td colspan=\"6\"><div class=\"td-empty\">Ainda não existem ações ou verificações. Usa + Ação ou + Check para começar.</div></td>`;tb.appendChild(tr)}"
        "const actions=items.filter(a=>a.kind==='ACTION');const checks=items.filter(a=>a.kind==='CHECK');"
        "if(actions.length){const trh=document.createElement('tr');trh.className='act-section';trh.innerHTML='<td colspan=\"6\">AÇÕES</td>';tb.appendChild(trh);"
        "actions.forEach(a=>{const tr=document.createElement('tr');tr.className='act-card-row '+a._ui.state+(_detailItemSel===a.id?' sel':'');tr.onclick=()=>detailSelectItem(a.id);tr.ondblclick=()=>{if(_detailEdit)detailEditItem()};"
        "tr.innerHTML=`<td colspan=\"6\"><div class=\"act-card\">"
        "<div class=\"act-top\"><span class=\"badge ${a._ui.state}\">${esc(a._ui.badge)}</span><span class=\"badge kind\">AÇÃO</span><button class=\"btn\" onclick=\"detailItemMenu(${a.id},event)\">...</button></div>"
        "<div class=\"act-title\">${esc(a.item_text||'')}</div>"
        "<div class=\"act-meta\"><span>Resp: ${esc(a.owner||'—')}</span><span>W: ${esc(a.workers||'—')}</span><span>Prazo: ${esc((a.due_date||'').slice(0,10)||'—')}</span><span>Estado: ${esc(a.status||'—')}</span></div>"
        "</div></td>`;tb.appendChild(tr)})}"
        "if(checks.length){const trh=document.createElement('tr');trh.className='act-section';trh.innerHTML='<td colspan=\"6\">CHECKLIST</td>';tb.appendChild(trh);"
        "checks.forEach(a=>{const tr=document.createElement('tr');tr.className='check-row '+a._ui.state+(_detailItemSel===a.id?' sel':'');tr.onclick=()=>detailSelectItem(a.id);tr.ondblclick=()=>{if(_detailEdit)detailToggleCheck(a.id)};"
        "const chk=`<input type=\"checkbox\" ${a.done?'checked':''} onclick=\"event.stopPropagation();detailToggleCheck(${a.id})\">`;"
        "tr.innerHTML=`<td>${chk}</td><td colspan=\"4\"><div class=\"check-text\">${esc(a.item_text||'')}</div></td><td><button class=\"btn\" onclick=\"detailItemMenu(${a.id},event)\">...</button></td>`;tb.appendChild(tr)})}"
        "if($('td_act_edit_btn'))$('td_act_edit_btn').disabled=!_detailEdit||!_detailItemSel;if($('td_act_del_btn'))$('td_act_del_btn').disabled=!_detailEdit||!_detailItemSel;updateDetailActionStats()}"
        "function renderDetailAttachments(){",
        html,
        count=1,
        flags=re.S,
    )
    html = html.replace(
        "$('td_prog_lbl').textContent=pr.total?`(${pr.done}/${pr.total} · ${pr.percent}%)`:'(sem ações)';",
        "const c=detailCounters();$('td_prog_lbl').textContent=c.total?`(${c.total} itens)`:'(sem itens)';",
        1,
    )
    html = html.replace(
        "function detailShowItemForm(kind){$('td_item_kind').value=kind||'ACTION';const isCh=kind==='CHECK';['td_item_owner_f','td_item_status_f','td_item_start_f','td_item_due_f','td_item_workers_f','td_item_ev_f','td_item_blk_f'].forEach(id=>{const el=$(id);if(el)el.style.display=isCh?'none':'block'});$('td_item_form').style.display='grid'}",
        "function tdEnsureActionNotesUi(){const frm=$('td_item_form');if(!frm||$('td_item_notes_f'))return;"
        "const host=document.createElement('div');host.className='field span2';host.id='td_item_notes_f';host.innerHTML="
        "'<label>Notas da ação</label><div id=\"td_item_notes_tools\" class=\"rtbar\">'"
        "+'<button type=\"button\" onclick=\"rtCmd(\\'bold\\',\\'td_item_notes_rt\\')\"><b>B</b></button>'"
        "+'<button type=\"button\" onclick=\"rtCmd(\\'italic\\',\\'td_item_notes_rt\\')\"><i>I</i></button>'"
        "+'<button type=\"button\" onclick=\"rtInsert(\\'td_item_notes_rt\\',\\'note\\')\">Nota</button>'"
        "+'<button type=\"button\" onclick=\"rtInsert(\\'td_item_notes_rt\\',\\'warn\\')\">Atencao</button>'"
        "+'<button type=\"button\" onclick=\"rtLink(\\'td_item_notes_rt\\')\">Link</button>'"
        "+'<button type=\"button\" onclick=\"rtStamp(\\'td_item_notes_rt\\')\">Data/Hora</button>'"
        "+'<button type=\"button\" onclick=\"rtClear(\\'td_item_notes_rt\\')\">Limpar</button>'"
        "+'</div><div id=\"td_item_notes_rt\" class=\"rtbox\" contenteditable=\"true\"></div>';"
        "const saveRow=frm.querySelector('.span4');if(saveRow&&saveRow.parentNode)saveRow.parentNode.insertBefore(host,saveRow);else frm.appendChild(host)}"
        "function tdOpenCurrentActionDeps(){const id=Number($('td_item_id')?.value||0);if(!id){toast('Guarde a ação primeiro',true);return}detailEditDeps(id)}"
        "function tdEnsureActionDepsUi(){const frm=$('td_item_form');if(!frm||$('td_item_deps_f'))return;"
        "const host=document.createElement('div');host.className='field span2';host.id='td_item_deps_f';host.innerHTML="
        "'<label>Dependências</label><div style=\"display:flex;gap:8px;align-items:center;flex-wrap:wrap\">'"
        "+'<button type=\"button\" class=\"btn\" id=\"td_item_deps_btn\" onclick=\"tdOpenCurrentActionDeps()\">Editar dependências</button>'"
        "+'<span id=\"td_item_deps_info\" class=\"muted\">Guarde a ação para definir deps</span>'"
        "+'</div>';"
        "const saveRow=frm.querySelector('.span4');if(saveRow&&saveRow.parentNode)saveRow.parentNode.insertBefore(host,saveRow);else frm.appendChild(host)}"
        "function tdRefreshActionDepsInfo(){const info=$('td_item_deps_info');const btn=$('td_item_deps_btn');const kind=String($('td_item_kind')?.value||'ACTION').toUpperCase();"
        "if(kind!=='ACTION'){if(info)info.textContent='Apenas para ações';if(btn)btn.disabled=true;return}"
        "const id=Number($('td_item_id')?.value||0);if(!id){if(info)info.textContent='Guarde a ação para definir deps';if(btn)btn.disabled=true;return}"
        "if(btn)btn.disabled=false;if(info)info.textContent='A carregar…';(async()=>{try{const j=await api('/api/actions/'+encodeURIComponent(id)+'/deps');const n=(j.deps||[]).length;"
        "if(info)info.textContent=n?('Dependências: '+n):'Sem dependências'}catch(e){if(info)info.textContent='Erro ao ler dependências'}})()}"
        "function tdEnsureActionUserInputs(){const users=[...new Set(((_taskLookups?.users)||[]).map(v=>String(v||'').trim()).filter(v=>v))];"
        "const ownDl=$('td_owner_users_dl');if(ownDl){ownDl.innerHTML=users.map(u=>'<option value=\"'+esc(u)+'\"></option>').join('')}"
        "const wDl=$('td_workers_users_dl');if(wDl){wDl.innerHTML=users.map(u=>'<option value=\"'+esc(u)+'\"></option>').join('')}"
        "const q=$('td_workers_quick');if(q){q.innerHTML=users.slice(0,24).map(u=>'<button type=\"button\" class=\"btn\" style=\"padding:2px 8px;font-size:11px\" onclick=\"tdToggleWorker(\\''+esc(u).replace(/'/g,'&#39;')+'\\')\">'+esc(u)+'</button>').join('')||'<span class=\"muted\">Sem utilizadores na lista.</span>'}}"
        "function tdToggleWorker(name){const el=$('td_item_workers');if(!el)return;const n=String(name||'').trim();if(!n)return;"
        "const arr=String(el.value||'').split(',').map(v=>String(v||'').trim()).filter(v=>v);const low=n.toLowerCase();const idx=arr.findIndex(v=>v.toLowerCase()===low);"
        "if(idx>=0)arr.splice(idx,1);else arr.push(n);el.value=arr.join(', ')}"
        "function detailShowItemForm(kind){const k=String(kind||_detailFormKind||'ACTION').toUpperCase();_detailFormKind=k;tdEnsureItemKindField();if($('td_item_kind'))$('td_item_kind').value=k;const isCh=k==='CHECK';tdEnsureActionUserInputs();tdEnsureActionNotesUi();tdEnsureActionDepsUi();"
        "['td_item_owner_f','td_item_status_f','td_item_start_f','td_item_due_f','td_item_workers_f','td_item_ev_f','td_item_blk_f','td_item_notes_f','td_item_deps_f'].forEach(id=>{const el=$(id);if(el)el.style.display=isCh?'none':'block'});"
        "const frm=$('td_item_form');if(frm)frm.style.display='grid';tdRefreshActionDepsInfo()}",
        1,
    )
    html = html.replace(
        "function detailNewAction(){if(!_detailEdit)return;$('td_item_id').value='';$('td_item_text').value='';$('td_item_owner').value=user.username||'';$('td_item_status').value='No iniciado';$('td_item_start').value=new Date().toISOString().slice(0,10);$('td_item_due').value='';$('td_item_workers').value='';$('td_item_evidence').value='';$('td_item_blocked').value='';detailShowItemForm('ACTION')}",
        "function detailNewAction(){if(!_detailEdit){if(canEditTasks()&&typeof setDetailEditMode==='function')setDetailEditMode(true)}"
        "if(!_detailEdit){toast('Sem permissões para editar',true);return}"
        "if($('td_item_id'))$('td_item_id').value='';if($('td_item_text'))$('td_item_text').value='';if($('td_item_owner'))$('td_item_owner').value=user.username||'';"
        "if($('td_item_status'))$('td_item_status').value='Não iniciado';if($('td_item_start'))$('td_item_start').value=new Date().toISOString().slice(0,10);"
        "if($('td_item_due'))$('td_item_due').value='';if($('td_item_workers'))$('td_item_workers').value='';if($('td_item_evidence'))$('td_item_evidence').value='';if($('td_item_blocked'))$('td_item_blocked').value='';detailShowItemForm('ACTION');try{setRtHtml('td_item_notes_rt','')}catch(_){}}",
        1,
    )
    html = html.replace(
        "function detailNewCheck(){if(!_detailEdit)return;$('td_item_id').value='';$('td_item_text').value='';detailShowItemForm('CHECK')}",
        "function detailNewCheck(){if(!_detailEdit){if(canEditTasks()&&typeof setDetailEditMode==='function')setDetailEditMode(true)}"
        "if(!_detailEdit){toast('Sem permissões para editar',true);return}"
        "if($('td_item_id'))$('td_item_id').value='';if($('td_item_text'))$('td_item_text').value='';detailShowItemForm('CHECK')}",
        1,
    )
    html = html.replace(
        "function detailEditItem(){const a=detailAllItems().find(x=>x.id===_detailItemSel);if(!a)return;$('td_item_id').value=a.id;$('td_item_text').value=a.item_text||'';$('td_item_owner').value=a.owner||'';$('td_item_status').value=a.status||'No iniciado';$('td_item_start').value=(a.start_date||'').slice(0,10);$('td_item_due').value=(a.due_date||'').slice(0,10);$('td_item_workers').value=a.workers||'';$('td_item_evidence').value=a.evidence||'';$('td_item_blocked').value=a.blocked_reason||'';detailShowItemForm(a.kind||'ACTION')}",
        "function detailEditItem(){if(!_detailEdit){if(canEditTasks()&&typeof setDetailEditMode==='function')setDetailEditMode(true)}"
        "if(!_detailEdit){toast('Sem permissões para editar',true);return}"
        "const sel=String(_detailItemSel??'');const a=detailAllItems().find(x=>String(x.id??'')===sel||String(x.item_uuid??'')===sel);if(!a){toast('Selecione um item para editar',true);return}"
        "if($('td_item_id'))$('td_item_id').value=String(a.id??'');if($('td_item_text'))$('td_item_text').value=a.item_text||'';if($('td_item_owner'))$('td_item_owner').value=a.owner||'';"
        "if($('td_item_status'))$('td_item_status').value=a.status||'Não iniciado';if($('td_item_start'))$('td_item_start').value=(a.start_date||'').slice(0,10);if($('td_item_due'))$('td_item_due').value=(a.due_date||'').slice(0,10);"
        "if($('td_item_workers'))$('td_item_workers').value=a.workers||'';if($('td_item_evidence'))$('td_item_evidence').value=a.evidence||'';if($('td_item_blocked'))$('td_item_blocked').value=a.blocked_reason||'';detailShowItemForm(a.kind||'ACTION');try{setRtHtml('td_item_notes_rt',a.action_notes||'')}catch(_){}}",
        1,
    )
    html = re.sub(
        r"function detailNewAction\(\)\{[\s\S]*?\}",
        "function detailNewAction(){if(!_detailEdit){if(canEditTasks()&&typeof setDetailEditMode==='function')setDetailEditMode(true)}"
        "if(!_detailEdit){toast('Sem permissões para editar',true);return}"
        "if($('td_item_id'))$('td_item_id').value='';if($('td_item_text'))$('td_item_text').value='';if($('td_item_owner'))$('td_item_owner').value=user.username||'';"
        "if($('td_item_status'))$('td_item_status').value='Não iniciado';if($('td_item_start'))$('td_item_start').value=new Date().toISOString().slice(0,10);"
        "if($('td_item_due'))$('td_item_due').value='';if($('td_item_workers'))$('td_item_workers').value='';if($('td_item_evidence'))$('td_item_evidence').value='';if($('td_item_blocked'))$('td_item_blocked').value='';detailShowItemForm('ACTION');try{setRtHtml('td_item_notes_rt','')}catch(_){}}",
        html,
        count=1,
        flags=re.S,
    )
    html = re.sub(
        r"function detailEditItem\(\)\{[\s\S]*?\}",
        "function detailEditItem(){if(!_detailEdit){if(canEditTasks()&&typeof setDetailEditMode==='function')setDetailEditMode(true)}"
        "if(!_detailEdit){toast('Sem permissões para editar',true);return}"
        "const sel=String(_detailItemSel??'');const a=detailAllItems().find(x=>String(x.id??'')===sel||String(x.item_uuid??'')===sel);if(!a){toast('Selecione um item para editar',true);return}"
        "if($('td_item_id'))$('td_item_id').value=String(a.id??'');if($('td_item_text'))$('td_item_text').value=a.item_text||'';if($('td_item_owner'))$('td_item_owner').value=a.owner||'';"
        "if($('td_item_status'))$('td_item_status').value=a.status||'Não iniciado';if($('td_item_start'))$('td_item_start').value=(a.start_date||'').slice(0,10);if($('td_item_due'))$('td_item_due').value=(a.due_date||'').slice(0,10);"
        "if($('td_item_workers'))$('td_item_workers').value=a.workers||'';if($('td_item_evidence'))$('td_item_evidence').value=a.evidence||'';if($('td_item_blocked'))$('td_item_blocked').value=a.blocked_reason||'';detailShowItemForm(a.kind||'ACTION');try{setRtHtml('td_item_notes_rt',a.action_notes||'')}catch(_){}}",
        html,
        count=1,
        flags=re.S,
    )
    html = html.replace(
        "if($('td_item_workers'))$('td_item_workers').value=a.workers||'';if($('td_item_evidence'))$('td_item_evidence').value=a.evidence||'';if($('td_item_blocked'))$('td_item_blocked').value=a.blocked_reason||'';detailShowItemForm(a.kind||'ACTION')}",
        "if($('td_item_workers'))$('td_item_workers').value=a.workers||'';if($('td_item_evidence'))$('td_item_evidence').value=a.evidence||'';if($('td_item_blocked'))$('td_item_blocked').value=a.blocked_reason||'';try{setRtHtml('td_item_notes_rt',a.action_notes||'')}catch(_){}"
        "const k=String(a.kind||'ACTION').toUpperCase();detailShowItemForm(k);"
        "if(k==='CHECK')toast('Este item é CHECK: só Texto. Para Responsável/Workers/Prazo cria ou edita uma Ação.',false)}",
        1,
    )
    html = re.sub(
        r"async function detailSaveItem\(\)\{[\s\S]*?\}\s*async function detailDelItem\(\)",
        "async function detailSaveItem(){try{if(!_detailTid)return;"
        "const kind=String($('td_item_kind')?.value||_detailFormKind||'ACTION').toUpperCase();"
        "const p={item_text:String($('td_item_text')?.value||'').trim()};"
        "const id=String($('td_item_id')?.value||'').trim();"
        "if(!p.item_text){toast('Texto obrigatório',true);return}"
        "if(kind==='ACTION'){if(typeof validateDetailActionRequired==='function'&&!validateDetailActionRequired())return;"
        "Object.assign(p,{owner:String($('td_item_owner')?.value||'').trim(),status:String($('td_item_status')?.value||'').trim(),start_date:String($('td_item_start')?.value||'').slice(0,10),due_date:String($('td_item_due')?.value||'').slice(0,10),workers:String($('td_item_workers')?.value||'').trim(),evidence:String($('td_item_evidence')?.value||'').trim(),blocked_reason:String($('td_item_blocked')?.value||'').trim(),action_notes:String(getRtHtml('td_item_notes_rt')||'').trim()});"
        "const st=String(p.status||'').trim().toLowerCase();if(st==='bloqueado'&&!String(p.blocked_reason||'').trim()){toast('Motivo é obrigatório quando Bloqueado',true);return}}"
        "if(id){"
        "if(kind==='ACTION')await api('/api/actions/'+encodeURIComponent(id),{method:'PUT',body:JSON.stringify(p)});"
        "else await api('/api/checklist/'+encodeURIComponent(id),{method:'PUT',body:JSON.stringify(p)});"
        "}else if(kind==='CHECK')await api('/api/tasks/'+encodeURIComponent(_detailTid)+'/checklist',{method:'POST',body:JSON.stringify({kind:'CHECK',item_text:p.item_text})});"
        "else {const j=await api('/api/tasks/'+encodeURIComponent(_detailTid)+'/actions',{method:'POST',body:JSON.stringify(p)});"
        "const nid=Number(j?.id||j?.action_id||j?.item_id||0);if(nid>0&&String(p.action_notes||'').trim())await api('/api/actions/'+encodeURIComponent(nid),{method:'PUT',body:JSON.stringify({action_notes:p.action_notes})})};"
        "detailCancelItem();toast('Item guardado');loadTaskDetail(_detailTid)}catch(e){toast(e.message,true)}}"
        "async function detailDelItem()",
        html,
        count=1,
        flags=re.S,
    )
    html = html.replace(
        "renderDetailPlanFields(t,pl,ce);renderDetailLinks(t,ce)}",
        "renderDetailPlanFields(t,pl,ce);renderDetailLinks(t,ce);renderDetailFolder(t,_detailData?.folder||{},ce)}",
        1,
    )
    html = html.replace(
        "if($('td_links_edit'))t.Links=$('td_links_edit').value;return t}",
        "if($('td_links_edit'))t.Links=$('td_links_edit').value;"
        "if($('td_pasta_edit'))t.Pasta=$('td_pasta_edit').value.trim();return t}",
        1,
    )
    html = html.replace(
        "async function openTaskModal(tid,mode){",
        "function validateTaskModalRequired(){const req=[['tm_tarefa','Tarefa'],['tm_resp','Responsável'],['tm_estado','Estado'],['tm_prio','Prioridade'],['tm_milestone','Milestone'],['tm_projeto','Projeto'],['tm_linha','Linha'],['tm_maquina','Máquina'],['tm_prazo','Prazo']];"
        "const miss=[];req.forEach(([id,label])=>{const el=$(id);if(!el)return;const ok=String(el.value||'').trim().length>0;if(!ok)miss.push(label);el.style.borderColor=ok?'':'#dc2626'});"
        "if(miss.length){toast('Campos obrigatórios: '+miss.join(', '),true);return false}return true}"
        "function validateDetailActionRequired(){const kind=String($('td_item_kind')?.value||_detailFormKind||'ACTION').toUpperCase();if(kind!=='ACTION')return true;"
        "const req=[['td_item_text','Título/Texto'],['td_item_owner','Owner'],['td_item_due','Prazo']];"
        "const miss=[];req.forEach(([id,label])=>{const el=$(id);if(!el)return;const ok=String(el?.value||'').trim().length>0;if(!ok)miss.push(label);if(el.style)el.style.borderColor=ok?'':'#dc2626'});"
        "if(miss.length){toast('Campos obrigatórios: '+miss.join(', '),true);const first=req.find(([id])=>{const el=$(id);return el&&!String(el.value||'').trim()});if(first){try{$(first[0])?.focus()}catch(_){ }}return false}return true}"
        "async function openTaskModal(tid,mode){",
        1,
    )
    html = html.replace(
        "populateTaskForm(r);$('ttitle').textContent=r?.Tarefa||(mode==='new'?'Nova tarefa':'Tarefa');",
        "populateTaskForm(r);if($('tm_data_reg'))$('tm_data_reg').readOnly=true;$('ttitle').textContent=r?.Tarefa||(mode==='new'?'Nova tarefa':'Tarefa');",
        1,
    )
    html = html.replace(
        "async function saveTaskModal(){try{const p=taskPayload();const id=$('tm_id').value;const createFolder=!id&&$('tm_create_folder_on_save')&&$('tm_create_folder_on_save').checked;if(id){await api('/api/tasks/'+encodeURIComponent(id),{method:'PUT',body:JSON.stringify(p)})}else{let j=await api('/api/tasks',{method:'POST',body:JSON.stringify({...p,create_folder:createFolder})});$('tm_id').value=j.TaskID;taskSel=j.TaskID;if(j.folder&&j.folder.rel)$('tm_pasta').value=j.folder.rel}$('ttitle').textContent=$('tm_tarefa').value;toast('Tarefa guardada');closeTaskModal();loadTasks()}catch(e){toast(e.message,true)}}",
        "async function saveTaskModal(){try{if(!validateTaskModalRequired())return;const p=taskPayload();const id=$('tm_id').value;const createFolder=!id&&$('tm_create_folder_on_save')&&$('tm_create_folder_on_save').checked;if(id){await api('/api/tasks/'+encodeURIComponent(id),{method:'PUT',body:JSON.stringify(p)})}else{let j=await api('/api/tasks',{method:'POST',body:JSON.stringify({...p,create_folder:createFolder})});$('tm_id').value=j.TaskID;taskSel=j.TaskID;if(j.folder&&j.folder.rel)$('tm_pasta').value=j.folder.rel}$('ttitle').textContent=$('tm_tarefa').value;unsavedClear();toast('Tarefa guardada');closeTaskModal();loadTasks()}catch(e){toast(e.message,true)}}",
        1,
    )
    html = html.replace("function tdDl(name,items)", _FOLDER_JS + "function tdDl(name,items)", 1)
    html = re.sub(
        r"\$\('td_folder'\)\.textContent=\(fo\.rel\|\|[^;]+;",
        "void(0);",
        html,
        count=1,
    )
    html = re.sub(
        r'<section class="sec"><h3>Pasta</h3><div class="ro" id="td_folder">[^<]*</div></section>',
        _FOLDER_SECTION,
        html,
        count=1,
    )
    _detail_action_validate_fn = (
        "function validateDetailActionRequired(){const kind=String($('td_item_kind')?.value||_detailFormKind||'ACTION').toUpperCase();if(kind!=='ACTION')return true;"
        "const req=[['td_item_text','Título/Texto'],['td_item_owner','Owner'],['td_item_due','Prazo']];"
        "const miss=[];req.forEach(([id,label])=>{const el=$(id);if(!el)return;const ok=String(el?.value||'').trim().length>0;if(!ok)miss.push(label);if(el.style)el.style.borderColor=ok?'':'#dc2626'});"
        "if(miss.length){toast('Campos obrigatórios: '+miss.join(', '),true);const first=req.find(([id])=>{const el=$(id);return el&&!String(el.value||'').trim()});if(first){try{$(first[0])?.focus()}catch(_){ }}return false}return true}"
    )
    _task_complete_validate_fn = (
        "function _estadoIsConcluido(v){const s=String(v||'').trim().toLowerCase();return s==='concluído'||s==='concluido'}"
        "function _checklistItemIsDone(a){if(!a)return true;if(a.done||a.is_done)return true;"
        "const st=String(a.status||'').trim().toLowerCase();return st==='concluído'||st==='concluido'}"
        "function _openChecklistItems(items){return(items||[]).filter(a=>!_checklistItemIsDone(a))}"
        "function _taskCompleteBlockMessage(open){const acts=open.filter(a=>String(a.kind||'').toUpperCase()==='ACTION');"
        "const chks=open.filter(a=>String(a.kind||'').toUpperCase()!=='ACTION');const parts=[];"
        "if(acts.length){const na=acts.length;if(na===1)parts.push('1 ação («'+String(acts[0].item_text||'').slice(0,80)+'»)');"
        "else parts.push(na+' ações por concluir')}if(chks.length){const nc=chks.length;if(nc===1)parts.push('1 check («'+String(chks[0].item_text||'').slice(0,80)+'»)');"
        "else parts.push(nc+' checks por concluir')}return 'Não pode concluir a tarefa: ainda existem '+parts.join(' e ')+'.'}"
        "async function validateTaskCanComplete(estado,tid){if(!_estadoIsConcluido(estado))return true;const tidS=String(tid||'').trim();if(!tidS)return true;"
        "let open=[];if(String(_detailTid||'')===tidS&&typeof detailAllItems==='function'&&_detailData){open=_openChecklistItems(detailAllItems())}"
        "else{try{const j=await api('/api/tasks/'+encodeURIComponent(tidS)+'/detail');open=_openChecklistItems([...(j.actions||[]),...(j.checklist||[])])}catch(_){return true}}"
        "if(!open.length)return true;toast(_taskCompleteBlockMessage(open),true);return false}"
    )
    html = html.replace(
        "if(kind==='ACTION'){Object.assign(p,{owner:String($('td_item_owner')",
        "if(kind==='ACTION'){if(typeof validateDetailActionRequired==='function'&&!validateDetailActionRequired())return;"
        "Object.assign(p,{owner:String($('td_item_owner')",
        1,
    )
    if "function validateDetailActionRequired" not in html:
        _validate_prefix = _detail_action_validate_fn
        if "function validateTaskCanComplete" not in html:
            _validate_prefix += _task_complete_validate_fn
        if "function validateTaskModalRequired()" in html:
            html = html.replace(
                "function validateTaskModalRequired(){",
                _validate_prefix + "function validateTaskModalRequired(){",
                1,
            )
        elif "async function openTaskModal(tid,mode){" in html:
            html = html.replace(
                "async function openTaskModal(tid,mode){",
                _validate_prefix + "async function openTaskModal(tid,mode){",
                1,
            )
    elif "function validateTaskCanComplete" not in html:
        if "function validateDetailActionRequired()" in html:
            html = html.replace(
                "function validateDetailActionRequired(){",
                _task_complete_validate_fn + "function validateDetailActionRequired(){",
                1,
            )
        elif "function validateTaskModalRequired()" in html:
            html = html.replace(
                "function validateTaskModalRequired(){",
                _task_complete_validate_fn + "function validateTaskModalRequired(){",
                1,
            )
        elif "async function quickSetStatus(tid,current){" in html:
            html = html.replace(
                "async function quickSetStatus(tid,current){",
                _task_complete_validate_fn + "async function quickSetStatus(tid,current){",
                1,
            )
    if "validateTaskCanComplete(t.Estado,_detailTid)" not in html:
        html = html.replace(
            "async function saveTaskDetail(force){try{if(!_detailTid)return;const t=readDetailTaskFields();",
            "async function saveTaskDetail(force){try{if(!_detailTid)return;const t=readDetailTaskFields();"
            "if(typeof validateTaskCanComplete==='function'&&!await validateTaskCanComplete(t.Estado,_detailTid))return;",
            1,
        )
    if "validateTaskCanComplete(p.Estado,id)" not in html:
        html = html.replace(
            "const p=taskPayload();const id=$('tm_id').value;const createFolder=",
            "const p=taskPayload();const id=$('tm_id').value;"
            "if(id&&typeof validateTaskCanComplete==='function'&&!await validateTaskCanComplete(p.Estado,id))return;"
            "const createFolder=",
            1,
        )
    if "validateTaskCanComplete(pick,tid)" not in html:
        html = html.replace(
            "if(!states.includes(pick)){toast('Estado inválido',true);return}await api('/api/board/move',{method:'POST',body:JSON.stringify({task_id:tid,estado:pick})});",
            "if(!states.includes(pick)){toast('Estado inválido',true);return}"
            "if(typeof validateTaskCanComplete==='function'&&!await validateTaskCanComplete(pick,tid))return;"
            "await api('/api/board/move',{method:'POST',body:JSON.stringify({task_id:tid,estado:pick})});",
            1,
        )
    return _patch_html_tasks_visual(html)


_TASKS_KPIS_OLD = (
    '<section class="kpis"><div class="kpi"><div class="ico">📋</div><div><div class="muted">Total</div>'
    '<div class="v" id="tk_total">0</div></div></div><div class="kpi"><div class="ico">📝</div><div>'
    '<div class="muted">A fazer</div><div class="v" id="tk_open">0</div></div></div><div class="kpi">'
    '<div class="ico">✅</div><div><div class="muted">Concluídas</div><div class="v" id="tk_done">0</div></div></div>'
    '<div class="kpi"><div class="ico">⏰</div><div><div class="muted">Atrasadas</div><div class="v" id="tk_overdue">0</div>'
    '</div></div><div class="kpi"><div class="ico">🚫</div><div><div class="muted">Bloqueadas</div>'
    '<div class="v" id="tk_blocked">0</div></div></div></section>'
)

_TASKS_KPIS_NEW = (
    '<section class="kpis tk-kpis">'
    '<div class="kpi kpi-click" onclick="tkFilterKpi(\'all\')" title="Mostrar todas">'
    '<div class="ico">📋</div><div><div class="muted">Total</div><div class="v" id="tk_total">0</div>'
    '<div class="muted db-sub">Todas as tarefas</div></div></div>'
    '<div class="kpi kpi-click" onclick="tkFilterKpi(\'open\')" title="Filtrar abertas">'
    '<div class="ico">📝</div><div><div class="muted">A fazer</div><div class="v" id="tk_open">0</div>'
    '<div class="muted db-sub">Não concluídas</div></div></div>'
    '<div class="kpi kpi-click" onclick="tkFilterKpi(\'done\')" title="Filtrar concluídas">'
    '<div class="ico">✅</div><div><div class="muted">Concluídas</div><div class="v" id="tk_done">0</div>'
    '<div class="muted db-sub">Estado concluído</div></div></div>'
    '<div class="kpi kpi-click" onclick="tkFilterKpi(\'overdue\')" title="Filtrar atrasadas">'
    '<div class="ico">⏰</div><div><div class="muted">Atrasadas</div><div class="v" id="tk_overdue">0</div>'
    '<div class="muted db-sub">Prazo vencido</div></div></div>'
    '<div class="kpi kpi-click" onclick="tkFilterKpi(\'blocked\')" title="Filtrar bloqueadas">'
    '<div class="ico">🚫</div><div><div class="muted">Bloqueadas</div><div class="v" id="tk_blocked">0</div>'
    '<div class="muted db-sub">Com bloqueios</div></div></div>'
    "</section>"
)

_TASKS_FILTERS_OLD = (
    '<section class="card filters" id="task-filters"><b>🔎 Filtros</b><div class="grid" style="margin-top:14px">'
    '<div class="field"><label>Pesquisa</label><input id="tf_q" placeholder="TaskID, tarefa, responsável, workers..."></div>'
    '<div class="field"><label>Estado</label><select id="tf_estado"></select></div>'
    '<div class="field"><label>Prioridade</label><select id="tf_prio"></select></div>'
    '<div class="field"><label>Responsável</label><select id="tf_resp"></select></div>'
    '<div class="field"><label>Milestone</label><select id="tf_milestone"></select></div>'
    '<div class="field"><label>Assunto</label><select id="tf_assunto"></select></div>'
    '<div class="field"><label>Projeto</label><select id="tf_projeto"></select></div>'
    '<div class="field"><label>Linha</label><select id="tf_linha"></select></div>'
    '<div class="field"><label>Máquina</label><select id="tf_maquina"></select></div>'
    '<div class="field"><label>Prazo de</label><input id="tf_from" type="date"></div>'
    '<div class="field"><label>Prazo até</label><input id="tf_to" type="date"></div>'
    '<label style="padding-top:30px"><input type="checkbox" id="tf_mine"> Apenas minhas</label>'
    '<div class="field"><label>Envolvido</label><select id="tf_involved">'
    '<option value="">Todos</option><option value="1">Modo 1 — Responsável</option>'
    '<option value="2">Modo 2 — +Workers</option><option value="3">Modo 3 — +Ações</option></select></div>'
    '<label style="padding-top:30px"><input type="checkbox" id="tf_overdue"> Só atrasadas</label>'
    '<label style="padding-top:30px"><input type="checkbox" id="tf_blocked"> Só bloqueadas</label>'
    '<label style="padding-top:30px"><input type="checkbox" id="tf_show_done" onchange="loadTasks()"> Concluídas</label>'
    '<button class="btn" onclick="clearTaskFilters()">Limpar filtros</button></div></section>'
)

_TASKS_FILTERS_NEW = (
    '<section class="card filters tk-filters" id="task-filters" style="padding:12px">'
    '<div class="tk-filters-head"><b>🔎 Filtros</b>'
    '<button type="button" class="btn tk-adv-toggle" onclick="tasksToggleAdvanced()">'
    '<span id="tk_adv_lbl">Mostrar avançados</span></button>'
    '<button class="btn" type="button" onclick="clearTaskFilters()">Limpar filtros</button></div>'
    '<div class="grid tk-filters-main">'
    '<div class="field span2"><label>Pesquisa</label>'
    '<input id="tf_q" placeholder="TaskID, tarefa, responsável, workers..."></div>'
    '<div class="field"><label>Estado</label><select id="tf_estado"></select></div>'
    '<div class="field"><label>Prioridade</label><select id="tf_prio"></select></div>'
    '<div class="field"><label>Responsável</label><select id="tf_resp"></select></div>'
    '<div class="field"><label>Projeto</label><select id="tf_projeto"></select></div>'
    '<div class="field tk-chips-wrap"><label>Filtros rápidos</label>'
    '<div class="tk-chips">'
    '<label class="tk-chip"><input type="checkbox" id="tf_mine"> Minhas</label>'
    '<label class="tk-chip"><input type="checkbox" id="tf_overdue"> Atrasadas</label>'
    '<label class="tk-chip"><input type="checkbox" id="tf_blocked"> Bloqueadas</label>'
    '<label class="tk-chip"><input type="checkbox" id="tf_show_done"> Concluídas</label>'
    "</div></div></div>"
    '<div class="grid tk-filters-adv" id="tk_filters_adv" style="display:none">'
    '<div class="field"><label>Milestone</label><select id="tf_milestone"></select></div>'
    '<div class="field"><label>Assunto</label><select id="tf_assunto"></select></div>'
    '<div class="field"><label>Linha</label><select id="tf_linha"></select></div>'
    '<div class="field"><label>Máquina</label><select id="tf_maquina"></select></div>'
    '<div class="field"><label>Prazo de</label><input id="tf_from" type="date"></div>'
    '<div class="field"><label>Prazo até</label><input id="tf_to" type="date"></div>'
    '<div class="field"><label>Envolvido</label><select id="tf_involved">'
    '<option value="">Todos</option><option value="1">Modo 1 — Responsável</option>'
    '<option value="2">Modo 2 — +Workers</option><option value="3">Modo 3 — +Ações</option></select></div>'
    "</div></section>"
)

_TASKS_TOOLBAR_OLD = (
    '<div class="toolbar"><button class="btn primary" id="tb_new" onclick="newTask()">＋ Nova</button>'
    '<button class="btn" id="tb_edit" onclick="editTaskSel()" disabled>Editar</button>'
    '<button class="btn" id="tb_dup" onclick="dupTaskSel()" disabled>Duplicar</button>'
    '<button class="btn danger" id="tb_del" onclick="delTaskSel()" disabled>Apagar</button>'
    '<button class="btn" onclick="exportTasksCsv()">Exportar CSV</button>'
    '<button class="btn" onclick="openPortfolioGantt()">📊 Gantt</button>'
    '<button class="btn" onclick="exportTasksXls()">Exportar Excel</button>'
    '<button class="btn" onclick="openExcelFiltersModal()">Filtros Excel</button>'
    '<button class="btn" onclick="openTaskColsModal()">Colunas</button>'
    '<button class="btn" onclick="taskColsAutoFit()">Auto-ajustar</button>'
    '<button class="btn" id="tb_folder" onclick="openTaskFolderSel()" disabled>Abrir pasta</button>'
    '<button class="btn" id="tb_link" onclick="openTaskLinkSel()" disabled>Abrir link</button>'
    '<button class="btn" onclick="openArchiveModal()">Arquivo</button>'
    '<span class="spacer"></span><button class="btn" onclick="loadTasks()">Atualizar</button></div>'
)

_TASKS_TOOLBAR_NEW = (
    '<div class="toolbar tk-toolbar">'
    '<div class="tk-tb-group">'
    '<button class="btn primary" id="tb_new" onclick="newTask()">＋ Nova</button>'
    '<button class="btn" id="tb_edit" onclick="editTaskSel()" disabled>Editar</button>'
    '<button class="btn" id="tb_dup" onclick="dupTaskSel()" disabled>Duplicar</button>'
    '<button class="btn danger" id="tb_del" onclick="delTaskSel()" disabled>Apagar</button>'
    "</div><div class=\"tk-tb-sep\"></div><div class=\"tk-tb-group\">"
    '<button class="btn" onclick="exportTasksCsv()">Exportar CSV</button>'
    '<button class="btn" onclick="openPortfolioGantt()">📊 Gantt</button>'
    '<button class="btn" onclick="exportTasksXls()">Exportar Excel</button>'
    '<button class="btn" onclick="openExcelFiltersModal()">Filtros Excel</button>'
    "</div><div class=\"tk-tb-sep\"></div><div class=\"tk-tb-group\">"
    '<button class="btn" onclick="openTaskColsModal()">Colunas</button>'
    '<button class="btn" onclick="taskColsAutoFit()">Auto-ajustar</button>'
    '<button class="btn" id="tb_folder" onclick="openTaskFolderSel()" disabled>Abrir pasta</button>'
    '<button class="btn" id="tb_link" onclick="openTaskLinkSel()" disabled>Abrir link</button>'
    '<button class="btn" onclick="openArchiveModal()">Arquivo</button>'
    "</div><span class=\"spacer\"></span>"
    '<button class="btn" onclick="loadTasks()">Atualizar</button></div>'
)

_TASKS_FOOTER_HTML = (
    '<section class="card tasks-footer" id="tasks-footer">'
    '<div class="tk-foot-grid">'
    '<div class="tk-foot-stats">'
    '<div class="tk-foot-item"><span class="tk-foot-lbl">Atrasadas</span>'
    '<span class="tk-foot-v" id="tk_f_overdue">0</span>'
    '<span class="tk-foot-pct muted" id="tk_f_overdue_pct">0%</span></div>'
    '<div class="tk-foot-item"><span class="tk-foot-lbl">Em risco</span>'
    '<span class="tk-foot-v" id="tk_f_risk">0</span>'
    '<span class="tk-foot-pct muted" id="tk_f_risk_pct">0%</span></div>'
    '<div class="tk-foot-item"><span class="tk-foot-lbl">Concluídas</span>'
    '<span class="tk-foot-v" id="tk_f_done">0</span>'
    '<span class="tk-foot-pct muted" id="tk_f_done_pct">0%</span></div>'
    '<div class="tk-foot-item"><span class="tk-foot-lbl">Bloqueadas</span>'
    '<span class="tk-foot-v" id="tk_f_blocked">0</span>'
    '<span class="tk-foot-pct muted" id="tk_f_blocked_pct">0%</span></div>'
    '<div class="tk-foot-item"><span class="tk-foot-lbl">Sem prazo</span>'
    '<span class="tk-foot-v" id="tk_f_nodue">0</span>'
    '<span class="tk-foot-pct muted" id="tk_f_nodue_pct">0%</span></div>'
    "</div>"
    '<div class="tk-foot-ring-wrap"><div class="tk-foot-ring">'
    '<svg viewBox="0 0 36 36" class="tk-ring" aria-hidden="true">'
    '<path class="tk-ring-bg" d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"/>'
    '<path class="tk-ring-fg" id="tk_ring_fg" stroke-dasharray="0,100" '
    'd="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"/></svg>'
    '<div class="tk-ring-lbl"><span id="tk_prog_pct">0%</span><span class="muted">Progresso</span></div>'
    "</div></div></div></section>"
)

_TASKS_V1_CSS = (
    "#page-tasks .tk-kpis .kpi{min-height:92px}"
    "#page-tasks .tk-filters-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px}"
    "#page-tasks .tk-filters-head .btn{margin-left:auto}"
    "#page-tasks .tk-filters-head .tk-adv-toggle{margin-left:0}"
    "#page-tasks .tk-filters-main{display:grid;grid-template-columns:repeat(6,minmax(120px,1fr));gap:10px;align-items:end}"
    "#page-tasks .tk-filters-main .field{margin:0;min-width:0}"
    "#page-tasks .tk-filters-main .span2{grid-column:span 2}"
    "#page-tasks .tk-chips-wrap{grid-column:1/-1}"
    "#page-tasks .tk-chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:4px}"
    "#page-tasks .tk-chip{display:inline-flex;align-items:center;gap:6px;padding:6px 10px;border-radius:999px;"
    "background:#f1f5f9;border:1px solid #dbe3ee;font-size:12px;cursor:pointer;user-select:none}"
    "#page-tasks .tk-chip input{margin:0}"
    "#page-tasks .tk-filters-adv{display:grid;grid-template-columns:repeat(4,minmax(120px,1fr));gap:10px;margin-top:10px;padding-top:10px;border-top:1px dashed #dbe3ee}"
    "#page-tasks .tk-toolbar{gap:8px;align-items:center}"
    "#page-tasks .tk-tb-group{display:flex;flex-wrap:wrap;gap:6px;align-items:center}"
    "#page-tasks .tk-tb-sep{width:1px;height:28px;background:#dbe3ee;margin:0 2px}"
    "#page-tasks .tasks-footer{margin-top:12px;padding:12px 14px}"
    "#page-tasks .tk-foot-grid{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}"
    "#page-tasks .tk-foot-stats{display:flex;flex-wrap:wrap;gap:14px 18px;flex:1;min-width:280px}"
    "#page-tasks .tk-foot-item{display:flex;flex-direction:column;gap:2px;min-width:72px}"
    "#page-tasks .tk-foot-lbl{font-size:11px;color:#64748b}"
    "#page-tasks .tk-foot-v{font-size:18px;font-weight:700;line-height:1.1}"
    "#page-tasks .tk-foot-pct{font-size:11px}"
    "#page-tasks .tk-foot-ring-wrap{display:flex;justify-content:flex-end}"
    "#page-tasks .tk-foot-ring{position:relative;width:72px;height:72px}"
    "#page-tasks .tk-ring{width:100%;height:100%;transform:rotate(-90deg)}"
    "#page-tasks .tk-ring-bg{fill:none;stroke:#e5e7eb;stroke-width:3.2}"
    "#page-tasks .tk-ring-fg{fill:none;stroke:#16a34a;stroke-width:3.2;stroke-linecap:round;transition:stroke-dasharray .25s ease}"
    "#page-tasks .tk-ring-lbl{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;font-size:11px;line-height:1.15}"
    "#page-tasks .tk-ring-lbl span:first-child{font-size:15px;font-weight:700;color:#166534}"
    "#page-tasks .tk-pill{display:inline-flex;align-items:center;padding:2px 9px;border-radius:999px;font-size:11px;font-weight:600;white-space:nowrap}"
    "#page-tasks .tk-pill-wait{background:#e2e8f0;color:#334155}"
    "#page-tasks .tk-pill-prog{background:#dbeafe;color:#1d4ed8}"
    "#page-tasks .tk-pill-done{background:#dcfce7;color:#166534}"
    "#page-tasks .tk-pill-block{background:#ffedd5;color:#9a3412}"
    "#page-tasks .tk-pill-prio-low{background:#ecfdf5;color:#047857}"
    "#page-tasks .tk-pill-prio-med{background:#fef9c3;color:#854d0e}"
    "#page-tasks .tk-pill-prio-high{background:#fee2e2;color:#991b1b}"
    "#page-tasks #trows tr[class*='tk-row-'] td:first-child{position:relative}"
    "#page-tasks #trows tr[class*='tk-row-'] td:first-child::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;border-radius:0 2px 2px 0}"
    "#page-tasks #trows tr.tk-row-overdue td:first-child::before{background:#dc2626}"
    "#page-tasks #trows tr.tk-row-blocked td:first-child::before{background:#ea580c}"
    "#page-tasks #trows tr.tk-row-progress td:first-child::before{background:#2563eb}"
    "#page-tasks #trows tr.tk-row-done td:first-child::before{background:#16a34a}"
    "#page-tasks .tk-due-main{font-weight:600}"
    "#page-tasks .tk-due-sub{font-size:11px;color:#b91c1c;margin-top:2px}"
    "#page-tasks .tk-due-ok .tk-due-sub{color:#64748b}"
    "@media(max-width:1200px){#page-tasks .tk-filters-main{grid-template-columns:repeat(3,minmax(120px,1fr))}"
    "#page-tasks .tk-filters-main .span2{grid-column:span 2}}"
    "@media(max-width:760px){#page-tasks .tk-filters-main{grid-template-columns:repeat(2,minmax(110px,1fr))}"
    "#page-tasks .tk-tb-sep{display:none}}"
)

_TASKS_V1_JS = (
    "function tasksToggleAdvanced(){const el=$('tk_filters_adv');const lbl=$('tk_adv_lbl');"
    "const show=!!(el&&el.style.display==='none');if(el)el.style.display=show?'grid':'none';"
    "if(lbl)lbl.textContent=show?'Ocultar avançados':'Mostrar avançados'}"
    "function tasksBindFilterChips(){if(window._tkChipsBound)return;window._tkChipsBound=true;"
    "['tf_mine','tf_overdue','tf_blocked','tf_show_done'].forEach(id=>{const el=$(id);if(!el||el.dataset.tkChipBound)return;"
    "el.dataset.tkChipBound='1';el.addEventListener('change',()=>{saveTaskFilters();loadTasks()})})}"
    "function tkFilterKpi(kind){const k=String(kind||'all');"
    "if(k==='all'){clearTaskFilters();return}"
    "if($('tf_q'))$('tf_q').value='';if($('tf_estado'))$('tf_estado').value='Todos';"
    "if($('tf_prio'))$('tf_prio').value='Todos';if($('tf_resp'))$('tf_resp').value='Todos';"
    "if($('tf_projeto'))$('tf_projeto').value='Todos';"
    "if($('tf_mine'))$('tf_mine').checked=false;"
    "if($('tf_overdue'))$('tf_overdue').checked=(k==='overdue');"
    "if($('tf_blocked'))$('tf_blocked').checked=(k==='blocked');"
    "if($('tf_show_done'))$('tf_show_done').checked=(k==='done');"
    "if(k==='open'&&$('tf_show_done'))$('tf_show_done').checked=false;"
    "saveTaskFilters();loadTasks()}"
    "function _taskDaysToDue(r){if(typeof _taskIsDone==='function'&&_taskIsDone(r))return null;"
    "const d=String(r?.Prazo||'').slice(0,10);if(!d)return null;"
    "const t=new Date();t.setHours(0,0,0,0);const dt=new Date(d+'T00:00:00');if(isNaN(dt))return null;"
    "return Math.floor((dt.getTime()-t.getTime())/86400000)}"
    "function _taskIsRisk(r){if(typeof _taskIsDone==='function'&&_taskIsDone(r))return false;"
    "const dd=_taskDaysToDue(r);return dd!=null&&dd>=0&&dd<=7}"
    "function tasksUpdateFooter(){const rows=taskRows||[];const total=rows.length||0;"
    "const pct=n=>total?Math.round(n*100/total):0;"
    "const done=rows.filter(r=>typeof _taskIsDone==='function'?_taskIsDone(r):String(r?.Estado||'').trim().toLowerCase()==='concluído').length;"
    "const overdue=rows.filter(r=>(typeof _taskIsDone==='function'?!_taskIsDone(r):true)&&!!r?.is_overdue).length;"
    "const blocked=rows.filter(r=>Number(r?.blocked_count||0)>0).length;"
    "const risk=rows.filter(r=>_taskIsRisk(r)).length;"
    "const nodue=rows.filter(r=>!String(r?.Prazo||'').trim()).length;"
    "const set=(id,v)=>{const el=$(id);if(el)el.textContent=v};"
    "set('tk_f_overdue',overdue);set('tk_f_overdue_pct',pct(overdue)+'%');"
    "set('tk_f_risk',risk);set('tk_f_risk_pct',pct(risk)+'%');"
    "set('tk_f_done',done);set('tk_f_done_pct',pct(done)+'%');"
    "set('tk_f_blocked',blocked);set('tk_f_blocked_pct',pct(blocked)+'%');"
    "set('tk_f_nodue',nodue);set('tk_f_nodue_pct',pct(nodue)+'%');"
    "const pp=pct(done);set('tk_prog_pct',pp+'%');const ring=$('tk_ring_fg');if(ring)ring.setAttribute('stroke-dasharray',pp+',100')}"
    "function _taskRowAccent(r){const st=String(r?.Estado||'').trim().toLowerCase();"
    "if(st==='concluído'||st==='concluido')return 'tk-row-done';"
    "if((r?.blocked_count||0)>0)return 'tk-row-blocked';"
    "if(r?.is_overdue)return 'tk-row-overdue';"
    "if(st.includes('progress')||st.includes('curso'))return 'tk-row-progress';return ''}"
    "function _taskIsDone(r){const st=String(r?.Estado||'').trim().toLowerCase();return st==='concluído'||st==='concluido'}"
    "function _taskDueCell(r){const main=_taskDate(r?.Prazo);if(_taskIsDone(r))return `<div class=\"tk-due-main\">${main||'—'}</div>`;"
    "const badge=r?.due_badge||{};const txt=String(badge.text||'').trim();"
    "if(txt&&!txt.toLowerCase().includes('atras')){const cls='tk-due-sub tk-due-ok';"
    "return `<div class=\"tk-due-main\">${main||'—'}</div><div class=\"${cls}\">${esc(txt)}</div>`}"
    "if(txt&&txt.toLowerCase().includes('atras')){return `<div class=\"tk-due-main\">${main||'—'}</div><div class=\"tk-due-sub\">${esc(txt)}</div>`}"
    "const dd=_taskDaysToDue(r);if(dd!=null&&dd<0){return `<div class=\"tk-due-main\">${main||'—'}</div><div class=\"tk-due-sub\">Atraso ${Math.abs(dd)}d</div>`}"
    "return `<div class=\"tk-due-main\">${main||'—'}</div>`}"
)

_TE_BADGE_OLD = (
    "function teBadge(e){let s=String(e||'').trim();if(!s)s='Não iniciado';let c='te';"
    "if(s==='Concluído')c='te-done';else if(s==='Não iniciado'||s==='A Fazer')c='te-wait';"
    "else if(s==='Em Progresso')c='te-prog';return `<span class=\"badge ${c}\">${esc(s)}</span>`}"
)

_TE_BADGE_NEW = (
    "function teBadge(e){let s=String(e||'').trim();if(!s)s='Não iniciado';let c='tk-pill-wait';"
    "if(s==='Concluído')c='tk-pill-done';else if(s==='Em Progresso')c='tk-pill-prog';"
    "else if(s.toLowerCase().includes('bloque'))c='tk-pill-block';"
    "return `<span class=\"tk-pill ${c}\">${esc(s)}</span>`}"
)

_TP_BADGE_OLD = (
    "function tpBadge(p){const s=String(p||'');let c='tp-med';if(s==='Baixa')c='tp-low';"
    "if(s==='Alta')c='tp-high';return `<span class=\"badge ${c}\">${esc(s)}</span>`}"
)

_TP_BADGE_NEW = (
    "function tpBadge(p){const s=String(p||'');let c='tk-pill-prio-med';if(s==='Baixa')c='tk-pill-prio-low';"
    "if(s==='Alta')c='tk-pill-prio-high';return `<span class=\"tk-pill ${c}\">${esc(s)}</span>`}"
)

_TASK_COL_CELL_PRAZO_OLD = (
    "if(c==='DataRegisto'||c==='Prazo'||c==='InicioPrevisto'||c==='DataConclusao')return `<td data-col=\"${c}\">${_taskDate(r[c])}</td>`;"
)

_TASK_COL_CELL_PRAZO_NEW = (
    "if(c==='Prazo')return `<td data-col=\"${c}\">${_taskDueCell(r)}</td>`;"
    "if(c==='DataRegisto'||c==='InicioPrevisto'||c==='DataConclusao')return `<td data-col=\"${c}\">${_taskDate(r[c])}</td>`;"
)

_RENDER_TASKS_ROW_OLD = (
    "taskRows.forEach(r=>{let tr=document.createElement('tr');if(taskSel===r.TaskID)tr.className='sel';if(r.is_overdue)tr.classList.add('row-overdue');"
)

_RENDER_TASKS_ROW_NEW = (
    "taskRows.forEach(r=>{let tr=document.createElement('tr');const _acc=_taskRowAccent(r);"
    "if(taskSel===r.TaskID)tr.className='sel';if(_acc)tr.classList.add(_acc);if(r.is_overdue)tr.classList.add('row-overdue');"
)

_LOAD_TASKS_FOOTER_OLD = (
    "if(page==='tasks')$('upd').textContent='Última atualização: '+new Date().toLocaleTimeString('pt-PT')}catch(e){toast(e.message,true)}}"
)

_LOAD_TASKS_FOOTER_NEW = (
    "tasksBindFilterChips();tasksLoadPagePrefs();tasksUpdateFooter();"
    "if(page==='tasks')$('upd').textContent='Última atualização: '+new Date().toLocaleTimeString('pt-PT')}catch(e){toast(e.message,true)}}"
)

_TASKS_TABLE_PAGER_HTML = (
    '<div class="tasks-pager" id="tasks-pager">'
    '<span class="muted" id="tk_pager_info">0 tarefas</span>'
    '<div class="tk-pager-controls">'
    '<button type="button" class="btn" id="tk_pg_first" onclick="tasksSetPage(1)" disabled title="Primeira página">«</button>'
    '<button type="button" class="btn" id="tk_pg_prev" onclick="tasksSetPage(_taskPage-1)" disabled title="Anterior">‹</button>'
    '<span id="tk_pg_label" class="tk-pager-label">1 / 1</span>'
    '<button type="button" class="btn" id="tk_pg_next" onclick="tasksSetPage(_taskPage+1)" disabled title="Seguinte">›</button>'
    '<button type="button" class="btn" id="tk_pg_last" onclick="tasksSetPage(_taskPageLast)" disabled title="Última página">»</button>'
    '<label class="tk-pager-size">Mostrar '
    '<select id="tk_page_size" onchange="tasksSetPageSize(this.value)">'
    '<option value="25">25</option><option value="50">50</option>'
    '<option value="100">100</option><option value="0">Todas</option></select></label>'
    "</div></div>"
)

_TASKS_TABLE_CARD_OLD = (
    '<section class="card"><div class="table-wrap"><table><thead><tr><th></th>'
    '<th class="sortable" onclick="sortTasks(\'TaskID\')">TaskID</th>'
    '<th class="sortable" onclick="sortTasks(\'Tarefa\')">Tarefa</th><th>Notif.</th>'
    '<th class="sortable" onclick="sortTasks(\'Notificacoes\')">Notificações</th>'
)

_TASKS_TABLE_CARD_NEW = (
    '<section class="card tk-table-card">' + _TASKS_TABLE_PAGER_HTML + '<div class="table-wrap"><table><thead><tr><th></th>'
    '<th class="sortable" onclick="sortTasks(\'TaskID\')">TaskID</th>'
    '<th class="sortable" onclick="sortTasks(\'Tarefa\')">Tarefa</th><th>Notif.</th>'
    '<th class="sortable" onclick="sortTasks(\'Notificacoes\')">Notificações</th>'
)

_TASKS_TOOLBAR_V2_OLD = _TASKS_TOOLBAR_NEW

_TASKS_TOOLBAR_V2_NEW = (
    '<div class="toolbar tk-toolbar">'
    '<div class="tk-tb-group">'
    '<button class="btn primary" id="tb_new" onclick="newTask()">＋ Nova</button>'
    '<button class="btn" id="tb_edit" onclick="editTaskSel()" disabled>Editar</button>'
    '<button class="btn" id="tb_dup" onclick="dupTaskSel()" disabled>Duplicar</button>'
    '<button class="btn danger" id="tb_del" onclick="delTaskSel()" disabled>Apagar</button>'
    "</div><div class=\"tk-tb-sep\"></div><div class=\"tk-tb-group\">"
    '<button class="btn" onclick="openPortfolioGantt()">📊 Gantt</button>'
    '<button class="btn" onclick="openTaskColsModal()">Colunas</button>'
    '<button class="btn" onclick="taskColsAutoFit()">Auto-ajustar</button>'
    '<button class="btn" id="tb_folder" onclick="openTaskFolderSel()" disabled>Abrir pasta</button>'
    '<button class="btn" id="tb_link" onclick="openTaskLinkSel()" disabled>Abrir link</button>'
    "</div><div class=\"tk-tb-sep\"></div><div class=\"tk-tb-group tk-tb-more-wrap\">"
    '<button type="button" class="btn" onclick="tasksToggleMoreMenu(event)">Mais acções ▾</button>'
    '<div class="tk-tb-menu" id="tk_tb_menu" style="display:none">'
    '<button type="button" class="btn" onclick="exportTasksCsv();tasksCloseMoreMenu()">Exportar CSV</button>'
    '<button type="button" class="btn" onclick="exportTasksXls();tasksCloseMoreMenu()">Exportar Excel</button>'
    '<button type="button" class="btn" onclick="openExcelFiltersModal();tasksCloseMoreMenu()">Filtros Excel</button>'
    '<button type="button" class="btn" onclick="openArchiveModal();tasksCloseMoreMenu()">Arquivo</button>'
    "</div></div><span class=\"spacer\"></span>"
    '<button class="btn" onclick="loadTasks()">Atualizar</button></div>'
)

_TASKS_V2_CSS = (
    "#page-tasks .tk-table-card{padding-top:10px}"
    "#page-tasks .tasks-pager{display:flex;align-items:center;justify-content:space-between;gap:12px;"
    "flex-wrap:wrap;padding:0 12px 10px;border-bottom:1px solid #e5e7eb;margin-bottom:0}"
    "#page-tasks .tk-pager-controls{display:flex;align-items:center;gap:6px;flex-wrap:wrap}"
    "#page-tasks .tk-pager-label{min-width:56px;text-align:center;font-size:12px;font-weight:600;color:#334155}"
    "#page-tasks .tk-pager-size{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:#64748b;margin-left:4px}"
    "#page-tasks .tk-pager-size select{padding:6px 8px;border-radius:8px;border:1px solid #d7dde8;background:#fff}"
    "#page-tasks .tk-tb-more-wrap{position:relative}"
    "#page-tasks .tk-tb-menu{position:absolute;top:calc(100% + 4px);left:0;z-index:30;display:none;"
    "min-width:180px;padding:6px;background:#fff;border:1px solid #dbe3ee;border-radius:10px;"
    "box-shadow:0 8px 24px rgba(15,23,42,.12);flex-direction:column;gap:4px}"
    "#page-tasks .tk-tb-menu .btn{width:100%;justify-content:flex-start;text-align:left}"
)

_TASKS_V2_JS = (
    "let _taskPage=1,_taskPageSize=25,_taskPageLast=1;"
    "function tasksLoadPagePrefs(){if(window._tkPagePrefsLoaded)return;window._tkPagePrefsLoaded=true;"
    "try{const v=localStorage.getItem('taskPageSize');if(v!=null){const n=Number(v);"
    "if([25,50,100,0].includes(n)){_taskPageSize=n;const s=$('tk_page_size');if(s)s.value=String(n)}}}catch(_){}}"
    "function tasksSavePageSize(){try{localStorage.setItem('taskPageSize',String(_taskPageSize))}catch(_){}}"
    "function tasksResetPage(){_taskPage=1;tasksClampPage()}"
    "function tasksClampPage(){const total=(taskRows||[]).length;const ps=Number(_taskPageSize||25);"
    "_taskPageLast=(!ps||ps<=0)?1:Math.max(1,Math.ceil(total/ps)||1);"
    "if(_taskPage>_taskPageLast)_taskPage=_taskPageLast;if(_taskPage<1)_taskPage=1}"
    "function tasksPagerSlice(rows){const all=rows||[];tasksClampPage();const ps=Number(_taskPageSize||25);"
    "if(!ps||ps<=0)return all;const start=(_taskPage-1)*ps;return all.slice(start,start+ps)}"
    "function tasksUpdatePager(){tasksClampPage();const total=(taskRows||[]).length;const ps=Number(_taskPageSize||25);"
    "const start=total?(ps&&ps>0?(_taskPage-1)*ps+1:1):0;const end=ps&&ps>0?Math.min(total,_taskPage*ps):total;"
    "const info=$('tk_pager_info');if(info)info.textContent=total?(start+'–'+end+' de '+total):'0 tarefas';"
    "const lbl=$('tk_pg_label');if(lbl)lbl.textContent=_taskPage+' / '+_taskPageLast;"
    "const disFirst=_taskPage<=1;const disLast=_taskPage>=_taskPageLast||total===0;"
    "['tk_pg_first','tk_pg_prev'].forEach(id=>{const b=$(id);if(b)b.disabled=disFirst});"
    "['tk_pg_next','tk_pg_last'].forEach(id=>{const b=$(id);if(b)b.disabled=disLast})}"
    "function tasksSetPage(n){tasksClampPage();const p=Math.max(1,Math.min(_taskPageLast,Number(n)||1));"
    "if(p===_taskPage)return;_taskPage=p;renderTasks();"
    "const wrap=$('trows')?.closest('.table-wrap');if(wrap)wrap.scrollTop=0}"
    "function tasksSetPageSize(v){const n=Number(v);_taskPageSize=[25,50,100,0].includes(n)?n:25;"
    "_taskPage=1;tasksSavePageSize();renderTasks()}"
    "function tasksToggleMoreMenu(e){if(e){e.preventDefault();e.stopPropagation()}const m=$('tk_tb_menu');if(!m)return;"
    "const open=m.style.display!=='none';m.style.display=open?'none':'flex';"
    "if(!open){const close=ev=>{if(ev&&m.contains(ev.target))return;m.style.display='none';document.removeEventListener('click',close)};"
    "setTimeout(()=>document.addEventListener('click',close),0)}}"
    "function tasksCloseMoreMenu(){const m=$('tk_tb_menu');if(m)m.style.display='none'}"
)

_LOAD_TASKS_ROWS_OLD = "let _rows=(j.rows||[]);taskRows=_mineApplyRows(_rows);"

_LOAD_TASKS_ROWS_NEW = (
    "let _rows=(j.rows||[]);taskRows=_mineApplyRows(_rows);"
    "if(typeof tasksResetPage==='function')tasksResetPage();"
)

_SORT_TASKS_RENDER_OLD = "});renderTasks();if(!silent)toast('Ordenado:"

_SORT_TASKS_RENDER_NEW = "});if(typeof tasksClampPage==='function')tasksClampPage();renderTasks();if(!silent)toast('Ordenado:"

_RENDER_SQL_LOOP_OLD = (
    "const cols=_taskColsNormalize(_taskColsVisible);taskRows.forEach(r=>{let tr=document.createElement('tr');"
    "if(taskSel===r.TaskID)tr.className='sel';if(r.is_overdue)tr.classList.add('row-overdue');"
    "if((r.blocked_count||0)>0)tr.classList.add('row-blocked');"
)

_RENDER_SQL_LOOP_NEW = (
    "const cols=_taskColsNormalize(_taskColsVisible);"
    "const _pageRows=(typeof tasksPagerSlice==='function')?tasksPagerSlice(taskRows):taskRows;"
    "_pageRows.forEach(r=>{let tr=document.createElement('tr');"
    "const _acc=(typeof _taskRowAccent==='function')?_taskRowAccent(r):'';"
    "if(taskSel===r.TaskID)tr.className='sel';if(_acc)tr.classList.add(_acc);if(r.is_overdue)tr.classList.add('row-overdue');"
    "if((r.blocked_count||0)>0)tr.classList.add('row-blocked');"
)

_RENDER_SQL_PAGER_OLD = "tb.appendChild(tr)});const has=!!taskSel;if($('tb_edit'))"

_RENDER_SQL_PAGER_NEW = (
    "tb.appendChild(tr)});if(typeof tasksUpdatePager==='function')tasksUpdatePager();"
    "const has=!!taskSel;if($('tb_edit'))"
)


def _patch_html_tasks_visual_v2(html: str) -> str:
    if 'id="tasks-pager"' not in html and _TASKS_TABLE_CARD_OLD in html:
        html = html.replace(_TASKS_TABLE_CARD_OLD, _TASKS_TABLE_CARD_NEW, 1)
    if _TASKS_TOOLBAR_V2_OLD in html and _TASKS_TOOLBAR_V2_OLD != _TASKS_TOOLBAR_V2_NEW:
        html = html.replace(_TASKS_TOOLBAR_V2_OLD, _TASKS_TOOLBAR_V2_NEW, 1)
    if "#page-tasks .tasks-pager" not in html:
        html = html.replace("</style>", _TASKS_V2_CSS + "</style>", 1)
    if "function tasksPagerSlice" not in html:
        marker = "function tasksToggleAdvanced()"
        if marker in html:
            html = html.replace(marker, _TASKS_V2_JS + marker, 1)
    if _LOAD_TASKS_ROWS_OLD in html:
        html = html.replace(_LOAD_TASKS_ROWS_OLD, _LOAD_TASKS_ROWS_NEW, 1)
    if _SORT_TASKS_RENDER_OLD in html:
        html = html.replace(_SORT_TASKS_RENDER_OLD, _SORT_TASKS_RENDER_NEW, 1)
    if _RENDER_SQL_LOOP_OLD in html:
        html = html.replace(_RENDER_SQL_LOOP_OLD, _RENDER_SQL_LOOP_NEW, 1)
    if _RENDER_SQL_PAGER_OLD in html:
        html = html.replace(_RENDER_SQL_PAGER_OLD, _RENDER_SQL_PAGER_NEW, 1)
    return html


def _patch_html_tasks_visual(html: str) -> str:
    if _TASKS_KPIS_OLD in html:
        html = html.replace(_TASKS_KPIS_OLD, _TASKS_KPIS_NEW, 1)
    if _TASKS_FILTERS_OLD in html:
        html = html.replace(_TASKS_FILTERS_OLD, _TASKS_FILTERS_NEW, 1)
    if _TASKS_TOOLBAR_OLD in html:
        html = html.replace(_TASKS_TOOLBAR_OLD, _TASKS_TOOLBAR_NEW, 1)
    if 'id="tasks-footer"' not in html:
        html = html.replace(
            '</div></section></div><div id="page-task-detail"',
            '</div></section>' + _TASKS_FOOTER_HTML + '</div><div id="page-task-detail"',
            1,
        )
    if "#page-tasks .tk-kpis" not in html:
        html = html.replace("</style>", _TASKS_V1_CSS + "</style>", 1)
    if "function tasksToggleAdvanced()" not in html:
        marker = "function _taskDate(v){return esc(String(v||'').slice(0,10))}"
        if marker in html:
            html = html.replace(marker, _TASKS_V1_JS + marker, 1)
    if _TE_BADGE_OLD in html:
        html = html.replace(_TE_BADGE_OLD, _TE_BADGE_NEW, 1)
    if _TP_BADGE_OLD in html:
        html = html.replace(_TP_BADGE_OLD, _TP_BADGE_NEW, 1)
    if _TASK_COL_CELL_PRAZO_OLD in html:
        html = html.replace(_TASK_COL_CELL_PRAZO_OLD, _TASK_COL_CELL_PRAZO_NEW, 1)
    if _RENDER_TASKS_ROW_OLD in html:
        html = html.replace(_RENDER_TASKS_ROW_OLD, _RENDER_TASKS_ROW_NEW, 1)
    if _LOAD_TASKS_FOOTER_OLD in html:
        html = html.replace(_LOAD_TASKS_FOOTER_OLD, _LOAD_TASKS_FOOTER_NEW, 1)
    return _patch_html_tasks_visual_v3(_patch_html_tasks_visual_v2(html))


_TASK_DUE_CELL_OLD = (
    "function _taskDueCell(r){const main=_taskDate(r?.Prazo);"
    "const badge=r?.due_badge||{};const txt=String(badge.text||'').trim();"
    "if(txt){const cls=txt.toLowerCase().includes('atras')?'tk-due-sub':'tk-due-sub tk-due-ok';"
    "return `<div class=\"tk-due-main\">${main||'—'}</div><div class=\"${cls}\">${esc(txt)}</div>`}"
    "const dd=_taskDaysToDue(r);if(dd!=null&&dd<0){return `<div class=\"tk-due-main\">${main||'—'}</div><div class=\"tk-due-sub\">Atraso ${Math.abs(dd)}d</div>`}"
    "return `<div class=\"tk-due-main\">${main||'—'}</div>`}"
)

_TASK_DUE_CELL_NEW = (
    "function _taskIsDone(r){const st=String(r?.Estado||'').trim().toLowerCase();return st==='concluído'||st==='concluido'}"
    "function _taskDueCell(r){const main=_taskDate(r?.Prazo);if(_taskIsDone(r))return `<div class=\"tk-due-main\">${main||'—'}</div>`;"
    "const badge=r?.due_badge||{};const txt=String(badge.text||'').trim();"
    "if(txt&&!txt.toLowerCase().includes('atras')){const cls='tk-due-sub tk-due-ok';"
    "return `<div class=\"tk-due-main\">${main||'—'}</div><div class=\"${cls}\">${esc(txt)}</div>`}"
    "if(txt&&txt.toLowerCase().includes('atras')){return `<div class=\"tk-due-main\">${main||'—'}</div><div class=\"tk-due-sub\">${esc(txt)}</div>`}"
    "const dd=_taskDaysToDue(r);if(dd!=null&&dd<0){return `<div class=\"tk-due-main\">${main||'—'}</div><div class=\"tk-due-sub\">Atraso ${Math.abs(dd)}d</div>`}"
    "return `<div class=\"tk-due-main\">${main||'—'}</div>`}"
)

_TASK_ROW_ACCENT_OLD = (
    "function _taskRowAccent(r){if((r?.blocked_count||0)>0)return 'tk-row-blocked';"
    "if(r?.is_overdue)return 'tk-row-overdue';"
    "const st=String(r?.Estado||'').trim().toLowerCase();"
    "if(st==='concluído'||st==='concluido')return 'tk-row-done';"
    "if(st.includes('progress')||st.includes('curso'))return 'tk-row-progress';return ''}"
)

_TASK_ROW_ACCENT_NEW = (
    "function _taskRowAccent(r){const st=String(r?.Estado||'').trim().toLowerCase();"
    "if(st==='concluído'||st==='concluido')return 'tk-row-done';"
    "if((r?.blocked_count||0)>0)return 'tk-row-blocked';"
    "if(r?.is_overdue)return 'tk-row-overdue';"
    "if(st.includes('progress')||st.includes('curso'))return 'tk-row-progress';return ''}"
)

_TASK_DAYS_DUE_OLD = (
    "function _taskDaysToDue(r){const d=String(r?.Prazo||'').slice(0,10);if(!d)return null;"
)

_TASK_DAYS_DUE_NEW = (
    "function _taskDaysToDue(r){if(typeof _taskIsDone==='function'&&_taskIsDone(r))return null;"
    "const d=String(r?.Prazo||'').slice(0,10);if(!d)return null;"
)

_ROW_OVERDUE_OLD = "if(r.is_overdue)tr.classList.add('row-overdue');"

_ROW_OVERDUE_NEW = "if(r.is_overdue&&!(typeof _taskIsDone==='function'?_taskIsDone(r):false))tr.classList.add('row-overdue');"

_TASKS_KPIS_V3_OLD = (
    '<div class="kpi kpi-click" onclick="tkFilterKpi(\'blocked\')" title="Filtrar bloqueadas">'
    '<div class="ico">🚫</div><div><div class="muted">Bloqueadas</div>'
    '<div class="v" id="tk_blocked">0</div><div class="muted db-sub">Com bloqueios</div></div></div>'
    "</section>"
)

_TASKS_KPIS_V3_NEW = (
    '<div class="kpi kpi-click" onclick="tkFilterKpi(\'blocked\')" title="Filtrar bloqueadas">'
    '<div class="ico">🚫</div><div><div class="muted">Bloqueadas</div>'
    '<div class="v" id="tk_blocked">0</div><span class="tk-trend" id="tk_tr_blocked"></span>'
    '<div class="muted db-sub">Com bloqueios</div></div></div>'
    '<div class="kpi kpi-click" onclick="tkOpenAchImpact()" title="Conquistas das tarefas filtradas">'
    '<div class="ico">📈</div><div><div class="muted">Impacto €</div>'
    '<div class="v" id="tk_impact">€0</div><div class="muted db-sub" id="tk_impact_sub">Conquistas ligadas</div></div></div>'
    "</section>"
)

_APP_TOP_OLD = (
    '<div class="top"><b>App Engenharia Electronics — Web UI Local</b><span id="ver" class="muted"></span></div>'
)

_APP_TOP_NEW = (
    '<div class="top app-top"><b>App Engenharia Electronics — Web UI Local</b>'
    '<div class="app-top-tools">'
    '<input id="global_q" class="app-top-search" placeholder="Pesquisa rápida (Enter)..." '
    'onkeydown="if(event.key===\'Enter\')globalQuickSearch()">'
    '<button type="button" class="btn" onclick="globalQuickSearch()" title="Pesquisar">🔎</button>'
    '<button type="button" class="btn" id="btn_theme" onclick="toggleAppTheme()" title="Alternar tema">🌓</button>'
    "</div><span id=\"ver\" class=\"muted\"></span></div>"
)

_TASKS_V3_CSS = (
    "#page-tasks .tk-kpis{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px}"
    "#page-tasks .tk-trend{display:inline-block;margin-left:6px;font-size:11px;font-weight:700;vertical-align:middle}"
    "#page-tasks .tk-trend.up{color:#16a34a}#page-tasks .tk-trend.down{color:#dc2626}#page-tasks .tk-trend.flat{color:#94a3b8}"
    ".app-top{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:10px 16px;border-bottom:1px solid #e5e7eb;background:#fff}"
    ".app-top-tools{display:flex;align-items:center;gap:8px;margin-left:auto;flex:1;justify-content:flex-end;max-width:520px}"
    ".app-top-search{flex:1;min-width:160px;padding:8px 10px;border-radius:8px;border:1px solid #d7dde8;background:#fff}"
    "body.theme-dark{background:#0f172a;color:#e2e8f0}"
    "body.theme-dark .main{background:#0f172a}"
    "body.theme-dark .top,body.theme-dark .card,body.theme-dark .content{background:#1e293b;color:#e2e8f0;border-color:#334155}"
    "body.theme-dark .app-top-search,body.theme-dark select,body.theme-dark input{background:#0f172a;color:#e2e8f0;border-color:#475569}"
    "@media(max-width:1400px){#page-tasks .tk-kpis{grid-template-columns:repeat(3,minmax(0,1fr))}}"
    "@media(max-width:760px){#page-tasks .tk-kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.app-top-tools{max-width:none;width:100%;margin-left:0}}"
)

_TASKS_V3_JS = (
    "function tkOpenAchImpact(){if(typeof dbOpenAchievements==='function')dbOpenAchievements()}"
    "function tasksEnsureTrendSlots(){"
    "[['tk_total','tk_tr_total'],['tk_open','tk_tr_open'],['tk_done','tk_tr_done'],"
    "['tk_overdue','tk_tr_overdue'],['tk_blocked','tk_tr_blocked']].forEach(([vid,tid])=>{"
    "const v=$(vid),t=$(tid);if(!v||t)return;const s=document.createElement('span');"
    "s.className='tk-trend';s.id=tid;v.insertAdjacentElement('afterend',s)})}"
    "function tasksTrendHtml(cur,prev){const d=Number(cur||0)-Number(prev||0);"
    "if(!Number.isFinite(d)||prev==null||prev===undefined)return '';"
    "if(!d)return '<span class=\"tk-trend flat\">→</span>';"
    "const cls=d>0?'up':'down';return `<span class=\"tk-trend ${cls}\">${d>0?'+':''}${d}</span>`}"
    "function tasksUpdateTrends(k){tasksEnsureTrendSlots();let prev=null;"
    "try{prev=JSON.parse(localStorage.getItem('tasksKpiPrev')||'null')}catch(_){}"
    "const p=prev&&prev.k?prev.k:null;"
    "const set=(id,v)=>{const el=$(id);if(el)el.innerHTML=v};"
    "set('tk_tr_total',tasksTrendHtml(k.total,p?.total));set('tk_tr_open',tasksTrendHtml(k.open,p?.open));"
    "set('tk_tr_done',tasksTrendHtml(k.done,p?.done));set('tk_tr_overdue',tasksTrendHtml(k.overdue,p?.overdue));"
    "set('tk_tr_blocked',tasksTrendHtml(k.blocked,p?.blocked));"
    "try{localStorage.setItem('tasksKpiPrev',JSON.stringify({ts:Date.now(),k}))}catch(_){}}"
    "async function tasksLoadExtras(){try{"
    "const p=tqs();if($('tf_mine')?.checked)p.delete('only_mine');"
    "let j=await api('/api/tasks/extras?'+p);"
    "if($('tk_impact'))$('tk_impact').textContent=money(j.impact_eur||0);"
    "if($('tk_impact_sub'))$('tk_impact_sub').textContent=String(j.impact_label||'Conquistas ligadas')}catch(_){}}"
    "function globalQuickSearch(){const q=String($('global_q')?.value||'').trim();"
    "if(page==='tasks'){if($('tf_q'))$('tf_q').value=q;loadTasks();return}"
    "showPage('tasks');ensureTaskLookups().then(()=>{if($('tf_q'))$('tf_q').value=q;loadTasks()})}"
    "function toggleAppTheme(){const b=document.body;const dark=b.classList.toggle('theme-dark');"
    "try{localStorage.setItem('uiTheme',dark?'dark':'light')}catch(_){}}"
    "function initAppTheme(){try{const t=localStorage.getItem('uiTheme');"
    "if(t==='dark')document.body.classList.add('theme-dark')}catch(_){}}"
)

_LOAD_TASKS_EXTRAS_OLD = "tasksBindFilterChips();tasksLoadPagePrefs();tasksUpdateFooter();"

_LOAD_TASKS_EXTRAS_NEW = (
    "tasksBindFilterChips();tasksLoadPagePrefs();tasksUpdateFooter();tasksUpdateTrends(k);tasksLoadExtras();"
)


_TASKS_ENSURE_TRENDS_OLD = (
    "const v=$(vid),t=$(tid);if(!v||t)return;if(!t.parentElement){const s=document.createElement('span');"
    "s.className='tk-trend';s.id=tid;v.insertAdjacentElement('afterend',s)}})}"
)

_TASKS_ENSURE_TRENDS_NEW = (
    "const v=$(vid),t=$(tid);if(!v||t)return;const s=document.createElement('span');"
    "s.className='tk-trend';s.id=tid;v.insertAdjacentElement('afterend',s)})}"
)


def _patch_html_tasks_visual_v3(html: str) -> str:
    if _TASKS_ENSURE_TRENDS_OLD in html:
        html = html.replace(_TASKS_ENSURE_TRENDS_OLD, _TASKS_ENSURE_TRENDS_NEW, 1)
    if _TASK_DUE_CELL_OLD in html and _TASK_DUE_CELL_NEW not in html:
        html = html.replace(_TASK_DUE_CELL_OLD, _TASK_DUE_CELL_NEW, 1)
    if _TASK_ROW_ACCENT_OLD in html:
        html = html.replace(_TASK_ROW_ACCENT_OLD, _TASK_ROW_ACCENT_NEW, 1)
    if _TASK_DAYS_DUE_OLD in html:
        html = html.replace(_TASK_DAYS_DUE_OLD, _TASK_DAYS_DUE_NEW, 1)
    if _ROW_OVERDUE_OLD in html:
        html = html.replace(_ROW_OVERDUE_OLD, _ROW_OVERDUE_NEW, 1)
    if 'id="tk_impact"' not in html and _TASKS_KPIS_V3_OLD in html:
        html = html.replace(_TASKS_KPIS_V3_OLD, _TASKS_KPIS_V3_NEW, 1)
    if _APP_TOP_OLD in html and "app-top-tools" not in html:
        html = html.replace(_APP_TOP_OLD, _APP_TOP_NEW, 1)
    if "#page-tasks .tk-trend" not in html:
        html = html.replace("</style>", _TASKS_V3_CSS + "</style>", 1)
    if "function tasksLoadExtras" not in html:
        marker = "function tasksToggleAdvanced()"
        if marker in html:
            html = html.replace(marker, _TASKS_V3_JS + marker, 1)
    if _LOAD_TASKS_EXTRAS_OLD in html:
        html = html.replace(_LOAD_TASKS_EXTRAS_OLD, _LOAD_TASKS_EXTRAS_NEW, 1)
    if "initAppTheme();" not in html and "bindNav();" in html:
        html = html.replace("bindNav();", "bindNav();initAppTheme();", 1)
    html = html.replace(
        '<label class="tk-chip"><input type="checkbox" id="tf_show_done"> Ver concluídas</label>',
        '<label class="tk-chip"><input type="checkbox" id="tf_show_done"> Concluídas</label>',
    )
    html = html.replace(
        '<label style="padding-top:30px"><input type="checkbox" id="tf_show_done" onchange="loadTasks()"> Ver concluídas</label>',
        '<label style="padding-top:30px"><input type="checkbox" id="tf_show_done" onchange="loadTasks()"> Concluídas</label>',
    )
    return html


def _patch_html_dashboard(html: str) -> str:
    html = re.sub(
        r'<section class="kpis">.*?</section><section class="card filters" style="padding:16px;margin-top:14px">.*?</section><div id="db_charts" class="db-charts"></div>',
        '<section class="kpis db-kpis">'
        '<div class="kpi kpi-click" onclick="dbOpenTasksWith({})"><div class="ico">📋</div><div><div class="muted">Tarefas</div><div class="v" id="db_tk_total">0</div><div class="muted db-sub">Total segundo filtros</div></div></div>'
        '<div class="kpi kpi-click" onclick="dbOpenTasksWith({open_only:true})"><div class="ico">📝</div><div><div class="muted">Abertas</div><div class="v" id="db_tk_open">0</div><div class="muted db-sub">Não concluídas</div></div></div>'
        '<div class="kpi kpi-click" onclick="dbOpenTasksWith({overdue:true})"><div class="ico">⏰</div><div><div class="muted">Atrasadas</div><div class="v" id="db_tk_overdue">0</div><div class="muted db-sub">Prazo vencido</div></div></div>'
        '<div class="kpi kpi-click" onclick="dbOpenAchievements()"><div class="ico">🏆</div><div><div class="muted">Conquistas</div><div class="v" id="db_ach_total">0</div><div class="muted db-sub">Registos filtrados</div></div></div>'
        '<div class="kpi kpi-click" onclick="dbOpenAchievementImpact()"><div class="ico">📈</div><div><div class="muted">Impacto €</div><div class="v" id="db_ach_impact">€0</div><div class="muted db-sub" id="db_ach_impact_sub">Segundo filtros ativos</div></div></div>'
        "</section>"
        '<section class="card filters db-filters" style="padding:12px;margin-top:12px">'
        '<div class="grid db-grid">'
        '<div class="field"><label>Modo</label><select id="db_mode" onchange="loadDashboard()"><option value="executivo">Executivo</option><option value="operacao">Operação</option><option value="analitico">Analítico</option><option value="eficiencia">Eficiência</option></select></div>'
        '<div class="field"><label>Estado</label><select id="db_estado_f" onchange="loadDashboard()"><option>Todos</option></select></div>'
        '<div class="field"><label>Prioridade</label><select id="db_prio_f" onchange="loadDashboard()"><option>Todos</option></select></div>'
        '<div class="field"><label>Responsável</label><select id="db_resp_f" onchange="loadDashboard()"><option>Todos</option></select></div>'
        '<div class="field"><label>Projeto</label><select id="db_proj_f" onchange="loadDashboard()"><option>Todos</option></select></div>'
        '<div class="field db-open"><label><input type="checkbox" id="db_only_open" onchange="loadDashboard()"> Só abertas</label></div>'
        '<div class="field db-apply"><button class="btn" onclick="loadDashboard()">Atualizar</button></div>'
        "</div></section>"
        '<div id="db_charts" class="db-charts"></div>',
        html,
        count=1,
        flags=re.S,
    )
    html = re.sub(
        r"function loadDashboard\(\)\{[\s\S]*?function renderDbCharts\(data\)\{[\s\S]*?\}\n(?=let _boardDragTid=null;)",
        "let _dbPrefsLoaded=false;"
        "function _dbTrim(s,n=36){const v=String(s||'');return v.length>n?(v.slice(0,n-1)+'…'):v}"
        "function _dbChartKey(c){const t=String(c?.title||'').trim().toLowerCase();const ty=String(c?.type||'').trim().toLowerCase();"
        "const l=(Array.isArray(c?.labels)?c.labels:Array.isArray(c?.y)?c.y:Array.isArray(c?.x)?c.x:[]).slice(0,6).map(x=>String(x||'')).join('|').toLowerCase();"
        "return ty+'|'+t+'|'+l}"
        "function dbFilterSummary(){const pairs=[['Modo','db_mode'],['Estado','db_estado_f'],['Prioridade','db_prio_f'],['Responsável','db_resp_f'],['Projeto','db_proj_f']];"
        "const out=[];pairs.forEach(([k,id])=>{const v=$(id)?.value;if(v&&v!=='Todos')out.push(k+': '+v)});if($('db_only_open')?.checked)out.push('Só abertas');return out.join(' · ')||'Sem filtros adicionais'}"
        "async function loadDashboardPrefs(){if(_dbPrefsLoaded)return;try{let j=await api('/api/dashboard/prefs');const p=j?.prefs||{};"
        "if($('db_mode')&&p.mode)$('db_mode').value=p.mode;if($('db_estado_f')&&p.estado)$('db_estado_f').value=p.estado;if($('db_prio_f')&&p.prioridade)$('db_prio_f').value=p.prioridade;"
        "if($('db_resp_f')&&p.responsavel)$('db_resp_f').value=p.responsavel;if($('db_proj_f')&&p.projeto)$('db_proj_f').value=p.projeto;if($('db_only_open'))$('db_only_open').checked=!!p.only_open}catch(_){ }_dbPrefsLoaded=true}"
        "async function saveDashboardPrefs(){try{await api('/api/dashboard/prefs',{method:'POST',body:JSON.stringify({mode:$('db_mode')?.value||'executivo',estado:$('db_estado_f')?.value||'Todos',prioridade:$('db_prio_f')?.value||'Todos',responsavel:$('db_resp_f')?.value||'Todos',projeto:$('db_proj_f')?.value||'Todos',only_open:!!$('db_only_open')?.checked})})}catch(_){}}"
        "async function loadDashboard(){try{await ensureTaskLookups();populateDbFilters();await loadDashboardPrefs();"
        "let j=await api('/api/dashboard/summary'+dbQs());const tk=j.tasks||{},ak=j.achievements||{};$('db_tk_total').textContent=tk.total??0;$('db_tk_open').textContent=tk.open??0;"
        "$('db_tk_overdue').textContent=tk.overdue??0;$('db_ach_total').textContent=ak.total??0;$('db_ach_impact').textContent=money(ak.impact_total||0);"
        "if($('db_ach_impact_sub'))$('db_ach_impact_sub').textContent=String(ak.scope_label||'Segundo filtros ativos');"
        "let ch=await api('/api/dashboard/charts'+dbQs());renderDbCharts(ch);saveDashboardPrefs();"
        "if(page==='dashboard')$('upd').textContent='Última atualização: '+new Date().toLocaleTimeString('pt-PT')}catch(e){toast(e.message,true)}}"
        "function populateDbFilters(){const tl=_taskLookups||{};const prep=(a,d)=>[d,...new Set((a||[]).filter(x=>x&&x!==d))];if($('db_estado_f')&&!$('db_estado_f').dataset.filled){fill('db_estado_f',prep(tl.estados,'Todos'));fill('db_prio_f',['Todos','Alta','Média','Baixa']);fill('db_resp_f',prep(tl.users,'Todos'));fill('db_proj_f',prep(tl.projects,'Todos'));$('db_estado_f').dataset.filled='1'}}"
        "function dbQs(){const q=new URLSearchParams();q.set('mode',$('db_mode')?.value||'executivo');[['estado','db_estado_f','Todos'],['prioridade','db_prio_f','Todos'],['responsavel','db_resp_f','Todos'],['projeto','db_proj_f','Todos']].forEach(([k,id,d])=>{const v=$(id)?.value;if(v&&v!==d)q.set(k,v)});if($('db_only_open')?.checked)q.set('only_open','1');return '?'+q.toString()}"
        "function dbApplyTaskFilters(extra={}){if($('tf_q'))$('tf_q').value='';if($('tf_estado'))$('tf_estado').value=extra.estado||'Todos';if($('tf_prio'))$('tf_prio').value=extra.prioridade||'Todos';if($('tf_resp'))$('tf_resp').value=extra.responsavel||'Todos';if($('tf_projeto'))$('tf_projeto').value=extra.projeto||'Todos';"
        "if($('tf_overdue'))$('tf_overdue').checked=!!extra.overdue;if($('tf_blocked'))$('tf_blocked').checked=!!extra.blocked;"
        "if($('tf_show_done'))$('tf_show_done').checked=!!extra.show_done;"
        "if(extra.open_only&&$('tf_show_done'))$('tf_show_done').checked=false;"
        "if(extra.open_only)window._dash_open_only=false}"
        "function dbOpenTasksWith(extra={}){showPage('tasks');ensureTaskLookups().then(()=>{dbApplyTaskFilters(extra||{});loadTasks()})}"
        "function dbOpenAchievements(){showPage('ach');setTimeout(()=>{if(typeof loadAll==='function')loadAll()},0)}"
        "function dbOpenAchievementImpact(){dbOpenAchievements()}"
        "function dbOnChartClick(c,p){try{if(!c||!p)return;const pt=(p.points||[])[0];if(!pt)return;const t=String(c.title||'').toLowerCase();"
        "if(c.type==='pie'&&t.includes('estado')){const estado=String(pt.label||'').trim();if(estado)dbOpenTasksWith({estado});return}"
        "if(c.type==='bar_h'&&t.includes('atras')){const tid=(Array.isArray(c.task_ids)?c.task_ids[pt.pointIndex]:null)||String(pt.y||'').split('—')[0].trim();if(tid&&tid.startsWith('Task')){showPage('tasks');setTimeout(()=>openTaskDetail(tid),0)}return}"
        "}catch(_){}}"
        "function ensurePlotly(){return new Promise(res=>{if(window.Plotly)return res();const s=document.createElement('script');s.src='https://cdn.plot.ly/plotly-2.27.0.min.js';s.onload=res;document.head.appendChild(s)})}"
        "function ensureHtml2Canvas(){return new Promise((res,rej)=>{if(window.html2canvas)return res();const s=document.createElement('script');s.src='https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js';s.onload=res;s.onerror=rej;document.head.appendChild(s)})}"
        "async function exportDbCharts(){try{const root=$('page-dashboard');if(!root){toast('Dashboard indisponível',true);return}await ensureHtml2Canvas();const c=await html2canvas(root,{backgroundColor:'#f3f4f6',scale:1.5,useCORS:true});const a=document.createElement('a');const ts=new Date();const pad=n=>String(n).padStart(2,'0');const fn='dashboard_'+ts.getFullYear()+pad(ts.getMonth()+1)+pad(ts.getDate())+'_'+pad(ts.getHours())+pad(ts.getMinutes())+pad(ts.getSeconds())+'.png';a.href=c.toDataURL('image/png');a.download=fn;a.click();toast('PNG exportado: '+fn)}catch(e){try{await ensurePlotly();const els=[...document.querySelectorAll('#db_charts .db-chart')];if(!els.length){toast('Sem gráficos para exportar',true);return}for(let i=0;i<els.length;i++){const nm='dashboard_'+(i+1);await Plotly.downloadImage(els[i],{format:'png',width:1100,height:560,filename:nm})}toast('Exportados '+els.length+' PNG')}catch(ex){toast('Falha ao exportar PNG: '+(ex?.message||e?.message||'erro'),true)}}}"
        "function renderDbCharts(data){const box=$('db_charts');if(!box)return;box.innerHTML='';ensurePlotly().then(()=>{const seen=new Set();const charts=(data?.charts||[]).filter(c=>{const k=_dbChartKey(c);if(seen.has(k))return false;seen.add(k);return true});"
        "charts.forEach((c,i)=>{const el=document.createElement('div');el.className='card db-chart';el.innerHTML=`<h3 style=\"margin:0 0 8px;font-size:14px\">${esc(c.title)}</h3><div class=\"muted db-sub\" style=\"margin:-4px 0 8px\">${esc(dbFilterSummary())}</div><div id=\"dbc_plot_${i}\" style=\"height:280px\"></div>`;box.appendChild(el);"
        "const plotEl=el.querySelector('#dbc_plot_'+i);let trace,layout={margin:{t:10,r:16,b:44,l:58},paper_bgcolor:'#fff',plot_bgcolor:'#fafafa'};"
        "if(c.type==='pie'){trace=[{type:'pie',labels:c.labels,values:c.values,hole:.45}]}"
        "else if(c.type==='heatmap'){trace=[{type:'heatmap',x:c.x,y:c.y,z:c.z,colorscale:'Blues'}]}"
        "else if(c.type==='bar_h'){const y=(c.y||[]).map(v=>_dbTrim(v,40));trace=[{type:'bar',orientation:'h',x:c.x,y:y,customdata:c.task_ids||[],marker:{color:'#dc2626'},hovertemplate:'%{y}<br>Valor: %{x}<extra></extra>'}];layout.margin.l=Math.max(210,(y||[]).reduce((m,s)=>Math.max(m,String(s).length*6),120))}"
        "else{trace=[{type:'bar',x:c.x,y:c.y,marker:{color:'#0869d8'}}]}"
        "Plotly.newPlot(plotEl,trace,layout,{responsive:true,displayModeBar:false}).then(()=>{plotEl.style.cursor='pointer';plotEl.on('plotly_click',p=>dbOnChartClick(c,p))})})"
        "if(!charts.length)box.innerHTML='<div class=\"card\"><p class=\"muted\">Sem gráficos para os filtros atuais.</p></div>'}).catch(()=>{box.innerHTML='<p class=\"muted\">Gráficos indisponíveis</p>'})}\n",
        html,
        count=1,
    )
    return html


_BOARD_FILTERS_OLD = (
    '<section class="card filters"><div class="field"><label>Estado</label><select id="bf_estado" onchange="loadBoard()"><option>Todos</option></select></div>'
    '<div class="field"><label>Prioridade</label><select id="bf_prio" onchange="loadBoard()"><option>Todas</option></select></div>'
    '<div class="field"><label>Responsável</label><select id="bf_resp" onchange="loadBoard()"><option>Todos</option></select></div>'
    '<div class="field"><label>Projeto</label><select id="bf_proj" onchange="loadBoard()"><option>Todos</option></select></div>'
    '<div class="field span2"><label>Pesquisar</label><input id="bf_q" placeholder="ID, título, descrição..." '
    'oninput="clearTimeout(window.bfq);window.bfq=setTimeout(loadBoard,250)"></div></section>'
)

_BOARD_FILTERS_NEW = (
    '<section class="card filters board-filters" style="padding:12px">'
    '<div class="grid board-grid">'
    '<div class="field"><label>Estado</label><select id="bf_estado"><option>Todos</option></select></div>'
    '<div class="field"><label>Prioridade</label><select id="bf_prio"><option>Todas</option></select></div>'
    '<div class="field"><label>Responsável</label><select id="bf_resp"><option>Todos</option></select></div>'
    '<div class="field"><label>Projeto</label><select id="bf_proj"><option>Todos</option></select></div>'
    '<div class="field span2"><label>Pesquisar</label><input id="bf_q" placeholder="ID, título, descrição..."></div>'
    '<div class="field board-quick"><label>Filtros rápidos</label>'
    '<div class="board-chips">'
    '<label class="board-chip"><input type="checkbox" id="bf_mine"> Minhas</label>'
    '<label class="board-chip"><input type="checkbox" id="bf_overdue"> Atrasadas</label>'
    '<label class="board-chip"><input type="checkbox" id="bf_blocked"> Bloqueadas</label>'
    '<label class="board-chip"><input type="checkbox" id="bf_show_done"> Ver concluídas</label>'
    "</div></div>"
    '<div class="field board-apply"><button class="btn" type="button" onclick="boardClearFilters()">Limpar</button></div>'
    "</div></section>"
)

_BOARD_CSS = (
    "#page-board .board-grid{display:grid;grid-template-columns:repeat(6,minmax(120px,1fr));gap:10px;align-items:end}"
    "#page-board .board-grid .field{margin:0;min-width:0}"
    "#page-board .board-grid .span2{grid-column:span 2}"
    "#page-board .board-quick{grid-column:1/-1}"
    "#page-board .board-chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:4px}"
    "#page-board .board-chip{display:inline-flex;align-items:center;gap:6px;padding:6px 10px;border-radius:999px;"
    "background:#f1f5f9;border:1px solid #dbe3ee;font-size:12px;cursor:pointer;user-select:none}"
    "#page-board .board-chip input{margin:0}"
    "#page-board .board-apply{display:flex;justify-content:flex-end;align-items:end}"
    "#page-board .k-card{border:1px solid #dbe3ee;border-radius:10px;padding:10px;background:#fff;margin-bottom:8px;"
    "box-shadow:0 1px 2px rgba(15,23,42,.05);transition:box-shadow .12s ease,transform .08s ease}"
    "#page-board .k-card:hover{box-shadow:0 4px 14px rgba(8,105,216,.10)}"
    "#page-board .k-card-overdue{border-color:#fecaca;background:#fff7f7}"
    "#page-board .k-card-blocked{border-color:#fca5a5;background:#fff5f5}"
    "#page-board .k-badges{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}"
    "#page-board .k-badge{display:inline-flex;align-items:center;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600}"
    "#page-board .k-badge-block{background:#fee2e2;color:#991b1b}"
    "#page-board .k-due{display:inline-flex;align-items:center;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;margin-top:6px}"
    "@media(max-width:1200px){#page-board .board-grid{grid-template-columns:repeat(3,minmax(120px,1fr))}"
    "#page-board .board-grid .span2{grid-column:span 2}}"
    "@media(max-width:760px){#page-board .board-grid{grid-template-columns:repeat(2,minmax(110px,1fr))}}"
)

_BOARD_JS = (
    "let _boardPrefsLoaded=false,_boardFiltersSaveTimer=null;"
    "function boardReadFilters(){return{"
    "estado:$('bf_estado')?.value||'Todos',prioridade:$('bf_prio')?.value||'Todas',"
    "responsavel:$('bf_resp')?.value||'Todos',projeto:$('bf_proj')?.value||'Todos',"
    "q:($('bf_q')?.value||'').trim(),only_mine:!!$('bf_mine')?.checked,"
    "overdue_only:!!$('bf_overdue')?.checked,blocked_only:!!$('bf_blocked')?.checked,"
    "show_done:!!$('bf_show_done')?.checked};}"
    "function boardApplyFiltersObj(o){if(!o||typeof o!=='object')return;"
    "if($('bf_estado'))$('bf_estado').value=o.estado||'Todos';"
    "if($('bf_prio'))$('bf_prio').value=o.prioridade||'Todas';"
    "if($('bf_resp'))$('bf_resp').value=o.responsavel||'Todos';"
    "if($('bf_proj'))$('bf_proj').value=o.projeto||'Todos';"
    "if($('bf_q'))$('bf_q').value=o.q||'';"
    "if($('bf_mine'))$('bf_mine').checked=!!o.only_mine;"
    "if($('bf_overdue'))$('bf_overdue').checked=!!o.overdue_only;"
    "if($('bf_blocked'))$('bf_blocked').checked=!!o.blocked_only;"
    "if($('bf_show_done'))$('bf_show_done').checked=!!o.show_done;}"
    "function boardSavePrefs(){try{clearTimeout(_boardFiltersSaveTimer);"
    "_boardFiltersSaveTimer=setTimeout(async()=>{try{await api('/api/board/prefs',{method:'POST',body:JSON.stringify(boardReadFilters())})}catch(_){}},280)}catch(_){}}"
    "function boardSavePrefsSilent(){try{clearTimeout(_boardFiltersSaveTimer);"
    "_boardFiltersSaveTimer=setTimeout(async()=>{try{await api('/api/board/prefs',{method:'POST',body:JSON.stringify(boardReadFilters())})}catch(_){}},80)}catch(_){}}"
    "async function loadBoardPrefs(){if(_boardPrefsLoaded)return;try{"
    "let j=await api('/api/board/prefs');let o=(j&&j.prefs)||{};"
    "if(!o||!Object.keys(o).length){try{const raw=localStorage.getItem('boardFilters');if(raw)o=JSON.parse(raw)||{}}catch(_){}}"
    "boardApplyFiltersObj(o);if(o&&Object.keys(o).length)boardSavePrefsSilent();"
    "}catch(_){}_boardPrefsLoaded=true;}"
    "function boardClearFilters(){boardApplyFiltersObj({estado:'Todos',prioridade:'Todas',responsavel:'Todos',projeto:'Todos',q:'',only_mine:false,overdue_only:false,blocked_only:false,show_done:false});boardSavePrefs();loadBoard();}"
    "function boardQs(){const q=new URLSearchParams();const f=boardReadFilters();"
    "[['estado',f.estado,'Todos'],['prioridade',f.prioridade,'Todas'],['responsavel',f.responsavel,'Todos'],['projeto',f.projeto,'Todos']].forEach(([k,v,d])=>{if(v&&v!==d)q.set(k,v)});"
    "if(f.q)q.set('q',f.q);if(f.only_mine)q.set('only_mine','1');if(f.overdue_only)q.set('overdue_only','1');"
    "if(f.blocked_only)q.set('blocked_only','1');if(f.show_done)q.set('show_done','1');"
    "const s=q.toString();return s?('?'+s):'';}"
    "function saveBoardFilters(){boardSavePrefs();}"
    "function restoreBoardFilters(){}"
    "async function loadBoard(){try{await ensureTaskLookups();populateBoardFilters();await loadBoardPrefs();"
    "let j=await api('/api/board'+boardQs());renderBoard(j);boardSavePrefs();"
    "if(page==='board')$('upd').textContent='Última atualização: '+new Date().toLocaleTimeString('pt-PT')}catch(e){toast(e.message,true)}}"
    "function populateBoardFilters(){const tl=_taskLookups||{};const prep=(a,d)=>[d,...new Set((a||[]).filter(x=>x&&x!==d))];"
    "if($('bf_estado')&&!$('bf_estado').dataset.filled){fill('bf_estado',prep(tl.estados,'Todos'));fill('bf_prio',prep(tl.prioridades,'Todas'));"
    "fill('bf_resp',prep(tl.users,'Todos'));fill('bf_proj',prep(tl.projects,'Todos'));$('bf_estado').dataset.filled='1';"
    "['bf_estado','bf_prio','bf_resp','bf_proj','bf_mine','bf_overdue','bf_blocked','bf_show_done'].forEach(id=>{const el=$(id);if(!el||el.dataset.bfBound)return;"
    "el.dataset.bfBound='1';el.addEventListener('change',()=>{boardSavePrefs();loadBoard()})});"
    "const bq=$('bf_q');if(bq&&!bq.dataset.bfBound){bq.dataset.bfBound='1';bq.addEventListener('input',()=>{clearTimeout(window.bfq);window.bfq=setTimeout(()=>{boardSavePrefs();loadBoard()},250)})}}}"
    "function renderBoard(j){const box=$('board_cols');if(!box)return;box.innerHTML='';const canEdit=canEditTasks();"
    "(j.columns||[]).forEach(col=>{const wrap=document.createElement('div');wrap.className='k-col';wrap.style.background=col.color||'#f8fafc';"
    "const cards=(j.cards&&j.cards[col.key])||[];wrap.innerHTML=`<div class=\"k-col-head\"><span>${esc(col.label)}</span><span class=\"badge\">${(j.counts&&j.counts[col.key])||cards.length}</span></div><div class=\"k-col-body\"></div>`;"
    "const body=wrap.querySelector('.k-col-body');if(!cards.length)body.innerHTML='<p class=\"muted\" style=\"font-size:12px;text-align:center;padding:20px 0\">Sem tarefas</p>';"
    "else cards.forEach(r=>{const card=document.createElement('div');const blocked=!!(r.is_blocked||(r.blocked_count||0)>0);"
    "card.className='k-card'+(r.is_overdue?' k-card-overdue':'')+(blocked?' k-card-blocked':'');card.draggable=canEdit;card.dataset.tid=r.TaskID;"
    "const due=r.due_badge||{};const badges=[];"
    "if(due.text)badges.push(`<span class=\"k-badge k-due\" style=\"background:${due.bg};color:${due.fg}\">${esc(due.text)}</span>`);"
    "if(blocked)badges.push(`<span class=\"k-badge k-badge-block\">🚫 Bloqueio${(r.blocked_count||0)>1?' ('+r.blocked_count+')':''}</span>`);"
    "const badgeHtml=badges.length?`<div class=\"k-badges\">${badges.join('')}</div>`:'';"
    "const bits=[r.Responsavel&&`Resp: ${esc(r.Responsavel)}`,r.Prioridade&&`Prio: ${esc(r.Prioridade)}`,r.Projeto&&`Proj: ${esc(r.Projeto)}`].filter(Boolean).join(' | ');"
    "card.innerHTML=`<div><b>${esc(fmtTid(r.TaskID))}</b> — ${esc(r.Tarefa||'')}</div>${bits?`<div class=\"muted\" style=\"font-size:12px;margin-top:4px\">${bits}</div>`:''}${badgeHtml}"
    "${(r.NotifEmoji||r.Notificacoes)?`<div style=\"margin-top:4px\">${esc(r.NotifEmoji||r.Notificacoes)}</div>`:''}"
    "<div style=\"margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;align-items:center\"><button class=\"btn\" style=\"font-size:12px;padding:4px 8px\" type=\"button\">Abrir</button>"
    "<select class=\"k-status-sel\" style=\"font-size:12px;padding:4px 6px;border-radius:6px;border:1px solid #d7dde8\"></select></div>`;"
    "card.querySelector('button').onclick=(e)=>{e.stopPropagation();openTaskDetail(r.TaskID)};const ksel=card.querySelector('.k-status-sel');"
    "if(ksel&&canEdit){(j.columns||[]).forEach(c=>{const o=document.createElement('option');o.value=c.key;o.textContent=c.label;if((r.column||'')===c.key)o.selected=true;ksel.appendChild(o)});"
    "ksel.onclick=e=>e.stopPropagation();ksel.onchange=async(e)=>{e.stopPropagation();const nv=ksel.value;if(nv===(r.column||''))return;"
    "if(typeof validateTaskCanComplete==='function'&&!await validateTaskCanComplete(nv,r.TaskID)){ksel.value=r.column||'';return}"
    "try{await api('/api/board/move',{method:'POST',body:JSON.stringify({task_id:r.TaskID,estado:nv})});toast('Estado: '+nv);loadBoard()}catch(err){toast(err.message,true);ksel.value=r.column||''}}}"
    "if(canEdit){card.addEventListener('dragstart',e=>{_boardDragTid=r.TaskID;card.classList.add('dragging');e.dataTransfer.setData('text/plain',r.TaskID)});"
    "card.addEventListener('dragend',()=>{card.classList.remove('dragging');_boardDragTid=null})}body.appendChild(card)});"
    "if(canEdit){wrap.addEventListener('dragover',e=>{e.preventDefault();wrap.classList.add('drag-over')});wrap.addEventListener('dragleave',()=>wrap.classList.remove('drag-over'));"
    "wrap.addEventListener('drop',async e=>{e.preventDefault();wrap.classList.remove('drag-over');const tid=e.dataTransfer.getData('text/plain')||_boardDragTid;if(!tid)return;"
    "const cur=(cards.find(c=>c.TaskID===tid)||{}).column;if(cur===col.key)return;"
    "if(typeof validateTaskCanComplete==='function'&&!await validateTaskCanComplete(col.key,tid))return;"
    "try{await api('/api/board/move',{method:'POST',body:JSON.stringify({task_id:tid,estado:col.key})});"
    "toast('Estado atualizado');loadBoard()}catch(err){toast(err.message,true)}})}box.appendChild(wrap)})}"
)

_BOARD_JS_OLD = (
    "const BOARD_FILTERS_KEY='boardFilters';const TASK_FILTERS_KEY='taskFilters';"
    "function saveBoardFilters(){try{const o={estado:$('bf_estado')?.value,prioridade:$('bf_prio')?.value,responsavel:$('bf_resp')?.value,projeto:$('bf_proj')?.value,q:$('bf_q')?.value};localStorage.setItem(BOARD_FILTERS_KEY,JSON.stringify(o))}catch(e){}}"
    "function restoreBoardFilters(){try{const raw=localStorage.getItem(BOARD_FILTERS_KEY);if(!raw)return;const o=JSON.parse(raw)||{};if(o.estado&&$('bf_estado'))$('bf_estado').value=o.estado;if(o.prioridade&&$('bf_prio'))$('bf_prio').value=o.prioridade;if(o.responsavel&&$('bf_resp'))$('bf_resp').value=o.responsavel;if(o.projeto&&$('bf_proj'))$('bf_proj').value=o.projeto;if(o.q&&$('bf_q'))$('bf_q').value=o.q}catch(e){}}"
)


_BOARD_JS_OLD_BOARDQS = (
    "function boardQs(){const q=new URLSearchParams();[['estado','bf_estado','Todos'],['prioridade','bf_prio','Todas'],"
    "['responsavel','bf_resp','Todos'],['projeto','bf_proj','Todos']].forEach(([k,id,d])=>{const v=$(id)?.value;if(v&&v!==d)q.set(k,v)});"
    "const t=$('bf_q')?.value?.trim();if(t)q.set('q',t);const s=q.toString();return s?('?'+s):''}"
)


def _patch_html_board(html: str) -> str:
    if _BOARD_FILTERS_OLD not in html:
        raise RuntimeError("Bytecode HTML inesperado — não foi possível aplicar patch board Fase A")
    html = html.replace(_BOARD_FILTERS_OLD, _BOARD_FILTERS_NEW, 1)
    if "#page-board .board-grid{" not in html:
        html = html.replace("</style>", _BOARD_CSS + "</style>", 1)
    if _BOARD_JS_OLD in html:
        html = html.replace(_BOARD_JS_OLD, "", 1)
    if _BOARD_JS_OLD_BOARDQS in html:
        html = html.replace(_BOARD_JS_OLD_BOARDQS, "", 1)
    html = re.sub(
        r"function boardQs\(\)\{const q=new URLSearchParams\(\);\[\['estado','bf_estado','Todos'\],[\s\S]*?return s\?\('\?\+s\):''\}",
        "",
        html,
        count=1,
    )
    html = re.sub(
        r"async function loadBoard\(\)\{[\s\S]*?\}catch\(e\)\{toast\(e\.message,true\)\}\}",
        "",
        html,
        count=1,
    )
    html = re.sub(
        r"function populateBoardFilters\(\)\{[\s\S]*?\}\}\}",
        "",
        html,
        count=1,
    )
    html = re.sub(
        r"function renderBoard\(j\)\{[\s\S]*?box\.appendChild\(wrap\)\}\)\}",
        "",
        html,
        count=1,
    )
    marker = "let _boardDragTid=null;"
    if marker not in html:
        raise RuntimeError("Bytecode HTML inesperado — anchor board JS em falta")
    if "function boardReadFilters()" not in html:
        html = html.replace(marker, marker + _BOARD_JS, 1)
    if "validateTaskCanComplete(nv,r.TaskID)" not in html:
        html = html.replace(
            "const nv=ksel.value;if(nv===(r.column||''))return;try{await api('/api/board/move'",
            "const nv=ksel.value;if(nv===(r.column||''))return;"
            "if(typeof validateTaskCanComplete==='function'&&!await validateTaskCanComplete(nv,r.TaskID)){ksel.value=r.column||'';return}"
            "try{await api('/api/board/move'",
            1,
        )
    if "validateTaskCanComplete(col.key,tid)" not in html:
        html = html.replace(
            "if(cur===col.key)return;try{await api('/api/board/move',{method:'POST',body:JSON.stringify({task_id:tid,estado:col.key})});",
            "if(cur===col.key)return;"
            "if(typeof validateTaskCanComplete==='function'&&!await validateTaskCanComplete(col.key,tid))return;"
            "try{await api('/api/board/move',{method:'POST',body:JSON.stringify({task_id:tid,estado:col.key})});",
            1,
        )
    return html


_TD_NAV = (
    '<nav id="td_nav" class="td-nav">'
    '<button type="button" class="btn" onclick="tdScrollSec(\'td_sec_desc\')">Resumo</button>'
    '<button type="button" class="btn" onclick="tdScrollSec(\'td_sec_class\')">Classificação</button>'
    '<button type="button" class="btn" onclick="tdScrollSec(\'td_sec_actions\')">Ações</button>'
    '<button type="button" class="btn" onclick="tdScrollSec(\'td_sec_plan\')">Planeamento</button>'
    '<button type="button" class="btn" onclick="tdScrollSec(\'td_sec_folder\')">Pasta/Anexos</button>'
    '<button type="button" class="btn" onclick="tdScrollSec(\'td_sec_res\')">Resultados</button>'
    '<button type="button" class="btn" onclick="tdScrollSec(\'td_sec_hist\')">Histórico</button>'
    "</nav>"
)

_TD_NAV_JS = (
    "function tdScrollSec(id){const el=$(id);if(!el)return;el.scrollIntoView({behavior:'smooth',block:'start'})}"
    "function updateDetailActionStats(){const el=$('td_act_stats');if(!el)return;"
    "const c=(typeof detailCounters==='function')?detailCounters():{total:0,actions:0,checks:0,done:0,overdue:0,blocked:0,risk:0};"
    "el.textContent='Total: '+c.total+' · Ações: '+c.actions+' · Checks: '+c.checks+' · Concluídas: '+c.done+' · Atrasadas: '+c.overdue+' · Bloqueadas: '+c.blocked+' · Em risco: '+c.risk}"
)

_DIAG_HTML = (
    '<section class="card" style="padding:16px;margin-bottom:14px">'
    '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">'
    '<h3 style="margin:0">Diagnóstico</h3>'
    '<button class="btn" type="button" onclick="loadDiagnostics()">Executar diagnóstico</button>'
    "</div>"
    '<div id="sys_diag" class="muted" style="margin-top:12px;font-size:13px">Clique em «Executar diagnóstico».</div>'
    "</section>"
)

_DIAG_JS = (
    "async function loadDiagnostics(){try{const box=$('sys_diag');if(box)box.textContent='A verificar...';"
    "let j=await api('/api/system/diagnostics');if(box){"
    "box.innerHTML=(j.checks||[]).map(c=>{"
    "const cls=c.ok?'ok':'err';"
    "return '<div class=\"diag-row '+cls+'\"><span><b>'+esc(c.name)+'</b></span>"
    "<span style=\"text-align:right;max-width:65%\">'+esc(c.detail||'')+'</span></div>'}).join('')||'Sem checks';}"
    "if(j.errors&&j.errors.length&&$('sys_log'))$('sys_log').textContent=j.errors.join('\\n');"
    "if(!j.ok)toast('Diagnóstico: existem problemas — ver secção abaixo',true)"
    "}catch(e){toast(e.message,true);if($('sys_diag'))$('sys_diag').textContent=e.message}}"
)

_SYSTEM_LISTS_HTML = (
    '<section class="card" style="padding:16px;margin-bottom:14px">'
    '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">'
    '<h3 style="margin:0">Listas da aplicação</h3>'
    '<div style="display:flex;gap:8px;flex-wrap:wrap">'
    '<button class="btn" type="button" onclick="loadSystemLists()">Atualizar listas</button>'
    '<button class="btn primary" id="sys_lists_save_btn" type="button" onclick="saveSystemLists()">Guardar lista</button>'
    "</div></div>"
    '<div class="grid" style="margin-top:10px">'
    '<div class="field"><label>Tipo de lista</label><select id="sys_list_type" onchange="sysListRenderCurrent()"></select></div>'
    '<div class="field"><label>Permissões</label><div id="sys_lists_perm" class="muted">A verificar...</div></div>'
    '<div class="field" style="grid-column:1/-1"><label>Valores (1 por linha)</label><textarea id="sys_list_values" style="min-height:180px"></textarea></div>'
    "</div></section>"
)

_SYSTEM_LISTS_JS = (
    "const _sysListTypes=['estados','prioridades','projects','lines','machines','milestones','assuntos','pessoal'];"
    "let _sysListsCache={};let _sysListsCanEdit=false;"
    "function _sysCanEdit(){try{if(typeof canEditTasks==='function')return !!canEditTasks()}catch(_){ }"
    "const r=String(user?.role||'').trim().toLowerCase();return r==='edit'||r==='admin'}"
    "function sysFillListTypes(){const s=$('sys_list_type');if(!s)return;if(s.dataset.ready==='1')return;"
    "s.innerHTML=_sysListTypes.map(t=>'<option value=\"'+esc(t)+'\">'+esc(t)+'</option>').join('');s.dataset.ready='1'}"
    "function sysListRenderCurrent(){const t=String($('sys_list_type')?.value||'estados');"
    "if($('sys_list_values'))$('sys_list_values').value=((_sysListsCache[t]||[]).map(v=>String(v||'').trim()).filter(v=>v)).join('\\n')}"
    "function _sysListsUiState(){const can=(_sysListsCanEdit&&_sysCanEdit());const ta=$('sys_list_values');if(ta)ta.readOnly=!can;"
    "const b=$('sys_lists_save_btn');if(b)b.style.display=can?'inline-block':'none';"
    "if($('sys_lists_perm'))$('sys_lists_perm').textContent=can?'Pode editar (edit/admin)':'Apenas leitura (read)'}"
    "async function loadSystemLists(){try{sysFillListTypes();let j=await api('/api/system/lists');"
    "_sysListsCache=j.lists||{};_sysListsCanEdit=!!j.can_edit;sysListRenderCurrent();_sysListsUiState()}"
    "catch(e){toast(e.message,true)}}"
    "async function saveSystemLists(){try{if(!_sysCanEdit()||!_sysListsCanEdit){toast('Sem permissões para editar listas',true);return}"
    "sysFillListTypes();const t=String($('sys_list_type')?.value||'estados');"
    "const vals=String($('sys_list_values')?.value||'').split(/\\r?\\n/).map(v=>String(v||'').trim()).filter(v=>v);"
    "let payload={};payload[t]=vals;await api('/api/system/lists',{method:'POST',body:JSON.stringify({lists:payload})});"
    "unsavedClear();toast('Lista guardada: '+t);await loadSystemLists();try{_taskLookupsReady=false;await ensureTaskLookups()}catch(_){}}"
    "catch(e){toast(e.message,true)}}"
)

_ADMIN_HEAVY_HTML = (
    '<section class="card adm2-panel" id="adm2_panel_db" style="padding:16px;margin-bottom:12px;display:none">'
    '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">'
    '<h3 style="margin:0">Administração DB</h3>'
    '<div style="display:flex;gap:8px;flex-wrap:wrap">'
    '<button class="btn" type="button" onclick="loadAdminAll()">Atualizar</button>'
    '<button class="btn" type="button" onclick="adminRunBackup()">Backup lógico</button>'
    '<button class="btn" type="button" onclick="adminRunCleanup()">Limpeza</button>'
    "</div></div>"
    '<div class="field" style="margin-top:10px"><label>Visão geral</label>'
    '<pre id="adm_overview" class="muted" style="max-height:160px;overflow:auto">Sem dados.</pre></div>'
    '<div class="field"><label>Arquivos</label>'
    '<div class="table-wrap"><table><thead><tr><th>ID</th><th>TaskID</th><th>Ação</th><th>User</th><th>TS</th><th>Ações</th></tr></thead>'
    '<tbody id="adm_archives"></tbody></table></div></div>'
    '<div class="field"><label>Sessões (últimas)</label>'
    '<pre id="adm_sessions" class="muted" style="max-height:140px;overflow:auto">Sem dados.</pre></div>'
    '<div class="field"><label>Logs app (tail)</label>'
    '<pre id="adm_logs" class="muted" style="max-height:180px;overflow:auto">Sem dados.</pre></div>'
    "</section>"
)

_ADMIN_HEAVY_JS = (
    "function _admJson(v){try{return JSON.stringify(v,null,2)}catch(_){return String(v||'')}}"
    "async function loadAdminOverview(){try{let j=await api('/api/admin/overview');if($('adm_overview'))$('adm_overview').textContent=_admJson(j)}catch(e){if($('adm_overview'))$('adm_overview').textContent='Erro: '+e.message;toast(e.message,true)}}"
    "async function loadAdminArchives(){try{let j=await api('/api/admin/archives?limit=80');const tb=$('adm_archives');if(!tb)return;tb.innerHTML='';"
    "(j.rows||[]).forEach(r=>{const tr=document.createElement('tr');"
    "tr.innerHTML='<td>'+esc(String(r.id||''))+'</td><td>'+esc(String(r.TaskID||''))+'</td><td>'+esc(String(r.action||''))+'</td>'"
    "+'<td>'+esc(String(r.user||''))+'</td><td>'+esc(String(r.ts||''))+'</td>'"
    "+'<td><button class=\"btn\" onclick=\"adminRestoreArchive('+Number(r.id||0)+')\">Restaurar</button>'"
    "+'<button class=\"btn danger\" onclick=\"adminDeleteArchive('+Number(r.id||0)+')\">Apagar</button></td>';"
    "tb.appendChild(tr)});if(!(j.rows||[]).length){tb.innerHTML='<tr><td colspan=\"6\" class=\"muted\">Sem arquivos.</td></tr>'}}"
    "catch(e){toast(e.message,true)}}"
    "async function adminRestoreArchive(id){try{await api('/api/admin/archives/restore',{method:'POST',body:JSON.stringify({archive_id:id})});toast('Arquivo restaurado');loadAdminArchives()}catch(e){toast(e.message,true)}}"
    "async function adminDeleteArchive(id){try{await api('/api/admin/archives/delete',{method:'POST',body:JSON.stringify({archive_id:id})});toast('Arquivo apagado');loadAdminArchives()}catch(e){toast(e.message,true)}}"
    "async function loadAdminSessions(){try{let j=await api('/api/admin/sessions?limit=120');if($('adm_sessions'))$('adm_sessions').textContent=_admJson(j.rows||[])}catch(e){if($('adm_sessions'))$('adm_sessions').textContent='Erro: '+e.message}}"
    "async function loadAdminLogs(){try{let j=await api('/api/admin/logs?lines=160');if($('adm_logs'))$('adm_logs').textContent=(j.lines||[]).join('\\n')||'Sem logs.'}catch(e){if($('adm_logs'))$('adm_logs').textContent='Erro: '+e.message}}"
    "async function adminRunBackup(){try{let j=await api('/api/admin/maintenance/backup',{method:'POST',body:'{}'});toast(j.message||'Backup concluído');loadAdminOverview()}catch(e){toast(e.message,true)}}"
    "async function adminRunCleanup(){try{let j=await api('/api/admin/maintenance/cleanup',{method:'POST',body:'{}'});toast(j.message||'Limpeza concluída');loadAdminOverview()}catch(e){toast(e.message,true)}}"
    "async function loadAdminAll(){await loadAdminOverview();await loadAdminArchives();await loadAdminSessions();await loadAdminLogs()}"
)

_ADMIN_CENTER_PAGE = (
    '<div id="page-admin" class="page"><div class="title"><div>'
    "<h1>Administração</h1>"
    '<div class="muted">Escolha uma secção para configurar.</div>'
    '</div><button class="btn" onclick="loadAdminCenter()">Atualizar</button></div>'
    '<section class="card" style="padding:12px 16px;margin-bottom:12px">'
    '<div class="adm2-nav" style="display:flex;gap:8px;flex-wrap:wrap">'
    '<button type="button" class="btn primary" id="adm2_btn_password" onclick="adminShowPanel(\'password\')">'
    '🔐 Password admin</button>'
    '<button type="button" class="btn" id="adm2_btn_emojis" onclick="adminShowPanel(\'emojis\')">'
    '😀 Emojis de notificação</button>'
    '<button type="button" class="btn" id="adm2_btn_users" onclick="adminShowPanel(\'users\')">'
    '👥 Utilizadores / roles</button>'
    '<button type="button" class="btn" id="adm2_btn_bindings" onclick="adminShowPanel(\'bindings\')">'
    '🖥️ PC / Bindings</button>'
    '<button type="button" class="btn" id="adm2_btn_db" onclick="adminShowPanel(\'db\')">'
    '🗄️ Administração DB</button>'
    "</div></section>"
    '<section class="card adm2-panel" id="adm2_panel_password" style="padding:16px;margin-bottom:12px">'
    '<h3 style="margin:0 0 10px">Password Admin</h3>'
    '<div class="grid">'
    '<div class="field"><label>Nova password</label><input id="adm2_pass1" type="password" placeholder="••••••"></div>'
    '<div class="field"><label>Confirmar password</label><input id="adm2_pass2" type="password" placeholder="••••••"></div>'
    '<div class="field" style="align-self:end"><button class="btn primary" onclick="saveAdminPassword()">Guardar password</button></div>'
    '<div class="field" style="grid-column:1/-1"><div id="adm2_pass_status" class="muted">Sem alterações.</div></div>'
    "</div></section>"
    '<section class="card adm2-panel" id="adm2_panel_emojis" style="padding:16px;margin-bottom:12px;display:none">'
    '<h3 style="margin:0 0 10px">Emojis de Notificação</h3>'
    '<div class="grid">'
    '<div class="field"><label>Bloqueado</label><input id="adm2_emoji_bloqueado" placeholder="🚫"></div>'
    '<div class="field"><label>Novo</label><input id="adm2_emoji_new" placeholder="🆕"></div>'
    '<div class="field"><label>Atraso</label><input id="adm2_emoji_atraso" placeholder="⏰"></div>'
    '<div class="field" style="grid-column:1/-1"><button class="btn primary" onclick="saveAdminEmojis()">Guardar emojis</button></div>'
    "</div></section>"
    '<section class="card adm2-panel" id="adm2_panel_users" style="padding:16px;display:none">'
    '<h3 style="margin:0 0 10px">Utilizadores / Roles</h3>'
    '<div class="muted" style="margin-bottom:12px">Configure o utilizador da app, a <b>conta Windows</b> (login noutro PC) '
    'e o <b>PC principal</b> (login automático por máquina). PCs extra em <b>PC / Bindings</b>.</div>'
    '<div class="grid" style="margin-bottom:14px;padding-bottom:14px;border-bottom:1px solid #e5e7eb">'
    '<div class="field"><label>Username (novo)</label><input id="adm2_new_username" placeholder="ex.: jsmith"></div>'
    '<div class="field"><label>Nome a apresentar</label><input id="adm2_new_display" placeholder="ex.: João Silva"></div>'
    '<div class="field"><label>Conta Windows</label><input id="adm2_new_windows" placeholder="ex.: nestar"></div>'
    '<div class="field"><label>PC principal</label><input id="adm2_new_machine_user" placeholder="ex.: LHSLTOFF0024"></div>'
    '<div class="field"><label>Role</label><select id="adm2_new_role">'
    '<option value="read">read</option><option value="edit">edit</option><option value="admin">admin</option>'
    '</select></div>'
    '<div class="field" style="align-self:end"><label><input type="checkbox" id="adm2_new_active" checked> Ativo</label></div>'
    '<div class="field" style="grid-column:1/-1;display:flex;gap:8px;flex-wrap:wrap">'
    '<button class="btn" type="button" onclick="adm2FillNewWindowsAccount()">Usar conta Windows deste PC</button>'
    '<button class="btn" type="button" onclick="adm2FillNewUserMachine()">Usar PC deste servidor</button>'
    '<button class="btn primary" type="button" onclick="createAdminUser()">+ Adicionar utilizador</button></div></div>'
    '<div class="table-wrap"><table><thead><tr>'
    '<th>Username</th><th>Nome</th><th>Conta Windows</th><th>PC principal</th><th>Role</th><th>Ativo</th><th>Ações</th>'
    '</tr></thead><tbody id="adm2_users"></tbody></table></div></section>'
    '<section class="card adm2-panel" id="adm2_panel_bindings" style="padding:16px;display:none">'
    '<h3 style="margin:0 0 10px">PC / Bindings</h3>'
    '<div class="muted" style="margin-bottom:12px">Associe o nome do PC ao utilizador para login automático no modo <b>PC/Máquina</b>.</div>'
    '<div class="grid" style="margin-bottom:14px;padding-bottom:14px;border-bottom:1px solid #e5e7eb">'
    '<div class="field"><label>Nome do PC</label><input id="adm2_new_machine" placeholder="ex.: LHSLTOFF0024"></div>'
    '<div class="field"><label>Utilizador</label><select id="adm2_new_bind_user"></select></div>'
    '<div class="field" style="align-self:end"><label><input type="checkbox" id="adm2_new_bind_active" checked> Ativo</label></div>'
    '<div class="field" style="align-self:end;display:flex;gap:8px;flex-wrap:wrap">'
    '<button class="btn" type="button" onclick="adm2FillServerMachine()">Usar PC deste servidor</button>'
    '<button class="btn primary" type="button" onclick="createAdminBinding()">+ Associar PC</button></div></div>'
    '<div class="table-wrap"><table><thead><tr>'
    '<th>PC / Máquina</th><th>Utilizador</th><th>Nome</th><th>Ativo</th><th>Ações</th>'
    '</tr></thead><tbody id="adm2_bindings"></tbody></table></div></section>'
    + _ADMIN_HEAVY_HTML +
    "</div>"
)

_ADMIN_CENTER_JS = (
    "let _adm2Panel='password';"
    "function adminCanManage(){return String(user?.role||'').toLowerCase()==='admin'}"
    "function ensureAdminPage(){if(!adminCanManage())throw new Error('Apenas admin')}"
    "function ensureAdminNav(){const b=$('nav-admin');if(!b)return;b.style.display=adminCanManage()?'':'none'}"
    "function _adm2ApplyPanel(){const panels={password:'adm2_panel_password',emojis:'adm2_panel_emojis',users:'adm2_panel_users',bindings:'adm2_panel_bindings',db:'adm2_panel_db'};"
    "const btns={password:'adm2_btn_password',emojis:'adm2_btn_emojis',users:'adm2_btn_users',bindings:'adm2_btn_bindings',db:'adm2_btn_db'};"
    "Object.keys(panels).forEach(k=>{const el=$(panels[k]);if(el)el.style.display=(k===_adm2Panel)?'':'none';"
    "const b=$(btns[k]);if(b)b.classList.toggle('primary',k===_adm2Panel)});}"
    "async function adminShowPanel(p,skipGuard){const next=String(p||'password');"
    "const run=async()=>{_adm2Panel=next;_adm2ApplyPanel();try{"
    "if(_adm2Panel==='users')await loadAdminUsers2();else if(_adm2Panel==='bindings')await loadAdminBindings2();"
    "else if(_adm2Panel==='db')await loadAdminAll();else await loadAdminSettings();"
    "}catch(e){toast(e.message,true)}};"
    "if(!skipGuard&&window.__unsavedDirty&&typeof _unsavedNavigate==='function')return _unsavedNavigate(()=>adminShowPanel(next,true));"
    "return run()}"
    "async function loadAdminSettings(){ensureAdminPage();let j=await api('/api/admin/settings');"
    "if($('adm2_emoji_bloqueado'))$('adm2_emoji_bloqueado').value=String(j.emoji_bloqueado||'🚫');"
    "if($('adm2_emoji_new'))$('adm2_emoji_new').value=String(j.emoji_new||'🆕');"
    "if($('adm2_emoji_atraso'))$('adm2_emoji_atraso').value=String(j.emoji_atraso||'⏰');"
    "if($('adm2_pass_status'))$('adm2_pass_status').textContent=(j.admin_password_set?'Password admin definida.':'Password admin não definida.')}"
    "async function saveAdminPassword(){try{ensureAdminPage();const p1=String($('adm2_pass1')?.value||'');const p2=String($('adm2_pass2')?.value||'');"
    "if(!p1||p1.length<4){toast('Password deve ter pelo menos 4 caracteres',true);return}"
    "if(p1!==p2){toast('Password e confirmação não coincidem',true);return}"
    "await api('/api/admin/settings/password',{method:'POST',body:JSON.stringify({password:p1})});"
    "if($('adm2_pass1'))$('adm2_pass1').value='';if($('adm2_pass2'))$('adm2_pass2').value='';"
    "if($('adm2_pass_status'))$('adm2_pass_status').textContent='Password admin atualizada.';unsavedClear();toast('Password admin atualizada')}"
    "catch(e){toast(e.message,true)}}"
    "async function saveAdminEmojis(){try{ensureAdminPage();"
    "const emoji_bloqueado=String($('adm2_emoji_bloqueado')?.value||'🚫').trim()||'🚫';"
    "const emoji_new=String($('adm2_emoji_new')?.value||'🆕').trim()||'🆕';"
    "const emoji_atraso=String($('adm2_emoji_atraso')?.value||'⏰').trim()||'⏰';"
    "await api('/api/admin/settings/emojis',{method:'POST',body:JSON.stringify({emoji_bloqueado,emoji_new,emoji_atraso})});"
    "unsavedClear();toast('Emojis atualizados')}"
    "catch(e){toast(e.message,true)}}"
    "async function loadAdminUsers2(){try{ensureAdminPage();await adm2LoadAuthMeta();let j=await api('/api/auth/users');const tb=$('adm2_users');if(!tb)return;tb.innerHTML='';"
    "(j.rows||[]).forEach(r=>{const tr=document.createElement('tr');const key=String(r.username||'').replace(/[^\\w\\-]/g,'_');"
    "const u=encodeURIComponent(r.username||'');const extra=Array.isArray(r.machines)?r.machines.filter(m=>m&&m!==r.primary_machine):[];"
    "const extraHint=extra.length?('<div class=\"muted\" style=\"font-size:11px\">+PCs: '+esc(extra.join(', '))+'</div>'):'';"
    "tr.innerHTML='<td>'+esc(String(r.username||''))+'</td>'"
    "+'<td><input id=\"adm2_disp_'+key+'\" value=\"'+esc(String(r.display_name||''))+'\" style=\"width:100%;min-width:110px\"></td>'"
    "+'<td><input id=\"adm2_win_'+key+'\" value=\"'+esc(String(r.windows_account||''))+'\" style=\"width:100%;min-width:100px\"></td>'"
    "+'<td><input id=\"adm2_mach_'+key+'\" value=\"'+esc(String(r.primary_machine||''))+'\" style=\"width:100%;min-width:110px\">'+extraHint+'</td>'"
    "+'<td><select id=\"adm2_role_'+key+'\"><option value=\"read\">read</option><option value=\"edit\">edit</option><option value=\"admin\">admin</option></select></td>'"
    "+'<td><label><input type=\"checkbox\" id=\"adm2_active_'+key+'\"> ativo</label></td>'"
    "+'<td style=\"white-space:nowrap\"><button class=\"btn\" type=\"button\" onclick=\"saveAdminUser2(\\''+u+'\\')\">Guardar</button> '"
    "+'<button class=\"btn danger\" type=\"button\" onclick=\"deleteAdminUser(\\''+u+'\\')\">Apagar</button></td>';"
    "tb.appendChild(tr);const rs=$('adm2_role_'+key);if(rs)rs.value=r.role||'read';const ac=$('adm2_active_'+key);if(ac)ac.checked=!!r.active});"
    "if(!(j.rows||[]).length){tb.innerHTML='<tr><td colspan=\"7\" class=\"muted\">Sem utilizadores.</td></tr>'}}catch(e){toast(e.message,true)}}"
    "async function createAdminUser(){try{ensureAdminPage();const username=String($('adm2_new_username')?.value||'').trim();"
    "const display_name=String($('adm2_new_display')?.value||'').trim();const windows_account=String($('adm2_new_windows')?.value||'').trim();"
    "const primary_machine=String($('adm2_new_machine_user')?.value||'').trim();const role=String($('adm2_new_role')?.value||'read');"
    "const active=!!$('adm2_new_active')?.checked;if(!username){toast('Username é obrigatório',true);return}"
    "await api('/api/auth/users',{method:'POST',body:JSON.stringify({username,display_name:display_name||username,windows_account,primary_machine,role,active})});"
    "if($('adm2_new_username'))$('adm2_new_username').value='';if($('adm2_new_display'))$('adm2_new_display').value='';"
    "if($('adm2_new_windows'))$('adm2_new_windows').value='';if($('adm2_new_machine_user'))$('adm2_new_machine_user').value='';"
    "unsavedClear();toast('Utilizador adicionado');await loadAdminUsers2()}catch(e){toast(e.message,true)}}"
    "async function saveAdminUser2(encUser){try{const u=decodeURIComponent(encUser);const key=String(u||'').replace(/[^\\w\\-]/g,'_');"
    "const display_name=String($('adm2_disp_'+key)?.value||'').trim();const windows_account=String($('adm2_win_'+key)?.value||'').trim();"
    "const primary_machine=String($('adm2_mach_'+key)?.value||'').trim();const role=$('adm2_role_'+key)?.value||'read';"
    "const active=!!$('adm2_active_'+key)?.checked;"
    "await api('/api/auth/users/update',{method:'POST',body:JSON.stringify({username:u,display_name,windows_account,primary_machine,role,active})});"
    "unsavedClear();toast('Utilizador atualizado');await loadAdminUsers2()}catch(e){toast(e.message,true)}}"
    "async function deleteAdminUser(encUser){try{const u=decodeURIComponent(encUser);"
    "if(!confirm('Apagar o utilizador \"'+u+'\"? Esta ação remove o acesso (pode voltar a criar o mesmo username).'))return;"
    "await api('/api/auth/users/delete',{method:'POST',body:JSON.stringify({username:u})});"
    "unsavedClear();toast('Utilizador apagado');await loadAdminUsers2()}catch(e){toast(e.message,true)}}"
    "let _adm2ServerMachine='';let _adm2WindowsUser='';"
    "async function adm2LoadAuthMeta(){try{const j=await api('/api/auth/meta');"
    "_adm2ServerMachine=String(j.server_machine||'').trim();"
    "_adm2WindowsUser=String(j.windows_user_normalized||j.windows_user||'').trim()}catch(_){_adm2ServerMachine='';_adm2WindowsUser=''}}"
    "function adm2FillNewWindowsAccount(){if($('adm2_new_windows'))$('adm2_new_windows').value=_adm2WindowsUser||$('adm2_new_windows').value||'';"
    "if($('adm2_new_username')&&!String($('adm2_new_username').value||'').trim()&&_adm2WindowsUser)$('adm2_new_username').value=_adm2WindowsUser}"
    "function adm2FillNewUserMachine(){if($('adm2_new_machine_user'))$('adm2_new_machine_user').value=_adm2ServerMachine||$('adm2_new_machine_user').value||''}"
    "async function adm2LoadServerMachine(){await adm2LoadAuthMeta()}"
    "function adm2FillServerMachine(){adm2FillNewUserMachine();if($('adm2_new_machine'))$('adm2_new_machine').value=_adm2ServerMachine||$('adm2_new_machine').value||''}"
    "function adm2FillBindUserSelect(rows,selected){const sel=$('adm2_new_bind_user');if(!sel)return;sel.innerHTML='';"
    "(rows||[]).forEach(r=>{const o=document.createElement('option');o.value=String(r.username||'');"
    "o.textContent=String(r.username||'')+(r.display_name?(' — '+r.display_name):'');sel.appendChild(o)});"
    "if(selected)sel.value=selected}"
    "async function loadAdminBindings2(){try{ensureAdminPage();await adm2LoadServerMachine();"
    "const [bj,uj]=await Promise.all([api('/api/auth/bindings'),api('/api/auth/users')]);"
    "adm2FillBindUserSelect(uj.rows||[]);const tb=$('adm2_bindings');if(!tb)return;tb.innerHTML='';"
    "(bj.rows||[]).forEach(r=>{const tr=document.createElement('tr');const key=String(r.machine||'').replace(/[^\\w\\-]/g,'_');"
    "const m=encodeURIComponent(r.machine||'');"
    "tr.innerHTML='<td>'+esc(String(r.machine||''))+'</td>'"
    "+'<td><select id=\"adm2_bind_user_'+key+'\"></select></td>'"
    "+'<td id=\"adm2_bind_disp_'+key+'\">'+esc(String(r.display_name||''))+'</td>'"
    "+'<td><label><input type=\"checkbox\" id=\"adm2_bind_active_'+key+'\"> ativo</label></td>'"
    "+'<td style=\"white-space:nowrap\"><button class=\"btn\" type=\"button\" onclick=\"saveAdminBinding2(\\''+m+'\\')\">Guardar</button> '"
    "+'<button class=\"btn danger\" type=\"button\" onclick=\"deleteAdminBinding(\\''+m+'\\')\">Apagar</button></td>';"
    "tb.appendChild(tr);const us=$('adm2_bind_user_'+key);if(us){(uj.rows||[]).forEach(u=>{const o=document.createElement('option');"
    "o.value=String(u.username||'');o.textContent=String(u.username||'');us.appendChild(o)});us.value=r.username||''}"
    "const ac=$('adm2_bind_active_'+key);if(ac)ac.checked=!!r.active});"
    "if(!(bj.rows||[]).length){tb.innerHTML='<tr><td colspan=\"5\" class=\"muted\">Sem PCs associados.</td></tr>'}}catch(e){toast(e.message,true)}}"
    "async function createAdminBinding(){try{ensureAdminPage();const machine=String($('adm2_new_machine')?.value||'').trim();"
    "const username=String($('adm2_new_bind_user')?.value||'').trim();const active=!!$('adm2_new_bind_active')?.checked;"
    "if(!machine){toast('Nome do PC é obrigatório',true);return}if(!username){toast('Selecione um utilizador',true);return}"
    "await api('/api/auth/bindings',{method:'POST',body:JSON.stringify({machine,username,active})});"
    "if($('adm2_new_machine'))$('adm2_new_machine').value='';unsavedClear();toast('PC associado');await loadAdminBindings2()}"
    "catch(e){toast(e.message,true)}}"
    "async function saveAdminBinding2(encMachine){try{const machine=decodeURIComponent(encMachine);const key=String(machine||'').replace(/[^\\w\\-]/g,'_');"
    "const username=String($('adm2_bind_user_'+key)?.value||'').trim();const active=!!$('adm2_bind_active_'+key)?.checked;"
    "await api('/api/auth/bindings/update',{method:'POST',body:JSON.stringify({machine,username,active})});"
    "unsavedClear();toast('Binding atualizado');await loadAdminBindings2()}catch(e){toast(e.message,true)}}"
    "async function deleteAdminBinding(encMachine){try{const machine=decodeURIComponent(encMachine);"
    "if(!confirm('Apagar o binding do PC \"'+machine+'\"?'))return;"
    "await api('/api/auth/bindings/delete',{method:'POST',body:JSON.stringify({machine})});"
    "unsavedClear();toast('Binding apagado');await loadAdminBindings2()}catch(e){toast(e.message,true)}}"
    "async function loadAdminCenter(){try{ensureAdminPage();await adminShowPanel(_adm2Panel||'password',true)}catch(e){toast(e.message,true)}}"
)

_AUTH_MODAL_HTML = (
    '<div class="modal-bg" id="auth-modal" style="display:none">'
    '<div class="modal" style="width:min(520px,94vw)">'
    '<div class="mh"><b>Login</b></div>'
    '<div class="mb">'
    '<div class="field"><label>Modo</label><select id="auth_mode" onchange="authModeUI()">'
    '<option value="pc">PC/Máquina</option><option value="windows">Windows</option><option value="pass">Password</option>'
    '</select></div>'
    '<div class="field" id="auth_machine_wrap"><label>Máquina/PC</label><input id="auth_machine" placeholder="Nome da máquina/PC">'
    '<div id="auth_machine_hint" class="muted" style="font-size:12px;margin-top:4px"></div>'
    '<button type="button" class="btn" style="margin-top:6px" onclick="authUseServerMachine()">Usar PC detetado</button></div>'
    '<div class="field" id="auth_binding_wrap"><label>Estado do binding</label><div id="auth_binding" class="muted">A verificar...</div></div>'
    '<div class="field" id="auth_windows_wrap" style="display:none"><label>Utilizador Windows detetado</label><div id="auth_windows_user" class="muted">A detetar...</div></div>'
    '<div class="field" id="auth_user_wrap"><label>Utilizador</label><input id="auth_user" placeholder="username"></div>'
    '<div class="field" id="auth_pass_wrap" style="display:none"><label>Password</label><input id="auth_pass" type="password" placeholder="password"></div>'
    '<div class="field" id="auth_role_wrap"><label>Role (fallback)</label><select id="auth_role"><option value="read">read</option><option value="edit">edit</option><option value="admin">admin</option></select></div>'
    '<div class="muted" id="auth_msg" style="margin-top:8px"></div>'
    '<div class="mf" style="margin-top:10px"><button class="btn primary" onclick="authLogin()">Entrar</button></div>'
    "</div></div></div>"
)

_LOADING_OVERLAY_HTML = (
    '<div class="modal-bg" id="loading-modal" style="display:none;z-index:9999">'
    '<div class="modal" style="width:min(420px,90vw)">'
    '<div class="mh"><b>A carregar</b></div>'
    '<div class="mb">'
    '<div class="muted" id="loading-msg" style="margin-bottom:10px">A atualizar dados...</div>'
    '<div style="height:10px;background:#e5e7eb;border-radius:999px;overflow:hidden">'
    '<div id="loading-bar" style="height:10px;width:18%;background:#2563eb;border-radius:999px;transition:width .2s ease"></div>'
    "</div></div></div></div>"
)

_UNSAVED_MODAL_HTML = (
    '<div class="modal-bg" id="unsaved-modal" style="display:none;z-index:10001">'
    '<div class="modal" style="width:min(460px,92vw)">'
    '<div class="mh"><b>Alterações não guardadas</b></div>'
    '<div class="mb">'
    '<div class="muted" id="unsaved_msg" style="margin-bottom:12px">Tem alterações não guardadas.</div>'
    '<div class="mf" style="display:flex;gap:8px;flex-wrap:wrap">'
    '<button class="btn primary" onclick="unsavedGuardSaveAndExit()">Guardar e sair</button>'
    '<button class="btn danger" onclick="unsavedGuardLeaveWithoutSave()">Sair sem guardar</button>'
    '<button class="btn" onclick="unsavedGuardCancel()">Cancelar</button>'
    "</div></div></div></div>"
)

_UNSAVED_GUARD_JS = (
    "window.__unsavedDirty=false;window.__unsavedCtx='';window.__unsavedNext=null;window.__unsavedBusy=false;window.__unsavedBypass=false;window.__unsavedLastEl=null;"
    "function _unsavedIsEditable(el){if(!el)return false;if(el.disabled||el.readOnly)return false;return true}"
    "function _unsavedCtxFor(el){if(!el)return '';if(!_unsavedIsEditable(el)&&!el.isContentEditable)return '';const id=String(el.id||'');"
    "if(!id&&!(el.isContentEditable||el.closest('#td_deps_edit')))return '';"
    "if(id.startsWith('tf_')||id.startsWith('f_')||id.startsWith('db_')||id.startsWith('sch_f_')||id.startsWith('ef_')||id.startsWith('auth_'))return '';"
    "if(id.startsWith('tm_')||el.closest('#task-modal'))return 'task_modal';"
    "if(id.startsWith('sm_')||el.closest('#sched-modal'))return 'scheduled';"
    "if(id==='notes_rt'||el.closest('#page-notes'))return 'notes';"
    "if(id.startsWith('adm2_pass'))return 'admin_password';"
    "if(id.startsWith('adm2_emoji'))return 'admin_emojis';"
    "if(id==='sys_list_values'||id==='sys_list_type')return 'system_lists';"
    "if(id.startsWith('adm2_disp_')||id.startsWith('adm2_role_')||id.startsWith('adm2_active_')||id.startsWith('adm2_win_')||id.startsWith('adm2_mach_'))return 'admin_user';"
    "if(id==='adm2_new_username'||id==='adm2_new_display'||id==='adm2_new_role'||id==='adm2_new_active'||id==='adm2_new_windows'||id==='adm2_new_machine_user')return 'admin_user_new';"
    "if(id.startsWith('adm2_bind_user_')||id.startsWith('adm2_bind_active_'))return 'admin_binding';"
    "if(id==='adm2_new_machine'||id==='adm2_new_bind_user'||id==='adm2_new_bind_active')return 'admin_binding_new';"
    "if(id.startsWith('td_f_')||id==='td_links_edit'||id==='td_pasta_edit'||id==='td_desc_rt'||id==='td_res_ini_rt'||id==='td_res_fim_rt'||el.closest('#td_deps_edit')){"
    "if(typeof _detailEdit!=='undefined'&&!_detailEdit)return '';return 'task_detail'}"
    "return ''}"
    "function unsavedMarkDirty(ctx,el){if(!ctx)return;window.__unsavedDirty=true;window.__unsavedCtx=ctx||window.__unsavedCtx||'';if(el)window.__unsavedLastEl=el}"
    "function unsavedClear(){window.__unsavedDirty=false;window.__unsavedCtx='';window.__unsavedLastEl=null}"
    "function _unsavedShow(){if($('unsaved_msg'))$('unsaved_msg').textContent='Tem alterações não guardadas.';if($('unsaved-modal'))$('unsaved-modal').style.display='flex'}"
    "function _unsavedHide(){if($('unsaved-modal'))$('unsaved-modal').style.display='none'}"
    "function unsavedGuardCancel(){window.__unsavedNext=null;_unsavedHide()}"
    "async function _unsavedTrySave(){const c=String(window.__unsavedCtx||'');if(!window.__unsavedDirty)return true;try{window.__unsavedBypass=true;"
    "if(c==='task_modal'&&typeof saveTaskModal==='function')await saveTaskModal();"
    "else if(c==='task_detail'&&typeof saveTaskDetail==='function')await saveTaskDetail();"
    "else if(c==='notes'&&typeof saveNotes==='function')await saveNotes(false);"
    "else if(c==='scheduled'&&typeof saveSchedModal==='function')await saveSchedModal();"
    "else if(c==='admin_password'&&typeof saveAdminPassword==='function')await saveAdminPassword();"
    "else if(c==='admin_emojis'&&typeof saveAdminEmojis==='function')await saveAdminEmojis();"
    "else if(c==='system_lists'&&typeof saveSystemLists==='function')await saveSystemLists();"
    "else if(c==='admin_user'){const el=window.__unsavedLastEl;const tr=el&&el.closest?el.closest('tr'):null;"
    "const bt=tr?tr.querySelector('button[onclick*=\"saveAdminUser2(\"]'):null;"
    "if(bt){const m=String(bt.getAttribute('onclick')||'').match(/saveAdminUser2\\('([^']+)'\\)/);if(m&&typeof saveAdminUser2==='function')await saveAdminUser2(m[1]);}}"
    "else if(c==='admin_user_new'&&typeof createAdminUser==='function')await createAdminUser();"
    "else if(c==='admin_binding'){const el=window.__unsavedLastEl;const tr=el&&el.closest?el.closest('tr'):null;"
    "const bt=tr?tr.querySelector('button[onclick*=\"saveAdminBinding2(\"]'):null;"
    "if(bt){const m=String(bt.getAttribute('onclick')||'').match(/saveAdminBinding2\\('([^']+)'\\)/);if(m&&typeof saveAdminBinding2==='function')await saveAdminBinding2(m[1]);}}"
    "else if(c==='admin_binding_new'&&typeof createAdminBinding==='function')await createAdminBinding();"
    "}catch(_){ }finally{window.__unsavedBypass=false}return !window.__unsavedDirty}"
    "async function unsavedGuardSaveAndExit(){if(window.__unsavedBusy)return;window.__unsavedBusy=true;const ok=await _unsavedTrySave();window.__unsavedBusy=false;"
    "if(!ok){toast('Guarde as alterações antes de sair.',true);return}"
    "const next=window.__unsavedNext;window.__unsavedNext=null;unsavedClear();_unsavedHide();if(typeof next==='function')next()}"
    "function unsavedGuardLeaveWithoutSave(){const next=window.__unsavedNext;window.__unsavedNext=null;unsavedClear();_unsavedHide();if(typeof next==='function')next()}"
    "function _unsavedNavigate(next){if(window.__unsavedBypass||!window.__unsavedDirty){next();return true}window.__unsavedNext=next;_unsavedShow();return false}"
    "function _unsavedWrapClosers(){if(typeof closeTaskModal==='function'&&!closeTaskModal.__unsavedWrapped){const o=closeTaskModal;closeTaskModal=function(){return _unsavedNavigate(()=>o())};closeTaskModal.__unsavedWrapped=true}"
    "if(typeof closeSchedModal==='function'&&!closeSchedModal.__unsavedWrapped){const o2=closeSchedModal;closeSchedModal=function(){return _unsavedNavigate(()=>o2())};closeSchedModal.__unsavedWrapped=true}}"
    "function _unsavedInit(){if(window.__unsavedInitDone)return;window.__unsavedInitDone=true;"
    "document.addEventListener('input',ev=>{const el=ev.target;const c=_unsavedCtxFor(el);if(c)unsavedMarkDirty(c,el)},true);"
    "document.addEventListener('change',ev=>{const el=ev.target;const c=_unsavedCtxFor(el);if(c)unsavedMarkDirty(c,el)},true);"
    "window.addEventListener('beforeunload',ev=>{if(window.__unsavedDirty){ev.preventDefault();ev.returnValue=''}});"
    "const _origShowPage=(window.showPage||showPage);window.showPage=function(p){return _unsavedNavigate(()=>_origShowPage(p))};showPage=window.showPage;"
    "_unsavedWrapClosers();setTimeout(_unsavedWrapClosers,1200)}"
)

_AUTH_JS = (
    "let _authChecked=false,_idleDeadline=0,_idleTimer=null;"
    "let _authMeta=null;"
    "const AUTH_MACHINE_KEY='webui.auth.machine.v1';"
    "const AUTH_POST_LOGIN_REFRESH_KEY='webui.auth.postlogin.refresh.v1';"
    "let _loadingTick=null,_loadingPct=18;"
    "function _bumpIdle(){_idleDeadline=Date.now()+(_authIdleMs*1000)}"
    "let _authIdleMs=2700;"
    "function authMachineGet(){try{return String(localStorage.getItem(AUTH_MACHINE_KEY)||'').trim()}catch(_){return ''}}"
    "function authMachineSet(v){try{const m=String(v||'').trim();if(m)localStorage.setItem(AUTH_MACHINE_KEY,m)}catch(_){}}"
    "function loadingShow(msg){if($('loading-msg'))$('loading-msg').textContent=msg||'A atualizar dados...';"
    "if($('loading-modal'))$('loading-modal').style.display='flex';_loadingPct=18;"
    "if($('loading-bar'))$('loading-bar').style.width=_loadingPct+'%';"
    "if(_loadingTick)clearInterval(_loadingTick);_loadingTick=setInterval(()=>{_loadingPct=Math.min(92,_loadingPct+Math.floor(Math.random()*6)+2);"
    "if($('loading-bar'))$('loading-bar').style.width=_loadingPct+'%'},180)}"
    "function loadingHide(){if(_loadingTick){clearInterval(_loadingTick);_loadingTick=null}"
    "if($('loading-bar'))$('loading-bar').style.width='100%';setTimeout(()=>{if($('loading-modal'))$('loading-modal').style.display='none';"
    "if($('loading-bar'))$('loading-bar').style.width='18%'},180)}"
    "function authPostLoginRefresh(){try{if(sessionStorage.getItem(AUTH_POST_LOGIN_REFRESH_KEY)==='1'){sessionStorage.removeItem(AUTH_POST_LOGIN_REFRESH_KEY);return false}"
    "sessionStorage.setItem(AUTH_POST_LOGIN_REFRESH_KEY,'1')}catch(_){ }"
    "location.reload();return true}"
    "function authApplyUser(u){if(!u)return;try{if(typeof roleNorm==='function')u.role=roleNorm(u.role);user=u}catch(_){ }"
    "if($('uname'))$('uname').textContent=String(u.display_name||u.username||'');"
    "if($('urole'))$('urole').textContent=String(u.role||'read');"
    "try{if(typeof ensureAdminNav==='function')ensureAdminNav()}catch(_){}}"
    "function authUseServerMachine(){const sm=String(_authMeta?.server_machine||'').trim();if(!sm){toast('PC do servidor não detetado',true);return}"
    "if($('auth_machine'))$('auth_machine').value=sm;authMachineSet(sm);authLoadMeta()}"
    "function authModeUI(){const m=String($('auth_mode')?.value||'pc');"
    "if($('auth_machine_wrap'))$('auth_machine_wrap').style.display=(m==='pc')?'':'none';"
    "if($('auth_binding_wrap'))$('auth_binding_wrap').style.display=(m==='pc')?'':'none';"
    "if($('auth_windows_wrap'))$('auth_windows_wrap').style.display=(m==='windows')?'':'none';"
    "if($('auth_user_wrap'))$('auth_user_wrap').style.display=(m==='pass')?'':'none';"
    "if($('auth_pass_wrap'))$('auth_pass_wrap').style.display=(m==='pass')?'':'none';"
    "if($('auth_role_wrap'))$('auth_role_wrap').style.display=(m==='pc')?'none':'none';"
    "if(m==='pc'&&_authMeta&&_authMeta.binding&&_authMeta.binding.username){if($('auth_user'))$('auth_user').value=_authMeta.binding.username}"
    "if(m==='windows'){const wu=String(_authMeta?.windows_user_normalized||_authMeta?.windows_user||'').trim();"
    "if($('auth_windows_user'))$('auth_windows_user').textContent=wu||'Utilizador Windows não detetado';if($('auth_user'))$('auth_user').value=wu}"
    "if(m==='pass'){if($('auth_user')&&!$('auth_user').value)$('auth_user').value='admin'}}"
    "function authBindingMsg(meta){const b=meta&&meta.binding?meta.binding:null;"
    "if(!b||!b.username)return 'Sem binding para esta máquina';"
    "const act=(b.binding_active===false||b.binding_active===0)?'inativo':'ativo';"
    "const role=String(b.role||'read');"
    "return 'Binding encontrado: '+String(b.username)+' ('+role+', '+act+')'}"
    "async function authLoadMeta(){try{const typed=String($('auth_machine')?.value||'').trim();const mach=typed||authMachineGet();"
    "const q=mach?('?machine='+encodeURIComponent(mach)):'';let j=await api('/api/auth/meta'+q);_authMeta=j||{};"
    "const serverPc=String(j.server_machine||'').trim();"
    "if($('auth_machine')&&!String($('auth_machine').value||'').trim())$('auth_machine').value=(serverPc||mach||'');"
    "authMachineSet(String($('auth_machine')?.value||''));"
    "if($('auth_machine_hint'))$('auth_machine_hint').textContent=serverPc?('PC deste servidor: '+serverPc):'PC do servidor não detetado';"
    "if($('auth_binding')){$('auth_binding').textContent=authBindingMsg(j);"
    "$('auth_binding').style.color=(j.binding&&j.binding.username)?'#166534':'#92400e'}"
    "if($('auth_mode')&&j.suggested_mode)$('auth_mode').value=j.suggested_mode;"
    "if($('auth_windows_user'))$('auth_windows_user').textContent=String(j.windows_user_normalized||j.windows_user||'').trim()||'Utilizador Windows não detetado';"
    "if($('auth_user')&&!$('auth_user').value)$('auth_user').value=(j.binding?.username||j.windows_user_normalized||j.windows_user||'');"
    "authModeUI()}catch(_){}}"
    "function showAuthModal(msg){loadingHide();if($('auth_msg'))$('auth_msg').textContent=msg||'';if($('auth-modal'))$('auth-modal').style.display='flex';"
    "if($('auth_machine')&&!$('auth_machine').dataset.bindHook){$('auth_machine').dataset.bindHook='1';"
    "$('auth_machine').addEventListener('input',()=>authMachineSet($('auth_machine')?.value||''));"
    "$('auth_machine').addEventListener('change',()=>{authMachineSet($('auth_machine')?.value||'');authLoadMeta()})}"
    "if($('auth_machine')&&!String($('auth_machine').value||'').trim())$('auth_machine').value=authMachineGet();"
    "authLoadMeta()}"
    "function hideAuthModal(){if($('auth-modal'))$('auth-modal').style.display='none'}"
    "async function authEnsure(){if(_authChecked)return;let s=await api('/api/auth/session');if(!s.authenticated){loadingHide();showAuthModal('Faça login para continuar.');throw new Error('auth_required')}"
    "authApplyUser(s.user||null);_authChecked=true;_authIdleMs=parseInt(s.idle_sec||2700,10)||2700;_bumpIdle();setupIdleGuard()}"
    "async function authLogin(){try{const mode=String($('auth_mode')?.value||'pc');"
    "let username=String($('auth_user')?.value||'').trim();const role_hint=String($('auth_role')?.value||'read');"
    "const password=String($('auth_pass')?.value||'');const machine=String($('auth_machine')?.value||'').trim();"
    "if(mode==='pc'&&!machine){showAuthModal('Indique a máquina/PC.');return}"
    "if(mode==='windows'){username=String(_authMeta?.windows_user_normalized||_authMeta?.windows_user||username||'').trim();if($('auth_user'))$('auth_user').value=username}"
    "if(machine)authMachineSet(machine);"
    "if(mode!=='pc'&&!username){showAuthModal('Indique o utilizador.');return}"
    "if(mode==='pass'&&!password){showAuthModal('Indique a password.');return}"
    "loadingShow('A validar login...');"
    "let j=await api('/api/auth/login',{method:'POST',body:JSON.stringify({mode,username,role_hint,password,machine})});"
    "if(j&&j.user_machine)authMachineSet(j.user_machine);"
    "if(!j.authenticated){loadingHide();showAuthModal('Login inválido.');return}authApplyUser(j.user||null);hideAuthModal();_authChecked=true;_authIdleMs=parseInt(j.idle_sec||2700,10)||2700;_bumpIdle();setupIdleGuard();"
    "loadingShow('A carregar dados...');await loadAll();if(authPostLoginRefresh())return;loadingHide()}"
    "catch(e){loadingHide();showAuthModal(e.message||'Falha no login')}}"
    "async function authLogout(msg){try{await api('/api/auth/logout',{method:'POST',body:'{}'})}catch(_){ }"
    "_authChecked=false;showAuthModal(msg||'Sessão terminada')}"
    "function setupIdleGuard(){if(window._idleGuardSet)return;window._idleGuardSet=true;['click','keydown','mousemove','touchstart'].forEach(ev=>document.addEventListener(ev,_bumpIdle,{passive:true}));"
    "_idleTimer=setInterval(()=>{if(!_authChecked)return;if(Date.now()>_idleDeadline){authLogout('Sessão expirada por inatividade')}},5000)}"
)

_PROJECT_GANTT_HTML = (
    '<div id="pj-gantt-panel" class="pj-gantt-panel">'
    '<div class="pj-gantt-head" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin:0 0 10px">'
    '<h3 style="margin:0">Gantt</h3>'
    '<div style="display:flex;gap:6px;align-items:center">'
    '<button class="btn" type="button" id="pj_gantt_expand_btn" onclick="projectGanttToggleExpand()">Expandir</button>'
    "</div></div>"
    '<div id="pj-gantt-toolbar">'
    '<button class="btn" type="button" onclick="projectGanttSetView(\'Day\')">Dia</button>'
    '<button class="btn" type="button" onclick="projectGanttSetView(\'Week\')">Semana</button>'
    '<button class="btn" type="button" onclick="projectGanttSetView(\'Month\')">Mês</button>'
    '<button class="btn" type="button" onclick="projectGanttToday()">Hoje</button>'
    '<button class="btn" type="button" onclick="projectGanttRefresh()">Atualizar</button>'
    '<button class="btn primary" type="button" id="pj_gantt_toggle_actions" onclick="projectGanttToggleActions()">Ver acções</button>'
    '<span id="pj-gantt-meta"></span>'
    "</div>"
    '<div id="pj-gantt-legend"></div>'
    '<div id="pj_gantt"></div>'
    '<section id="pj-gantt-undated"><b>Sem data</b><div id="pj-gantt-undated-body" class="muted">—</div></section>'
    "</div>"
)

_PROJECT_GANTT_CSS = (
    "#page-project #pj-gantt-panel{position:relative}"
    "#page-project #pj-gantt-panel.is-max{position:fixed;inset:10px;z-index:1200;background:#fff;border:1px solid #dbe3ef;"
    "border-radius:12px;padding:16px 18px;box-shadow:0 18px 38px rgba(2,6,23,.22);overflow:auto;display:flex;flex-direction:column}"
    "#page-project #pj-gantt-panel.is-max #pj_gantt{flex:1;min-height:calc(100vh - 240px)}"
    "#page-project #pj-gantt-toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px;"
    "position:sticky;top:0;z-index:1;background:#fff;padding:4px 0 8px}"
    "#page-project #pj-gantt-toolbar .btn{padding:5px 10px;font-size:12px}"
    "#page-project #pj-gantt-toolbar .btn:hover{filter:brightness(.97)}"
    "#page-project #pj-gantt-toolbar .btn:active{transform:translateY(1px)}"
    "#page-project #pj-gantt-toolbar .btn:focus-visible{outline:2px solid #93c5fd;outline-offset:1px}"
    "#page-project #pj-gantt-meta{margin-left:auto;font-size:12px;color:#64748b}"
    "#page-project #pj-gantt-legend{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:-2px 0 8px}"
    "#page-project #pj-gantt-legend .lg{display:inline-flex;align-items:center;gap:6px;padding:2px 8px;border:1px solid #dbe3ef;border-radius:999px;background:#f8fbff;font-size:11px;color:#334155}"
    "#page-project #pj-gantt-legend .dot{width:8px;height:8px;border-radius:999px;display:inline-block}"
    "#page-project #pj-gantt-legend .d-milestone{background:#7c3aed}"
    "#page-project #pj-gantt-legend .d-task{background:#1e40af}"
    "#page-project #pj-gantt-legend .d-todo{background:#64748b}"
    "#page-project #pj-gantt-legend .d-progress{background:#2563eb}"
    "#page-project #pj-gantt-legend .d-blocked{background:#ea580c}"
    "#page-project #pj-gantt-legend .d-overdue{background:#dc2626}"
    "#page-project #pj-gantt-legend .d-done{background:#16a34a}"
    "#page-project #pj_gantt{min-height:460px;border:1px solid #e5e7eb;border-radius:8px;padding:8px;overflow:auto;background:#fff}"
    "#page-project #pj_gantt::-webkit-scrollbar{height:10px;width:10px}"
    "#page-project #pj_gantt::-webkit-scrollbar-thumb{background:#cbd5e1;border-radius:999px}"
    "#page-project #pj_gantt::-webkit-scrollbar-track{background:#f1f5f9}"
    "#page-project #pj_gantt .gantt-container{--g-row-color:#f8fafc;--g-border-color:#d7e2ee;--g-tick-color:#e8eef5;--g-tick-color-thick:#d3deea;--g-today-highlight:#0f172a}"
    "#page-project #pj_gantt .gantt .bar-label{font-size:12px;font-weight:600;text-shadow:0 1px 0 rgba(255,255,255,.25)}"
    "#page-project #pj_gantt .gantt-container .lower-text{font-size:11px;color:#64748b}"
    "#page-project #pj_gantt .gantt-container .upper-text{font-size:13px;font-weight:600;color:#1f2937}"
    "#page-project #pj_gantt .gantt .current-highlight{width:2px}"
    "#page-project #pj_gantt .gantt .current-date-highlight{font-weight:700}"
    "#page-project #pj_gantt .gantt .bar-wrapper .bar{rx:4;ry:4}"
    "#page-project #pj_gantt .gantt .bar-wrapper:hover .bar{filter:brightness(.97)}"
    "#page-project #pj_gantt .gantt .bar-wrapper:hover .bar-label{fill:#0f172a}"
    "#page-project #pj_gantt .gantt .row-line{stroke:#dbe5f0}"
    "#page-project #pj_gantt .gantt .tick{stroke:#e8eef5}"
    "#page-project #pj_gantt .gantt .tick.thick{stroke:#d3deea}"
    "#page-project #pj-gantt-undated{margin-top:12px;border:1px dashed #d3deea;border-radius:8px;padding:8px 10px;background:#f8fafc}"
    "#page-project #pj-gantt-undated b{display:block;margin-bottom:4px}"
    "#page-project #pj-gantt-undated ul{margin:8px 0 0 16px;padding:0;max-height:112px;overflow:auto}"
    "#page-project #pj-gantt-undated li{margin:4px 0;line-height:1.35}"
    "#page-project #pj_gantt .bar-progress{opacity:.9}"
    "#page-project #pj_gantt .tg-milestone .bar{fill:#8b5cf6;stroke:#6d28d9}"
    "#page-project #pj_gantt .tg-milestone .bar-progress{fill:#7c3aed}"
    "#page-project #pj_gantt .tg-task .bar{fill:#1d4ed8;stroke:#1e3a8a}"
    "#page-project #pj_gantt .tg-task .bar-progress{fill:#1e40af}"
    "#page-project #pj_gantt .tg-done .bar{fill:#22c55e}"
    "#page-project #pj_gantt .tg-done .bar-progress{fill:#16a34a}"
    "#page-project #pj_gantt .tg-progress .bar{fill:#3b82f6}"
    "#page-project #pj_gantt .tg-progress .bar-progress{fill:#2563eb}"
    "#page-project #pj_gantt .tg-blocked .bar{fill:#f97316}"
    "#page-project #pj_gantt .tg-blocked .bar-progress{fill:#ea580c}"
    "#page-project #pj_gantt .tg-overdue .bar{fill:#ef4444}"
    "#page-project #pj_gantt .tg-overdue .bar-progress{fill:#dc2626}"
    "#page-project #pj_gantt .tg-todo .bar{fill:#94a3b8}"
    "#page-project #pj_gantt .tg-todo .bar-progress{fill:#64748b}"
    "@media(max-width:1100px){#page-project #pj-gantt-toolbar{gap:6px}#page-project #pj-gantt-toolbar .btn{padding:4px 8px;font-size:11px}#page-project #pj_gantt{min-height:390px}}"
    "@media(max-width:900px){#page-project #pj-gantt-meta{width:100%;margin-left:0}#page-project #pj-gantt-undated ul{max-height:88px}}"
)

_PROJECT_GANTT_JS = (
    "let _pjGanttObj=null,_pjGanttData=null,_pjGanttView='Week',_pjShowActions=false,_pjGanttCanEdit=false,_pjGanttBusy=false;"
    "function _projectGanttFmtDate(v){try{const d=(v instanceof Date)?v:new Date(v);if(isNaN(d.getTime()))return '';"
    "const m=String(d.getMonth()+1).padStart(2,'0'),dd=String(d.getDate()).padStart(2,'0');return d.getFullYear()+'-'+m+'-'+dd}catch(_){return ''}}"
    "function _projectGanttStatusClass(it){const tp=String(it?.item_type||it?.kind||'').toUpperCase();"
    "if(tp==='MILESTONE')return 'tg-milestone';if(tp==='TASK')return 'tg-task';"
    "return (typeof _taskGanttStatusClass==='function')?_taskGanttStatusClass(it):'tg-todo'}"
    "function _projectGanttToRows(items){return(items||[]).map(it=>({"
    "id:String(it.id||''),name:String(it.name||''),start:String(it.start||''),end:String(it.end||''),"
    "progress:Number(it.progress||0),dependencies:String(it.dependencies||''),custom_class:_projectGanttStatusClass(it),_raw:it}))}"
    "function _projectGanttRenderLegend(items){const el=$('pj-gantt-legend');if(!el)return;const arr=(items||[]);"
    "let ms=0,tasks=0;const c={todo:0,progress:0,blocked:0,overdue:0,done:0};"
    "arr.forEach(it=>{const tp=String(it?.item_type||it?.kind||'').toUpperCase();"
    "if(tp==='MILESTONE'){ms++;return}if(tp==='TASK'){tasks++;return}"
    "const k=_projectGanttStatusClass(it);if(k==='tg-done')c.done++;else if(k==='tg-progress')c.progress++;"
    "else if(k==='tg-blocked')c.blocked++;else if(k==='tg-overdue')c.overdue++;else c.todo++});"
    "let html='<span class=\"lg\"><span class=\"dot d-milestone\"></span>Milestones: '+ms+'</span>'"
    "+'<span class=\"lg\"><span class=\"dot d-task\"></span>Tarefas: '+tasks+'</span>';"
    "if(_pjShowActions){html+='<span class=\"lg\"><span class=\"dot d-todo\"></span>A fazer: '+c.todo+'</span>'"
    "+'<span class=\"lg\"><span class=\"dot d-progress\"></span>Em progresso: '+c.progress+'</span>'"
    "+'<span class=\"lg\"><span class=\"dot d-blocked\"></span>Bloqueada: '+c.blocked+'</span>'"
    "+'<span class=\"lg\"><span class=\"dot d-overdue\"></span>Atrasada: '+c.overdue+'</span>'"
    "+'<span class=\"lg\"><span class=\"dot d-done\"></span>Concluída: '+c.done+'</span>'}"
    "el.innerHTML=html}"
    "function _projectGanttPopupHtml(t){const r=(t&&t._raw)||{};const tp=String(r.item_type||r.kind||'').toUpperCase();"
    "let x='';const own=String(r.owner||r.responsavel||'').trim();if(own)x+='<span>Responsável: '+esc(own)+'</span><br>';"
    "if(r.workers)x+='<span>Equipa: '+esc(r.workers)+'</span><br>';"
    "if(r.blocked_reason)x+='<span>Bloqueio: '+esc(r.blocked_reason)+'</span><br>';"
    "if(r.dependencies)x+='<span>Dependências: '+esc(r.dependencies)+'</span><br>';"
    "if(r.reason&&(!r.start&&!r.end))x+='<span class=\"muted\">'+esc(r.reason)+'</span><br>';"
    "return `<div style=\"padding:8px 10px;min-width:240px\"><b>${esc(r.name||'')}</b><br>"
    "<span class=\"muted\">${esc(r.start||'—')} → ${esc(r.end||'—')}</span><br>"
    "<span>Tipo: ${esc(tp||'—')}</span><br>"
    "<span>Milestone: ${esc(r.milestone||'—')}</span><br>"
    "<span>Tarefa: ${esc(r.task_id||'—')}</span><br>"
    "<span>Status: ${esc(r.status||'—')}</span><br>${x}</div>`}"
    "function _projectGanttRenderUndated(rows){const box=$('pj-gantt-undated-body');if(!box)return;"
    "if(!rows||!rows.length){box.innerHTML='<span class=\"muted\">Sem itens sem data.</span>';return}"
    "box.innerHTML='<ul>'+rows.map(r=>{const rs=r.reason?(' · '+esc(r.reason)):'';"
    "return `<li><b>${esc(r.item_type||r.kind||'ITEM')}</b> · ${esc(r.milestone||'')} · ${esc(r.task_id||'')} · ${esc(r.name||'—')} "
    "(${esc(r.status||'—')})${rs}</li>`}).join('')+'</ul>'}"
    "async function _projectGanttOnDateChange(t,start,end){try{if(!_pjGanttCanEdit||_pjGanttBusy)return;"
    "const r=(t&&t._raw)||{};const tp=String(r.item_type||r.kind||'').toUpperCase();"
    "if(tp==='MILESTONE'){toast('O intervalo da milestone é calculado pelas tarefas',true);return}"
    "const s=_projectGanttFmtDate(start),e=_projectGanttFmtDate(end);if(!s||!e){toast('Datas inválidas',true);return}"
    "_pjGanttBusy=true;"
    "if(tp==='TASK'){await api('/api/projects/planning/update',{method:'POST',body:JSON.stringify({updates:[{id:String(r.task_id||''),start:s,end:e}]})});"
    "if(r){r.start=s;r.end=e}}else{const aid=Number(r.action_id||0);if(!aid)return;"
    "await api('/api/actions/'+aid+'/gantt-update',{method:'POST',body:JSON.stringify({start_date:s,due_date:e})});"
    "if(r){r.start=s;r.end=e;r.start_date=s;r.due_date=e}}"
    "toast('Datas atualizadas')}catch(err){toast(err.message||'Falha ao guardar datas',true);setTimeout(()=>projectGanttRefresh(),50)}"
    "finally{_pjGanttBusy=false}}"
    "function _projectGanttRender(){const host=$('pj_gantt');if(!host)return;host.innerHTML='';"
    "if(!window.Gantt){host.innerHTML='<p class=\"muted\">Frappe Gantt não carregado.</p>';return}"
    "const srcRows=((_pjGanttData&&_pjGanttData.items)||[]);_projectGanttRenderLegend(srcRows);const rows=_projectGanttToRows(srcRows);"
    "if(!rows.length){host.innerHTML='<p class=\"muted\">Sem barras para renderizar.</p>';return}"
    "_pjGanttObj=new Gantt('#pj_gantt',rows,{view_mode:_pjGanttView||'Week',readonly:(!_pjGanttCanEdit),language:'pt',"
    "date_change:(task,start,end)=>{_projectGanttOnDateChange(task,start,end)},"
    "custom_popup_html:t=>_projectGanttPopupHtml(t)});}"
    "function projectGanttSetView(v){_pjGanttView=String(v||'Week');_projectGanttRender()}"
    "function projectGanttToday(){_pjGanttView='Day';_projectGanttRender()}"
    "function projectGanttToggleExpand(){const p=$('pj-gantt-panel');if(!p)return;const b=$('pj_gantt_expand_btn');"
    "const on=p.classList.toggle('is-max');if(b)b.textContent=on?'Restaurar':'Expandir'}"
    "function projectGanttToggleActions(){_pjShowActions=!_pjShowActions;const b=$('pj_gantt_toggle_actions');"
    "if(b)b.textContent=_pjShowActions?'Ocultar acções':'Ver acções';projectGanttRefresh()}"
    "async function projectGanttRefresh(){await projectGanttLoad()}"
    "async function projectGanttLoad(){try{"
    "const q=new URLSearchParams();q.set('projeto',$('pj_projeto')?.value||'Todos');"
    "q.set('milestone',$('pj_milestone')?.value||'Todos');q.set('include_actions',_pjShowActions?'1':'0');"
    "const j=await api('/api/projects/gantt-data?'+q);_pjGanttData=j||{};"
    "if($('pj_milestone')&&Array.isArray(j.milestones)){const cur=$('pj_milestone').value||'Todos';"
    "fill('pj_milestone',j.milestones);$('pj_milestone').value=(j.milestones.includes(cur)?cur:(j.milestone||'Todos'))}"
    "_pjGanttCanEdit=!!(j&&j.permissions&&j.permissions.can_edit);"
    "const meta=(j.milestone&&j.milestone!=='Todos')?('Milestone: '+j.milestone+' · '):'';"
    "if($('pj-gantt-meta'))$('pj-gantt-meta').textContent=meta+(_pjGanttCanEdit?'Edição ativa (drag/resize)':'Somente leitura');"
    "_projectGanttRenderUndated(j.undated_items||[]);_projectGanttRender()}catch(e){toast(e.message,true)}}"
)

_STABILITY_CSS = (
    ".td-nav{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;gap:6px;padding:0 0 12px;"
    "margin-bottom:4px;background:var(--bg,#f1f5f9)}"
    ".td-nav .btn{font-size:12px;padding:6px 10px}"
    ".diag-row{display:flex;justify-content:space-between;gap:12px;padding:8px 10px;border-radius:8px;"
    "margin-bottom:6px;font-size:13px;background:#f8fafc;border-left:4px solid #94a3b8}"
    ".diag-row.ok{border-left-color:#16a34a}"
    ".diag-row.err{border-left-color:#dc2626;background:#fef2f2}"
    ".badge.te:empty::after{content:'Não iniciado'}"
    ".content{overflow-x:hidden}"
    ".main{min-width:0}"
    ".detail-grid{grid-template-columns:minmax(0,1fr) clamp(280px,30vw,360px);align-items:start}"
    ".detail-main,.detail-side,.sec{min-width:0}"
    ".detail-side{position:sticky;top:78px;max-height:calc(100vh - 96px);overflow:auto;padding-right:2px}"
    "#td_desc_rt,#td_res_ini_rt,#td_res_fim_rt{min-height:220px}"
    ".table-wrap{overscroll-behavior:contain}"
    ".table-wrap table{width:100%}"
    "#page-tasks .table-wrap{overflow:auto;min-height:420px;max-height:calc(100vh - 405px);overscroll-behavior:contain}"
    "#page-tasks .table-wrap table{width:100%;min-width:100%;table-layout:fixed;border-collapse:separate;border-spacing:0}"
    "#page-tasks .table-wrap th,#page-tasks .table-wrap td{white-space:normal;word-break:break-word;overflow-wrap:anywhere;"
    "vertical-align:top;line-height:1.25;padding:10px 8px;overflow:hidden;text-overflow:ellipsis}"
    "#page-tasks .table-wrap thead th{position:sticky;top:0;z-index:6;background:#fff;box-shadow:0 1px 0 #e5e7eb}"
    "#page-tasks .table-wrap th.task-col-th{padding-right:14px}"
    "#page-tasks .table-wrap th .col-resizer{position:absolute;top:0;right:0;width:8px;height:100%;cursor:col-resize;z-index:4}"
    "#page-tasks .table-wrap th .col-resizer:hover{background:rgba(37,99,235,.15)}"
    "#page-tasks .table-wrap td[data-col=\"Tarefa\"],#page-tasks .table-wrap td[data-col=\"Assunto\"],#page-tasks .table-wrap td[data-col=\"Notificacoes\"]{white-space:normal}"
    "#page-tasks .table-wrap th .th-filter-arrow{display:inline-block;margin-left:6px;color:#64748b;cursor:pointer;font-size:11px;line-height:1}"
    "#page-tasks .table-wrap th .th-filter-arrow:hover{color:#0f172a}"
    ".table-wrap th{z-index:3;box-shadow:0 1px 0 #e5e7eb;background:#fff}"
    "#trows tr{transition:background-color .12s ease,box-shadow .12s ease}"
    "#trows tr:hover{background:#edf5ff}"
    "#trows tr.sel{background:#dbeafe;box-shadow:inset 3px 0 0 #2563eb}"
    "#trows tr.row-overdue td{background:#fff7ed}"
    "#trows tr.row-overdue td.status-cell{font-weight:700;color:#b91c1c}"
    "#trows tr.row-blocked td{background:#fef2f2}"
    "#trows tr.row-recent td{font-weight:600}"
    ".status-cell .te{border:1px solid #bfdbfe}"
    ".status-cell .te-prog{border-color:#7dd3fc}"
    ".status-cell .te-done{border-color:#86efac}"
    ".status-cell .te-wait{border-color:#d1d5db}"
    "#page-dashboard .db-kpis .kpi{align-items:flex-start}"
    "#page-dashboard .kpi .db-sub{font-size:11px;margin-top:2px;opacity:.85}"
    "#page-dashboard .kpi-click{cursor:pointer;transition:transform .08s ease,box-shadow .12s ease}"
    "#page-dashboard .kpi-click:hover{transform:translateY(-1px);box-shadow:0 4px 12px rgba(8,105,216,.14)}"
    "#page-dashboard .db-filters .db-grid{display:grid;grid-template-columns:repeat(6,minmax(140px,1fr));gap:10px;align-items:end}"
    "#page-dashboard .db-filters .field{margin:0}"
    "#page-dashboard .db-filters .db-open{align-self:center}"
    "#page-dashboard .db-filters .db-apply{display:flex;justify-content:flex-end}"
    "#page-dashboard .db-charts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:12px}"
    "#page-dashboard .db-chart{min-height:360px}"
    "#page-dashboard .filters{display:grid;grid-template-columns:repeat(6,minmax(140px,1fr));gap:10px;align-items:end;padding:12px!important}"
    "#page-dashboard .filters .field{margin:0;min-width:0}"
    "#page-dashboard .filters .field label{margin-bottom:4px;display:block}"
    "#page-dashboard .db-chart .chart-head{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px}"
    "#page-dashboard .db-chart .chart-head h3{margin:0;font-size:14px}"
    "#db_chart_modal .modal{width:min(1240px,96vw)}"
    "#db_chart_modal_plot{height:68vh;min-height:420px}"
    ".td-act-chips{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}"
    "#td_act_stats{display:flex;gap:6px;flex-wrap:wrap}"
    "#td_act_stats .chip{font-size:11px;padding:3px 8px;border:1px solid #dbe3ef;border-radius:999px;background:#f8fbff}"
    "#td_actions .act-section td{font-weight:700;color:#334155;padding:10px 8px 6px;border-bottom:0;background:#f8fafc}"
    "#td_actions .act-card-row td{padding:0;border-bottom:0}"
    "#td_actions .act-card{border-left:4px solid #94a3b8;border:1px solid #e5e7eb;border-left-width:4px;border-radius:6px !important;padding:4px 6px !important;margin:2px 0 !important;background:#fff}"
    "#td_actions .act-card-row.overdue .act-card{border-left-color:#dc2626;background:#fff7f7}"
    "#td_actions .act-card-row.progress .act-card{border-left-color:#2563eb;background:#eff6ff}"
    "#td_actions .act-card-row.blocked .act-card{border-left-color:#ea580c;background:#fff7ed}"
    "#td_actions .act-card-row.risk .act-card{border-left-color:#ca8a04;background:#fffbeb}"
    "#td_actions .act-card-row.done .act-card{border-left-color:#16a34a;background:#f0fdf4;opacity:.85}"
    "#td_actions .act-card-row.sel .act-card{box-shadow:0 0 0 2px #93c5fd inset}"
    ".act-top{display:flex;align-items:center;gap:6px;justify-content:space-between}"
    ".act-title{font-weight:600;margin:1px 0 !important;font-size:11px !important;line-height:1.15 !important}"
    ".act-meta{display:grid;grid-template-columns:repeat(4,minmax(100px,1fr));gap:2px;font-size:10px !important;color:#334155;line-height:1.15 !important}"
    ".badge.kind{background:#e2e8f0;color:#334155;padding:2px 8px;border-radius:999px;font-size:11px}"
    ".badge.overdue{background:#fee2e2;color:#991b1b;padding:2px 8px;border-radius:999px;font-size:11px}"
    ".badge.blocked{background:#ffedd5;color:#9a3412;padding:2px 8px;border-radius:999px;font-size:11px}"
    ".badge.progress{background:#dbeafe;color:#1d4ed8;padding:2px 8px;border-radius:999px;font-size:11px}"
    ".badge.risk{background:#fef9c3;color:#854d0e;padding:2px 8px;border-radius:999px;font-size:11px}"
    ".badge.done{background:#dcfce7;color:#166534;padding:2px 8px;border-radius:999px;font-size:11px}"
    ".badge.normal{background:#e2e8f0;color:#334155;padding:2px 8px;border-radius:999px;font-size:11px}"
    "#td_actions .check-row td{vertical-align:middle}"
    "#td_actions .check-row.sel td{background:#eef6ff}"
    "#td_actions .check-row.overdue td{background:#fff7f7}"
    "#td_actions .check-row.done td{opacity:.75}"
    "#td_actions .td-empty{padding:14px;color:#64748b;background:#f8fafc;border:1px dashed #cbd5e1;border-radius:10px}"
    ".toolbar{flex-wrap:wrap}"
    "#task-filters .grid{grid-template-columns:repeat(8,minmax(120px,1fr));gap:12px}"
    ".btn{transition:transform .08s ease,box-shadow .08s ease}"
    ".btn:hover{box-shadow:0 2px 8px rgba(8,105,216,.12)}"
    ".btn:active{transform:translateY(1px)}"
    ".toast{max-width:min(460px,92vw);line-height:1.4}"
    "@media(max-width:1600px){#page-dashboard .db-filters .db-grid{grid-template-columns:repeat(4,minmax(140px,1fr))}"
    "#page-dashboard .db-charts{grid-template-columns:repeat(2,minmax(0,1fr))}}"
    "@media(max-width:1600px){#page-dashboard .filters{grid-template-columns:repeat(4,minmax(140px,1fr))}}"
    "@media(max-width:1600px){#task-filters .grid{grid-template-columns:repeat(6,minmax(120px,1fr))}}"
    "@media(max-width:1366px){.content{padding:16px}.detail-grid{grid-template-columns:minmax(0,1fr) 300px}"
    "#task-filters .grid{grid-template-columns:repeat(4,minmax(120px,1fr))}"
    "#page-dashboard .db-filters .db-grid{grid-template-columns:repeat(3,minmax(130px,1fr))}"
    "#page-dashboard .filters{grid-template-columns:repeat(3,minmax(120px,1fr))}}"
    "@media(max-width:1180px){.detail-grid{grid-template-columns:1fr}.detail-side{position:static;max-height:none;overflow:visible}"
    "#page-dashboard .db-filters .db-grid{grid-template-columns:repeat(2,minmax(120px,1fr))}"
    "#page-dashboard .db-charts{grid-template-columns:1fr}"
    "#page-dashboard .filters{grid-template-columns:repeat(2,minmax(120px,1fr))}}"
)


def _patch_html_task_detail(html: str) -> str:
    html = html.replace(
        '<div class="detail-grid"><div class="detail-main">',
        '<div class="detail-grid"><div class="detail-main">' + _TD_NAV,
        1,
    )
    html = html.replace(
        '<section class="sec"><h3>Descrição / Notas</h3>',
        '<section class="sec" id="td_sec_desc"><h3>Descrição / Notas</h3>',
        1,
    )
    html = html.replace(
        '<section class="sec"><h3>Classificação</h3>',
        '<section class="sec" id="td_sec_class"><h3>Classificação</h3>',
        1,
    )
    html = html.replace(
        '<section class="sec"><h3>Ações / Checklist',
        '<section class="sec" id="td_sec_actions"><h3>Ações / Checklist',
        1,
    )
    html = html.replace(
        '<div class="prog"><div id="td_prog_bar"',
        '<div class="muted" id="td_act_stats" style="font-size:12px;margin-bottom:8px"></div><div class="prog"><div id="td_prog_bar"',
        1,
    )
    html = html.replace(
        '<section class="sec"><h3>Planeamento</h3>',
        '<section class="sec" id="td_sec_plan"><h3>Planeamento</h3>',
        1,
    )
    html = html.replace(
        '<section class="sec"><h3>Pasta</h3>',
        '<section class="sec" id="td_sec_folder"><h3>Pasta</h3>',
        1,
    )
    html = html.replace(
        '<section class="sec"><h3>Anexos</h3>',
        '<section class="sec" id="td_sec_att"><h3>Anexos</h3>',
        1,
    )
    html = html.replace(
        '<section class="sec"><h3>Resultados</h3>',
        '<section class="sec" id="td_sec_res"><h3>Resultados</h3>',
        1,
    )
    html = html.replace(
        '<section class="sec"><h3>Histórico</h3>',
        '<section class="sec" id="td_sec_hist"><h3>Histórico</h3>',
        1,
    )
    if "function tdScrollSec" not in html:
        html = html.replace("function tdDl(name,items)", _TD_NAV_JS + "function tdDl(name,items)", 1)
    html = html.replace(
        "$('td_side_meta').textContent='Workers: '+(t.Workers||'—')+' · Bloqueios: '+(t.blocked_count||0);",
        "$('td_side_meta').innerHTML='<div><b>Estado:</b> '+esc(t.Estado||'—')+'</div>'"
        "+'<div><b>Prazo:</b> '+esc((t.Prazo||'').slice(0,10)||'—')+'</div>'"
        "+'<div><b>Conclusão:</b> '+esc((t.DataConclusao||'').slice(0,10)||'—')+'</div>'"
        "+'<div><b>Workers:</b> '+esc(t.Workers||'—')+'</div>'"
        "+'<div><b>Bloqueios:</b> '+(t.blocked_count||0)+'</div>'"
        "+(n0((t&&((t.Private??t.private)))||0)?'<div>🔒 Privada</div>':'');"
        "updateDetailActionStats();",
        1,
    )
    html = html.replace(
        "if($('td_act_del_btn'))$('td_act_del_btn').disabled=!_detailEdit||!_detailItemSel}",
        "if($('td_act_del_btn'))$('td_act_del_btn').disabled=!_detailEdit||!_detailItemSel};updateDetailActionStats()",
        1,
    )
    return html


def _patch_html_diagnostics(html: str) -> str:
    html = html.replace(
        '<section class="card" style="padding:16px"><div class="meta" id="sys_meta"></div></section>',
        _DIAG_HTML + '<section class="card" style="padding:16px"><div class="meta" id="sys_meta"></div></section>',
        1,
    )
    marker = "async function loadSystem(){"
    if marker not in html:
        marker = "function loadSystem(){"
    if "loadDiagnostics" not in html:
        html = html.replace(marker, _DIAG_JS + marker, 1)
    html = html.replace(
        "if(page==='system')$('upd').textContent='Última atualização: '+new Date().toLocaleTimeString('pt-PT')}catch(e){toast(e.message,true)}}",
        "if(page==='system')$('upd').textContent='Última atualização: '+new Date().toLocaleTimeString('pt-PT');"
        "loadDiagnostics()}catch(e){toast(e.message,true)}}",
        1,
    )
    return html


def _patch_html_system_lists(html: str) -> str:
    html = html.replace(
        '<section class="card" style="padding:16px"><div class="meta" id="sys_meta"></div></section>',
        _SYSTEM_LISTS_HTML + '<section class="card" style="padding:16px"><div class="meta" id="sys_meta"></div></section>',
        1,
    )
    marker = "async function loadSystem(){"
    if marker not in html:
        marker = "function loadSystem(){"
    if "function loadSystemLists" not in html:
        html = html.replace(marker, _SYSTEM_LISTS_JS + marker, 1)
    html = html.replace(
        "if(page==='system')$('upd').textContent='Última atualização: '+new Date().toLocaleTimeString('pt-PT');"
        "loadDiagnostics()}catch(e){toast(e.message,true)}}",
        "if(page==='system')$('upd').textContent='Última atualização: '+new Date().toLocaleTimeString('pt-PT');"
        "loadSystemLists();loadDiagnostics()}catch(e){toast(e.message,true)}}",
        1,
    )
    return html


def _patch_html_admin_heavy(html: str) -> str:
    marker = "async function loadSystem(){"
    if marker not in html:
        marker = "function loadSystem(){"
    if "function loadAdminAll" not in html:
        html = html.replace(marker, _ADMIN_HEAVY_JS + marker, 1)
    return html


def _patch_html_auth(html: str) -> str:
    html = html.replace(
        '<div id="page-home" class="page active">',
        _AUTH_MODAL_HTML + _LOADING_OVERLAY_HTML + _UNSAVED_MODAL_HTML + '<div id="page-home" class="page active">',
        1,
    )
    marker = "async function init(){"
    if marker not in html:
        marker = "function init(){"
    if "function authEnsure" not in html:
        html = html.replace(marker, _AUTH_JS + marker, 1)
    html = html.replace(
        "let h=await api('/api/health');user=h.user;",
        "loadingShow('A iniciar sessão...');await authEnsure();let h=await api('/api/health?_ts='+Date.now());user=h.user;authApplyUser(user);loadingHide();",
        1,
    )
    if "JSON.stringify({initial:cur" not in html:
        html = html.replace(
            "async function odPickNative(){try{let j=await api('/api/folders/onedrive/pick',{method:'POST',body:'{}'});if(j.onedrive_root)$('od_path').value=j.onedrive_root}catch(e){toast(e.message,true)}}",
            "async function odPickNative(){try{const cur=$('od_path')?.value||'';"
            "let j=await api('/api/folders/onedrive/pick',{method:'POST',body:JSON.stringify({initial:cur,title:'Configurar OneDrive — 06 Pasta da App'})});"
            "const p=j.onedrive_root||j.path||'';if(p)$('od_path').value=p}catch(e){toast(e.message,true)}}",
            1,
        )
    return html


_LOAD_PROJECT_JS = (
    "async function loadProject(){try{await ensureTaskLookups();const q=new URLSearchParams();"
    "q.set('projeto',$('pj_projeto')?.value||'Todos');q.set('level','task');"
    "let j=await api('/api/projects/summary?'+q);"
    "if($('pj_projeto')&&!$('pj_projeto').dataset.filled){fill('pj_projeto',j.projects||['Todos']);"
    "$('pj_projeto').dataset.filled='1';if(j.projeto)$('pj_projeto').value=j.projeto}"
    "const k=j.kpis||{};$('pj_total').textContent=k.total??0;$('pj_done').textContent=(k.done_pct??0)+'%';"
    "$('pj_overdue').textContent=k.overdue??0;$('pj_blocked').textContent=k.blocked??0;"
    "await projectGanttLoad();"
    "if(page==='project')$('upd').textContent='Última atualização: '+new Date().toLocaleTimeString('pt-PT')"
    "}catch(e){toast(e.message,true)}}"
)


def _patch_html_project_advanced(html: str) -> str:
    html = html.replace(
        '<select id="pj_projeto" onchange="loadProject()">',
        "<select id=\"pj_projeto\" onchange=\"if($('pj_milestone'))$('pj_milestone').value='Todos';loadProject()\">",
        1,
    )
    html = html.replace(
        '<div class="field"><label>Vista</label><select id="pj_level" onchange="loadProject()"><option value="task">Tarefa</option><option value="milestone">Milestone</option></select></div>',
        '<div class="field"><label>Milestone</label><select id="pj_milestone" onchange="loadProject()"><option>Todos</option></select></div>',
        1,
    )
    html = html.replace(
        '<h3 style="margin:0 0 10px">Por estado</h3><div id="pj_status_chart" style="min-height:220px;margin-bottom:16px"></div>',
        "",
        1,
    )
    html = re.sub(
        r'<section class="card" style="padding:16px;margin-top:14px">\s*<div[^>]*>\s*<h3[^>]*>Planeamento avançado \(Web\)</h3>.*?</section>',
        "",
        html,
        count=1,
        flags=re.S,
    )
    if 'id="pj-gantt-panel"' not in html:
        replaced = False
        for old in (
            '<div class="pj-gantt-head" style="display:flex;justify-content:space-between',
            '<h3 style="margin:0 0 10px">Gantt</h3><div id="pj_gantt" style="min-height:280px"></div>',
        ):
            if old in html:
                html = re.sub(
                    r'<div class="pj-gantt-head" style="display:flex;justify-content:space-between.*?</section>\s*(?=</section></div><div id="page-scheduled"|</section></div><div id="page-scheduled")',
                    _PROJECT_GANTT_HTML,
                    html,
                    count=1,
                    flags=re.S,
                )
                if 'id="pj-gantt-panel"' not in html:
                    html = html.replace(
                        '<h3 style="margin:0 0 10px">Gantt</h3><div id="pj_gantt" style="min-height:280px"></div>',
                        _PROJECT_GANTT_HTML,
                        1,
                    )
                replaced = True
                break
        if not replaced and 'id="pj-gantt-panel"' not in html:
            html = html.replace(
                '<h3 style="margin:0 0 10px">Gantt</h3><div id="pj_gantt" style="min-height:280px"></div></section></div><div id="page-scheduled" class="page">',
                _PROJECT_GANTT_HTML + '</section></div><div id="page-scheduled" class="page">',
                1,
            )
    if "#page-project #pj-gantt-panel{" not in html and "#page-project #pj_gantt{" in html:
        html = html.replace("</style>", _PROJECT_GANTT_CSS + "</style>", 1)
    elif "#page-project #pj-gantt-panel{" not in html:
        html = html.replace("</style>", _PROJECT_GANTT_CSS + "</style>", 1)
    if "function projectGanttToggleExpand()" not in html:
        html = re.sub(
            r"let _pjGanttObj=null,_pjGanttData=null.*?(?=async function loadProject\(\))",
            _PROJECT_GANTT_JS,
            html,
            count=1,
            flags=re.S,
        )
    if "function projectGanttLoad()" not in html:
        marker = "async function loadProject(){"
        if marker not in html:
            marker = "function loadProject(){"
        html = html.replace(marker, _PROJECT_GANTT_JS + marker, 1)
    html = re.sub(
        r"async function loadProject\(\)\{try\{.*?\}catch\(e\)\{toast\(e\.message,true\)\}\}",
        _LOAD_PROJECT_JS,
        html,
        count=1,
        flags=re.S,
    )
    return html


_NAV_MENU_INNER = (
    '<button type="button" data-page="myday" id="nav-myday" onclick="showPage(\'myday\')">'
    '<span>🎯</span><span class="txt">O Meu Dia</span></button>'
    '<button type="button" data-page="tasks" id="nav-tasks" onclick="showPage(\'tasks\')">'
    '<span>📋</span><span class="txt">Tarefas</span></button>'
    '<button type="button" data-page="scheduled" id="nav-scheduled" onclick="showPage(\'scheduled\')">'
    '<span>📅</span><span class="txt">Programadas</span></button>'
    '<button type="button" data-page="dashboard" id="nav-dashboard" onclick="showPage(\'dashboard\')">'
    '<span>📊</span><span class="txt">Dashboard</span></button>'
    '<button type="button" data-page="project" id="nav-project" onclick="showPage(\'project\')">'
    '<span>📁</span><span class="txt">Projeto</span></button>'
    '<button type="button" data-page="board" id="nav-board" onclick="showPage(\'board\')">'
    '<span>🗂</span><span class="txt">Board</span></button>'
    '<div class="nav-sep" aria-hidden="true"></div>'
    '<button type="button" data-page="shortcuts" id="nav-shortcuts" onclick="showPage(\'shortcuts\')">'
    '<span>⚡</span><span class="txt">Atalhos</span></button>'
    '<button type="button" data-page="notes" id="nav-notes" onclick="showPage(\'notes\')">'
    '<span>📝</span><span class="txt">Notas</span></button>'
    '<button type="button" data-page="contacts" id="nav-contacts" onclick="showPage(\'contacts\')">'
    '<span>👥</span><span class="txt">Contactos</span></button>'
    '<button type="button" data-page="machines" id="nav-machines" onclick="showPage(\'machines\')">'
    '<span>🏭</span><span class="txt">Máquinas</span></button>'
    '<button type="button" data-page="ach" id="nav-ach" onclick="showPage(\'ach\')">'
    '<span>🏆</span><span class="txt">Conquistas</span></button>'
    '<div class="nav-sep" aria-hidden="true"></div>'
    '<button type="button" data-page="admin" id="nav-admin" style="display:none" onclick="showPage(\'admin\')">'
    '<span>🛠</span><span class="txt">Admin</span></button>'
    '<button type="button" data-page="system" id="nav-system" onclick="showPage(\'system\')">'
    '<span>⚙</span><span class="txt">Sistema</span></button>'
)

_NAV_MENU_CSS = ".nav-sep{height:1px;margin:8px 12px;background:#ffffff22}"


def _patch_html_nav_order(html: str) -> str:
    if ".nav-sep{" not in html:
        html = html.replace("</style>", _NAV_MENU_CSS + "</style>", 1)
    start = html.find('<div class="nav">')
    end = html.find('<div class="u">')
    if start < 0 or end < 0 or end <= start:
        return html
    return html[:start] + f'<div class="nav">{_NAV_MENU_INNER}</div>' + html[end:]


def _patch_html_stability(html: str) -> str:
    if "/web/vendor/frappe-gantt/frappe-gantt.css" not in html:
        html = html.replace("</head>", _TASK_GANTT_ASSETS + "</head>", 1)
    if "#task_gantt_modal .modal" not in html:
        html = html.replace("</style>", _TASK_GANTT_CSS + "</style>", 1)
    if "#page-myday .md-grid{" not in html:
        html = html.replace("</style>", _MY_DAY_CSS + "</style>", 1)
    if ".diag-row{" not in html:
        html = html.replace("</style>", _STABILITY_CSS + "</style>", 1)
    if "function detailOpenTaskGantt()" not in html:
        html = html.replace("function fill(id,a){", _TASK_GANTT_JS + "function fill(id,a){", 1)
    if "function unsavedMarkDirty(" not in html:
        html = html.replace("function fill(id,a){", _UNSAVED_GUARD_JS + "function fill(id,a){", 1)
        html = html.replace("bindNav();", "bindNav();_unsavedInit();", 1)
    html = html.replace(
        "function toast(m,e=false){let t=$('toast');t.textContent=m;t.className='toast'+(e?' err':'');t.style.display='block';setTimeout(()=>t.style.display='none',3500)}",
        "function _friendlyErrMsg(msg,ctx=''){"
        "const raw=String(msg||'').trim();if(!raw)return 'Erro ao carregar dados.';"
        "const s=raw.toLowerCase();const c=String(ctx||'').toLowerCase();"
        "if(s.includes('failed to fetch')||s.includes('networkerror')||s.includes('load failed')||s.includes('fetch failed')||s.includes('typeerror')){"
        "if(c.includes('/api/auth/login')||c==='login')return 'Erro ao validar login.';"
        "if(c.includes('/api/auth'))return 'Não foi possível comunicar com o servidor.';"
        "return 'Não foi possível comunicar com o servidor.'}"
        "if(s.includes('cors')||s.includes('ecconn')||s.includes('timed out')||s.includes('timeout')||s.includes('connection refused'))return 'Não foi possível comunicar com o servidor.';"
        "if(s.includes('unexpected token')||s.includes('resposta inválida')||s.includes('invalid json'))return 'Erro ao carregar dados.';"
        "return raw}"
        "function toast(m,e=false){let t=$('toast');const out=e?_friendlyErrMsg(m):String(m||'');t.textContent=out;t.className='toast'+(e?' err':'');t.style.display='block';setTimeout(()=>t.style.display='none',3500)}",
        1,
    )
    html = html.replace(
        "async function api(u,o={}){let r=await fetch(u,{headers:{'Content-Type':'application/json'},...o});let j=await r.json().catch(()=>({ok:false,error:'Resposta inválida'}));if(!r.ok||j.ok===false){const e=new Error(j.error||'Erro');e.status=r.status;e.body=j;throw e}return j}",
        "async function api(u,o={}){let r;try{r=await fetch(u,{headers:{'Content-Type':'application/json'},...o})}catch(ex){throw new Error(_friendlyErrMsg(ex?.message||ex,u))}"
        "let j=await r.json().catch(()=>({ok:false,error:'Erro ao carregar dados.'}));"
        "if(!r.ok||j.ok===false){const e=new Error(_friendlyErrMsg(j.error||'Erro',u));e.status=r.status;e.body=j;throw e}return j}",
        1,
    )
    html = html.replace(
        "catch(e){loadingHide();showAuthModal(e.message||'Falha no login')}",
        "catch(e){loadingHide();showAuthModal(_friendlyErrMsg(e?.message||'Falha no login','login'))}",
        1,
    )
    html = html.replace(
        "function teBadge(e){const s=String(e||'');",
        "function teBadge(e){let s=String(e||'').trim();if(!s)s='Não iniciado';",
        1,
    )
    html = html.replace(
        "${teBadge(a.status||(a.done?'Concluído':''))}</td>`;tb.appendChild(tr)};",
        "${teBadge(a.status||(a.done?'Concluído':'Não iniciado'))}</td>`;tb.appendChild(tr)};",
        1,
    )
    html = html.replace(
        '<select id="db_mode" onchange="loadDashboard()"><option value="executivo">Executivo</option><option value="operacao">Operação</option><option value="analitico">Analítico</option></select>',
        '<select id="db_mode" onchange="loadDashboard()"><option value="executivo">Executivo</option><option value="operacao">Operação</option><option value="analitico">Analítico</option><option value="eficiencia">Eficiência</option></select>',
        1,
    )
    html = html.replace(
        "function renderDbCharts(data){const box=$('db_charts');if(!box)return;box.innerHTML='';ensurePlotly().then(()=>{(data.charts||[]).forEach((c,i)=>{const el=document.createElement('div');el.className='card db-chart';el.innerHTML=`<h3 style=\"margin:0 0 8px;font-size:14px\">${esc(c.title)}</h3><div id=\"dbc_plot_${i}\" style=\"height:260px\"></div>`;box.appendChild(el);const plotEl=el.querySelector('div');let trace,layout={margin:{t:10,r:16,b:40,l:48},paper_bgcolor:'#fff',plot_bgcolor:'#fafafa'};if(c.type==='pie')trace=[{type:'pie',labels:c.labels,values:c.values,hole:.45}];else if(c.type==='heatmap')trace=[{type:'heatmap',x:c.x,y:c.y,z:c.z,colorscale:'Blues'}];else if(c.type==='bar_h'){trace=[{type:'bar',orientation:'h',x:c.x,y:c.y,marker:{color:'#dc2626'}}];layout.margin.l=Math.max(160,(c.y||[]).reduce((m,s)=>Math.max(m,String(s).length*7),80))}else trace=[{type:'bar',x:c.x,y:c.y,marker:{color:'#0869d8'}}];Plotly.newPlot(plotEl,trace,layout,{responsive:true,displayModeBar:false})})}).catch(()=>{box.innerHTML='<p class=\"muted\">Gráficos indisponíveis</p>'})}",
        "function renderDbCharts(data){const box=$('db_charts');if(!box)return;box.innerHTML='';window._dbChartsData=(data&&data.charts)||[];"
        "if(!window.dbEnsureChartModal){window.dbEnsureChartModal=function(){if($('db_chart_modal'))return;"
        "document.body.insertAdjacentHTML('beforeend','<div class=\"modal-bg\" id=\"db_chart_modal\" style=\"display:none\"><div class=\"modal\"><div class=\"mh\"><h3 id=\"db_chart_modal_title\" style=\"margin:0\">Gráfico</h3><button class=\"btn\" onclick=\"closeDbChartModal()\">✕</button></div><div class=\"mc\"><div id=\"db_chart_modal_plot\"></div></div></div></div>')};"
        "window.closeDbChartModal=function(){if($('db_chart_modal'))$('db_chart_modal').style.display='none'};"
        "window.dbOpenChartLarge=function(idx){try{dbEnsureChartModal();const c=(window._dbChartsData||[])[idx];if(!c)return;const pe=$('db_chart_modal_plot');$('db_chart_modal_title').textContent=c.title||'Gráfico';$('db_chart_modal').style.display='flex';"
        "let trace,layout={margin:{t:10,r:18,b:52,l:72},paper_bgcolor:'#fff',plot_bgcolor:'#fafafa'};"
        "if(c.type==='pie')trace=[{type:'pie',labels:c.labels,values:c.values,hole:.45}];"
        "else if(c.type==='heatmap')trace=[{type:'heatmap',x:c.x,y:c.y,z:c.z,colorscale:'Blues'}];"
        "else if(c.type==='bar_h'){trace=[{type:'bar',orientation:'h',x:c.x,y:c.y,marker:{color:'#dc2626'}}];layout.margin.l=Math.max(220,(c.y||[]).reduce((m,s)=>Math.max(m,String(s).length*7),120))}"
        "else trace=[{type:'bar',x:c.x,y:c.y,marker:{color:'#0869d8'}}];"
        "Plotly.newPlot(pe,trace,layout,{responsive:true,displayModeBar:true})}catch(e){toast(e.message,true)}}}"
        "ensurePlotly().then(()=>{(window._dbChartsData||[]).forEach((c,i)=>{const el=document.createElement('div');el.className='card db-chart';"
        "el.innerHTML=`<div class=\"chart-head\"><h3>${esc(c.title)}</h3><button class=\"btn\" style=\"padding:4px 8px;font-size:12px\" onclick=\"dbOpenChartLarge(${i})\">Expandir</button></div><div id=\"dbc_plot_${i}\" style=\"height:260px\"></div>`;"
        "box.appendChild(el);const plotEl=el.querySelector('#dbc_plot_'+i);let trace,layout={margin:{t:10,r:16,b:40,l:48},paper_bgcolor:'#fff',plot_bgcolor:'#fafafa'};"
        "if(c.type==='pie')trace=[{type:'pie',labels:c.labels,values:c.values,hole:.45}];"
        "else if(c.type==='heatmap')trace=[{type:'heatmap',x:c.x,y:c.y,z:c.z,colorscale:'Blues'}];"
        "else if(c.type==='bar_h'){trace=[{type:'bar',orientation:'h',x:c.x,y:c.y,marker:{color:'#dc2626'}}];layout.margin.l=Math.max(160,(c.y||[]).reduce((m,s)=>Math.max(m,String(s).length*7),80))}"
        "else trace=[{type:'bar',x:c.x,y:c.y,marker:{color:'#0869d8'}}];"
        "Plotly.newPlot(plotEl,trace,layout,{responsive:true,displayModeBar:false})})}).catch(()=>{box.innerHTML='<p class=\"muted\">Gráficos indisponíveis</p>'})}",
        1,
    )
    return _patch_html_nav_order(html)


def _patch_html_achievements(html: str) -> str:
    html = html.replace(
        '<button class="btn" onclick="exportCsv()">Exportar CSV</button>',
        '<button class="btn" onclick="exportCsv()">Exportar CSV</button>'
        '<button class="btn" onclick="exportXlsx()">Exportar XLSX</button>',
        1,
    )
    html = html.replace(
        "function exportCsv(){location='/api/achievements/export.csv?'+qs()}",
        "function exportCsv(){location='/api/achievements/export.csv?'+qs()}"
        "function exportXlsx(){location='/api/achievements/export.xlsx?'+qs()}",
        1,
    )
    return html


def _patch_html_scheduled(html: str) -> str:
    if "#page-scheduled .sched-st{" not in html:
        html = html.replace("</style>", _SCHED_CSS + "</style>", 1)
    html = html.replace(
        '<button class="btn" onclick="schedRunDue()">Processar vencidas</button>',
        '<button class="btn primary" onclick="schedRunDue()" title="Processa automaticamente todas as programadas na janela de execução">Processar todas vencidas</button>',
        1,
    )
    html = html.replace(
        '<div class="kpi"><div class="ico">👥</div><div><div class="muted">Partilhadas</div><div class="v" id="sch_shared">0</div></div></div></section>',
        '<div class="kpi"><div class="ico">👥</div><div><div class="muted">Partilhadas</div><div class="v" id="sch_shared">0</div></div></div>'
        '<div class="kpi kpi-click" onclick="schedFilterFailed()" title="Filtrar programadas com falha">'
        '<div class="ico">⚠</div><div><div class="muted">Com falha</div><div class="v" id="sch_failed">0</div></div></div></section>',
        1,
    )
    html = html.replace(
        '<section class="card"><div class="table-wrap"><table><thead><tr><th>Nome</th>',
        _SCHED_FILTERS_HTML + _SCHED_TOOLBAR_HTML
        + '<section class="card"><div class="table-wrap"><table><thead><tr><th>Nome</th>',
        1,
    )
    html = html.replace(
        '<div class="toolbar"><button class="btn primary" id="sch_new" onclick="schedNew()">Nova</button>'
        '<button class="btn" id="sch_edit" onclick="schedEdit()" disabled>Editar</button>'
        '<button class="btn" id="sch_gen" onclick="schedGenerate()" disabled>Gerar agora</button>'
        '<button class="btn" id="sch_mat" onclick="schedMaterialize()" disabled>Materializar</button>'
        '<button class="btn" id="sch_toggle" onclick="schedToggleSel()" disabled>Ativar/Desativar</button>'
        '<button class="btn" id="sch_open_task" onclick="schedOpenTask()" disabled>Abrir tarefa</button></div>',
        "",
        1,
    )
    html = html.replace(
        '</div></div><div class="modal-bg" id="sched-modal" style="display:none">',
        '</div>' + _SCHED_LOGS_HTML + '</div><div class="modal-bg" id="sched-modal" style="display:none">',
        1,
    )
    html = html.replace(
        "if($('sm_gda'))$('sm_gda').checked=!!row.generate_default_actions;const acts=row.action_defaults||[];"
        "if($('sm_actions'))$('sm_actions').value=JSON.stringify(acts.length?acts:[{text:'Revisao',due_offset_days:0,owner:''}],null,0);schedRecUI()}",
        "if($('sm_gda'))$('sm_gda').checked=!!row.generate_default_actions;const acts=row.action_defaults||[];"
        "if(typeof schedLoadActionsForm==='function')schedLoadActionsForm(acts);"
        "else if($('sm_actions'))$('sm_actions').value=JSON.stringify(acts.length?acts:[{text:'Revisão',due_offset_days:0,owner:''}],null,0);"
        "schedRecUI();if($('sm_preview'))$('sm_preview').textContent='Configure a recorrência e use «Pré-visualizar».'}",
        1,
    )
    html = html.replace(
        "function schedNew(){if(!canEditTasks())return;_schedEditId=null;$('sched-modal-title').textContent='Nova programada';fillSchedModal({",
        "function schedNew(){if(!canEditTasks())return;_schedEditId=null;"
        "if($('sched-modal-title'))$('sched-modal-title').textContent='Nova programada';"
        "if($('sched-modal-sub'))$('sched-modal-sub').textContent='Defina recorrência e valores por omissão da tarefa';"
        "schedTab('rec');fillSchedModal({",
        1,
    )
    html = html.replace(
        "$('sched-modal-title').textContent='Editar programada';fillSchedModal(j.row||{});",
        "if($('sched-modal-title'))$('sched-modal-title').textContent='Editar programada';"
        "if($('sched-modal-sub'))$('sched-modal-sub').textContent='Alterar template #'+String(_schedSel||'');"
        "schedTab('rec');fillSchedModal(j.row||{});",
        1,
    )
    html = re.sub(
        r"async function loadScheduled\(\)\{try\{let j=await api\('/api/scheduled'\);.*?\}\s*catch\(e\)\{toast\(e.message,true\)\}\}",
        "async function loadScheduled(){try{let j=await api('/api/scheduled');"
        "_schedRowsAll=j.rows||[];if(!_schedFiltersInit){await schedRestoreFilters();schedBindFilters();_schedFiltersInit=true}"
        "const s=j.summary||{};$('sch_pending').textContent=s.pending??0;$('sch_active').textContent=s.active??0;"
        "$('sch_next7').textContent=s.next7??0;$('sch_shared').textContent=s.shared??0;"
        "if($('sch_failed'))$('sch_failed').textContent=s.failed??0;"
        "if(window._sched_open_pending){window._sched_open_pending=false;"
        "if($('sch_f_pending'))$('sch_f_pending').checked=true;if($('sch_f_active'))$('sch_f_active').checked=false;"
        "schedSaveFilters()}schedRefilter();loadScheduledLogs();"
        "if(page==='scheduled')$('upd').textContent='Última atualização: '+new Date().toLocaleTimeString('pt-PT')}"
        "catch(e){toast(e.message,true)}}",
        html,
        count=1,
    )
    html = re.sub(
        r"function renderScheduled\(\)\{const tb=\$\('sch_rows'\);if\(!tb\)return;tb\.innerHTML='';.*?"
        r"if\(\$\('sch_new'\)\)\$\('sch_new'\)\.style\.display=canEditTasks\(\)\?'inline-block':'none'\}",
        _SCHED_RENDER_JS.strip(),
        html,
        count=1,
    )
    html = html.replace(
        "let _schedEditId=null;function closeSchedModal(){if($('sched-modal'))$('sched-modal').style.display='none'}",
        "let _schedFiltersInit=false;" + _SCHED_FILTERS_JS + _SCHED_LOGS_JS + _SCHED_MODAL_JS +
        "let _schedEditId=null;function closeSchedModal(){if($('sched-modal'))$('sched-modal').style.display='none'}",
        1,
    )
    html = re.sub(
        r'<div class="modal-bg" id="sched-modal" style="display:none">.*?</div></div><div id="page-notes"',
        _SCHED_MODAL_HTML + '<div id="page-notes"',
        html,
        count=1,
        flags=re.S,
    )
    return html


def _q1(qs: dict[str, list[str]], key: str, default: str = "") -> str:
    vals = qs.get(key) or []
    if not vals:
        return default
    return str(vals[0] or default).strip()


def _board_truthy(v: Any) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _parse_board_filters(q: dict[str, list[str]]) -> dict[str, Any]:
    f: dict[str, Any] = {
        "estado": _q1(q, "estado", "Todos"),
        "prioridade": _q1(q, "prioridade", "Todas"),
        "projeto": _q1(q, "projeto", "Todos"),
        "responsavel": _q1(q, "responsavel", "Todos"),
        "q": _q1(q, "q", ""),
    }
    if _board_truthy(_q1(q, "only_mine")):
        f["only_mine"] = True
    if _board_truthy(_q1(q, "overdue_only")):
        f["overdue_only"] = True
    if _board_truthy(_q1(q, "blocked_only")):
        f["blocked_only"] = True
    if _board_truthy(_q1(q, "show_done")):
        f["show_done"] = True
    return f


def _board_prefs_normalize(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "estado": str(payload.get("estado") or "Todos"),
        "prioridade": str(payload.get("prioridade") or "Todas"),
        "responsavel": str(payload.get("responsavel") or "Todos"),
        "projeto": str(payload.get("projeto") or "Todos"),
        "q": str(payload.get("q") or "").strip(),
        "only_mine": bool(payload.get("only_mine")),
        "overdue_only": bool(payload.get("overdue_only")),
        "blocked_only": bool(payload.get("blocked_only")),
        "show_done": bool(payload.get("show_done")),
    }


def _to_iso_date(v: Any) -> str:
    s = str(v or "").strip()
    if not s:
        return ""
    s = s.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:26], fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return s[:10] if len(s) >= 10 else ""


def _achievement_impact_value(ar: dict[str, Any]) -> float:
    for key in (
        "financial_impact_eur",
        "impact_eur",
        "Impacto",
        "ImpactoEUR",
        "impact",
        "Valor",
        "ValorEUR",
        "ValorEconomico",
    ):
        raw = ar.get(key)
        if raw is None or raw == "":
            continue
        s = str(raw).strip().replace("€", "").replace(" ", "")
        if not s:
            continue
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "," in s:
            s = s.replace(".", "").replace(",", ".")
        try:
            return float(s)
        except Exception:
            continue
    return 0.0


def _tasks_extras_payload(st: Any, q: dict[str, list[str]], base_mod: Any) -> dict[str, Any]:
    display = getattr(st, "display_name", st.username) or st.username
    f = dict(getattr(base_mod, "task_filters", lambda _q: {})(q or {}) or {})
    rows = st.db.list_tasks(f, st.username, display, st.role) or []
    tids = {str(r.get("TaskID") or "").strip() for r in rows if isinstance(r, dict) and str(r.get("TaskID") or "").strip()}
    impact = 0.0
    linked = 0
    if tids and hasattr(st.db, "list"):
        try:
            for ar in st.db.list({}) or []:
                if not isinstance(ar, dict):
                    continue
                tid = str(ar.get("task_id") or ar.get("TaskID") or "").strip()
                if not tid or tid not in tids:
                    continue
                val = _achievement_impact_value(ar)
                if val:
                    impact += val
                    linked += 1
        except Exception:
            pass
    label = f"{linked} conquista(s) ligada(s)" if linked else "Sem conquistas ligadas"
    return {"impact_eur": round(impact, 2), "impact_label": label, "linked_achievements": linked}


def _dashboard_efficiency_charts(base_mod: Any, st: Any, q: dict[str, list[str]]) -> dict[str, Any]:
    filters = {
        "q": _q1(q, "q", ""),
        "estado": _q1(q, "estado", "Todos"),
        "prioridade": _q1(q, "prioridade", "Todos"),
        "responsavel": _q1(q, "responsavel", "Todos"),
        "projeto": _q1(q, "projeto", "Todos"),
    }
    only_open = _q1(q, "only_open", "") in ("1", "true", "yes", "on")
    display = getattr(st, "display_name", st.username) or st.username
    rows = st.db.list_tasks(filters, st.username, display, st.role) or []
    if only_open:
        rows = [r for r in rows if str(r.get("Estado") or "").strip().lower() not in ("concluído", "concluido")]
    by_tid: dict[str, dict[str, Any]] = {}
    for r in rows:
        tid = str(r.get("TaskID") or "").strip()
        if tid:
            by_tid[tid] = r
    tids = set(by_tid.keys())

    closed_at: dict[str, str] = {}
    try:
        with st.db.connect() as conn:
            cur = conn.execute("SELECT TOP 8000 ts, TaskID, event, details FROM task_history ORDER BY id DESC;")
            for ts, task_id, event, details in cur.fetchall():
                tid = str(task_id or "").strip()
                if not tid or (tids and tid not in tids):
                    continue
                blob = (str(event or "") + " " + str(details or "")).lower()
                if "conclu" not in blob:
                    continue
                d = _to_iso_date(ts)
                if not d:
                    continue
                prev = closed_at.get(tid, "")
                if not prev or d > prev:
                    closed_at[tid] = d
    except Exception:
        closed_at = {}

    if not closed_at:
        for tid, r in by_tid.items():
            if str(r.get("Estado") or "").strip().lower() in ("concluído", "concluido"):
                d = _to_iso_date(r.get("DataRegisto"))
                if d:
                    closed_at[tid] = d

    weeks: dict[str, int] = {}
    for d in closed_at.values():
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            y, w, _ = dt.isocalendar()
            k = f"{y}-W{int(w):02d}"
            weeks[k] = int(weeks.get(k, 0)) + 1
        except Exception:
            pass

    ordered = sorted(weeks.items())
    if len(ordered) > 12:
        ordered = ordered[-12:]
    x = [k for k, _v in ordered]
    y = [int(v) for _k, v in ordered]
    if not x:
        x = ["Sem dados"]
        y = [0]

    return {
        "charts": [
            {
                "type": "bar",
                "title": "Tarefas fechadas por semana (12 sem.)",
                "x": x,
                "y": y,
            }
        ]
    }


def _parse_cookies(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in str(raw or "").split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _session_cleanup() -> None:
    now = time.time()
    expired = [tok for tok, s in _SESSIONS.items() if (now - float(s.get("last_seen", 0))) > _SESSION_IDLE_SEC]
    for tok in expired:
        _SESSIONS.pop(tok, None)


def _session_get_from_headers(headers) -> tuple[str | None, dict[str, Any] | None]:
    _session_cleanup()
    raw = headers.get("Cookie") if headers else ""
    token = _parse_cookies(raw).get(_SESSION_COOKIE)
    if not token:
        return None, None
    sess = _SESSIONS.get(token)
    if not sess:
        return token, None
    sess["last_seen"] = time.time()
    return token, sess


def _session_create(username: str, display_name: str, role: str, machine: str = "") -> tuple[str, dict[str, Any]]:
    token = uuid.uuid4().hex
    now = time.time()
    sid = str(uuid.uuid4())
    sess = {
        "token": token,
        "session_id": sid,
        "username": str(username or "").strip(),
        "display_name": str(display_name or username or "").strip(),
        "role": _normalize_role(role),
        "machine": str(machine or "").strip(),
        "created_at": now,
        "last_seen": now,
        "last_db_touch": 0.0,
    }
    _SESSIONS[token] = sess
    return token, sess


def _normalize_role(role: str) -> str:
    r = str(role or "").strip().lower()
    aliases = {
        "editor": "edit",
        "escrita": "edit",
        "write": "edit",
        "leitura": "read",
        "viewer": "read",
        "owner": "admin",
        "administrator": "admin",
    }
    r = aliases.get(r, r)
    if r not in ("read", "edit", "admin"):
        return "read"
    return r


def _can_edit_role(role: str) -> bool:
    return _normalize_role(role) in ("edit", "admin")


def _session_destroy(token: str | None) -> None:
    if token:
        _SESSIONS.pop(token, None)


def _auth_origin_path(base_mod) -> Path:
    return base_mod.cache_dir() / "auth_origin.json"


def _auth_origin_read(base_mod) -> dict[str, Any]:
    try:
        p = _auth_origin_path(base_mod)
        if not p.is_file():
            return {}
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        out = {
            "machine": str(raw.get("machine") or "").strip(),
            "username": str(raw.get("username") or "").strip(),
            "source": str(raw.get("source") or "").strip(),
            "ts": str(raw.get("ts") or "").strip(),
        }
        return out
    except Exception:
        return {}


def _auth_origin_write(base_mod, machine: str, username: str = "", source: str = "") -> None:
    m = str(machine or "").strip()
    if not m:
        return
    try:
        p = _auth_origin_path(base_mod)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "machine": m,
            "username": str(username or "").strip(),
            "source": str(source or "").strip(),
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _auth_normalize_login_username(username: str) -> str:
    uname = str(username or "").strip()
    if "\\" in uname:
        uname = uname.split("\\")[-1].strip()
    if "@" in uname:
        uname = uname.split("@")[0].strip()
    return uname


def _auth_resolve_user(db, username: str, role_hint: str = "read", require_exists: bool = False) -> dict[str, Any]:
    _auth_users_ensure_schema(db)
    uname = _auth_normalize_login_username(username)
    if not uname:
        raise ValueError("username em falta")
    role_hint = _normalize_role(role_hint)
    raw = str(username or "").strip()
    candidates: list[str] = []
    for v in (uname, raw, uname.lower(), raw.lower()):
        v = str(v or "").strip()
        if v and v not in candidates:
            candidates.append(v)
    with db.connect() as conn:
        cur = conn.cursor()
        for cand in candidates:
            try:
                cur.execute(
                    """
                    SELECT TOP 1 username, display_name, role, active
                    FROM dbo.users
                    WHERE is_deleted = 0 AND (
                        username = ? OR display_name = ? OR windows_account = ?
                    )
                    ORDER BY CASE WHEN username = ? THEN 0 WHEN windows_account = ? THEN 1 ELSE 2 END;
                    """,
                    (cand, cand, cand, cand, cand),
                )
            except Exception:
                cur.execute(
                    """
                    SELECT TOP 1 username, display_name, role, active
                    FROM dbo.users
                    WHERE (username = ? OR display_name = ?) AND is_deleted = 0
                    ORDER BY CASE WHEN username = ? THEN 0 ELSE 1 END;
                    """,
                    (cand, cand, cand),
                )
            row = cur.fetchone()
            if row:
                return {
                    "username": str(row[0] or uname),
                    "display_name": str(row[1] or row[0] or uname),
                    "role": _normalize_role(str(row[2] or "read")),
                    "active": bool(row[3]),
                    "from_db": True,
                }
    out = {
        "username": uname,
        "display_name": uname,
        "role": role_hint,
        "active": True,
        "from_db": False,
    }
    if require_exists:
        raise ValueError("utilizador não encontrado na tabela users")
    return out


def _auth_machine_key(machine: str) -> str:
    return str(machine or "").strip().upper()


def _auth_machine_binding(db, machine: str) -> dict[str, Any] | None:
    m = _auth_machine_key(machine)
    if not m:
        return None
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT TOP 1 b.machine, b.username, b.active, u.display_name, u.role, u.active
            FROM dbo.device_bindings b
            LEFT JOIN dbo.users u ON u.username = b.username
            WHERE b.machine = ? AND ISNULL(b.is_deleted,0)=0
            ORDER BY ISNULL(b.updated_at,b.created_at) DESC;
            """,
            (m,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "machine": str(row[0] or ""),
        "username": str(row[1] or ""),
        "binding_active": bool(row[2]),
        "display_name": str(row[3] or row[1] or ""),
        "role": _normalize_role(str(row[4] or "read")),
        "user_active": bool(row[5]) if row[5] is not None else True,
    }


def _check_admin_password(cfg: dict, password: str) -> bool:
    sha = str((cfg or {}).get("admin_password_sha256") or "").strip().lower()
    if not sha:
        return False
    got = hashlib.sha256(str(password or "").encode("utf-8")).hexdigest().lower()
    return got == sha


def _session_db_start(db, session_id: str, username: str, machine: str) -> None:
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with db.connect() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO dbo.sessions (session_id, username, machine, started_at, last_seen, ended_at) VALUES (?,?,?,?,?,NULL);",
                    (session_id, username, machine, now, now),
                )
            except Exception:
                cur.execute(
                    "INSERT INTO dbo.sessions (session_id, [user], machine, started_at, last_seen, ended_at) VALUES (?,?,?,?,?,NULL);",
                    (session_id, username, machine, now, now),
                )
            conn.commit()
    except Exception:
        pass


def _session_db_touch(db, session_id: str) -> None:
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with db.connect() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE dbo.sessions SET last_seen=? WHERE session_id=?;", (now, session_id))
            conn.commit()
    except Exception:
        pass


def _session_db_end(db, session_id: str) -> None:
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with db.connect() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE dbo.sessions SET ended_at=?, last_seen=? WHERE session_id=?;", (now, now, session_id))
            conn.commit()
    except Exception:
        pass


def _is_admin(role: str) -> bool:
    return _normalize_role(role) == "admin"


_LIST_TYPES_ALLOWED = ["estados", "prioridades", "projects", "lines", "machines", "milestones", "assuntos", "pessoal"]


def _project_cache_config_path() -> Path:
    return _BASE_DIR / "AppEngenhariaCache" / "config.json"


def _project_cache_config_read() -> dict[str, Any]:
    p = _project_cache_config_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _project_cache_config_write(patch: dict[str, Any]) -> None:
    p = _project_cache_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    cur = _project_cache_config_read()
    cur.update(dict(patch or {}))
    p.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")


def _admin_lists_get(db) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {k: [] for k in _LIST_TYPES_ALLOWED}
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT tipo, valor FROM dbo.app_lists ORDER BY tipo, valor;")
        for tipo, valor in cur.fetchall() or []:
            t = str(tipo or "").strip()
            v = str(valor or "").strip()
            if t in out and v:
                out[t].append(v)
    return out


def _admin_lists_save(db, lists_patch: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, list[str]] = {}
    for k, vals in dict(lists_patch or {}).items():
        key = str(k or "").strip()
        if key not in _LIST_TYPES_ALLOWED:
            continue
        seen: set[str] = set()
        arr: list[str] = []
        for v in list(vals or []):
            s = str(v or "").strip()
            if not s:
                continue
            sk = s.lower()
            if sk in seen:
                continue
            seen.add(sk)
            arr.append(s)
        clean[key] = arr
    if not clean:
        raise ValueError("lists em falta")
    with db.connect() as conn:
        cur = conn.cursor()
        for tipo, vals in clean.items():
            cur.execute("DELETE FROM dbo.app_lists WHERE tipo = ?;", (tipo,))
            for v in vals:
                cur.execute("INSERT INTO dbo.app_lists (tipo, valor) VALUES (?, ?);", (tipo, v))
        conn.commit()
    return {"ok": True, "updated_types": list(clean.keys())}


def _admin_settings_get(base_mod: Any, db_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(base_mod.load_config() or {})
    if isinstance(db_cfg, dict):
        cfg.update(db_cfg)
    cache_cfg = _project_cache_config_read()
    cfg.update(cache_cfg)
    return {
        "emoji_bloqueado": str(cfg.get("emoji_bloqueado") or "🚫"),
        "emoji_new": str(cfg.get("emoji_new") or "🆕"),
        "emoji_atraso": str(cfg.get("emoji_atraso") or "⏰"),
        "admin_password_set": bool(str(cfg.get("admin_password_sha256") or "").strip()),
    }


def _admin_settings_set_password(password: str) -> dict[str, Any]:
    pwd = str(password or "")
    if len(pwd) < 4:
        raise ValueError("password deve ter pelo menos 4 caracteres")
    sha = hashlib.sha256(pwd.encode("utf-8")).hexdigest().lower()
    _project_cache_config_write({"admin_password_sha256": sha})
    return {"ok": True}


def _admin_settings_set_emojis(payload: dict[str, Any]) -> dict[str, Any]:
    emo = {
        "emoji_bloqueado": str(payload.get("emoji_bloqueado") or "🚫").strip() or "🚫",
        "emoji_new": str(payload.get("emoji_new") or "🆕").strip() or "🆕",
        "emoji_atraso": str(payload.get("emoji_atraso") or "⏰").strip() or "⏰",
    }
    _project_cache_config_write(emo)
    return {"ok": True, **emo}


def _safe_scalar(conn, sql: str, params: tuple | list = ()) -> Any:
    try:
        cur = conn.cursor()
        cur.execute(sql, tuple(params or ()))
        row = cur.fetchone()
        return None if row is None else row[0]
    except Exception:
        return None


def _checklist_has_action_meta(
    owner: Any = "",
    workers: Any = "",
    start_date: Any = "",
    due_date: Any = "",
    status: Any = "",
    evidence: Any = "",
    blocked_reason: Any = "",
) -> bool:
    """Metadados reais de ação — ignora status sintético ('Não iniciado') em checks."""
    if any(str(x or "").strip() for x in (owner, workers, start_date, due_date, evidence, blocked_reason)):
        return True
    st = str(status or "").strip().lower()
    return bool(st and st not in ("não iniciado", "nao iniciado", "concluído", "concluido"))


def _task_detail_items_sql(db, task_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tid = str(task_id or "").strip()
    if not tid:
        return [], []
    rows: list[Any] = []
    with db.connect() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                IF COL_LENGTH('dbo.task_checklist','action_notes') IS NULL
                    ALTER TABLE dbo.task_checklist ADD action_notes NVARCHAR(MAX) NULL;
                """
            )
            conn.commit()
        except Exception:
            try:
                cur.execute("ALTER TABLE dbo.task_checklist ADD action_notes NVARCHAR(MAX) NULL;")
                conn.commit()
            except Exception:
                pass
        sql_core = (
            "SELECT id, COALESCE(item_text,''), COALESCE(done,0), COALESCE([ord],0), COALESCE(kind,'CHECK'), "
            "COALESCE(owner,''), COALESCE(workers,''), COALESCE(start_date,''), COALESCE(due_date,''), "
            "COALESCE(status,''), COALESCE(evidence,''), COALESCE(blocked_reason,''), COALESCE(item_uuid,''), "
            "COALESCE(action_notes,'') FROM dbo.task_checklist WHERE TaskID=? "
        )
        variants = [
            sql_core + "AND ISNULL(is_deleted,0)=0 AND COALESCE(deleted_at,'')='' ORDER BY [ord], id;",
            sql_core + "AND ISNULL(is_deleted,0)=0 ORDER BY [ord], id;",
            sql_core + "ORDER BY [ord], id;",
            (
                "SELECT id, COALESCE(item_text,''), COALESCE(done,0), COALESCE([ord],0), COALESCE(kind,'CHECK'), "
                "COALESCE(owner,''), COALESCE(workers,''), COALESCE(start_date,''), COALESCE(due_date,''), "
                "COALESCE(status,''), COALESCE(evidence,''), COALESCE(blocked_reason,''), COALESCE(item_uuid,''), "
                "N'' FROM dbo.task_checklist WHERE TaskID=? AND ISNULL(is_deleted,0)=0 ORDER BY [ord], id;"
            ),
            (
                "SELECT id, COALESCE(item_text,''), COALESCE(done,0), COALESCE([ord],0), COALESCE(kind,'CHECK'), "
                "COALESCE(owner,''), COALESCE(workers,''), COALESCE(start_date,''), COALESCE(due_date,''), "
                "COALESCE(status,''), COALESCE(evidence,''), COALESCE(blocked_reason,''), COALESCE(item_uuid,''), "
                "N'' FROM dbo.task_checklist WHERE TaskID=? ORDER BY [ord], id;"
            ),
        ]
        for sql in variants:
            try:
                cur.execute(sql, (tid,))
                rows = cur.fetchall() or []
                break
            except Exception:
                continue

    actions: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    for r in rows:
        status = str(r[9] or "").strip()
        done = bool(r[2]) or status.lower() in ("concluído", "concluido")
        kind = str(r[4] or "CHECK").strip().upper()
        has_action_meta = _checklist_has_action_meta(r[5], r[6], r[7], r[8], r[9], r[10], r[11])
        if kind not in ("ACTION", "CHECK"):
            kind = "ACTION" if has_action_meta else "CHECK"
        elif kind == "CHECK" and has_action_meta:
            # Paridade Desktop: item com campos de ação deve ser tratado como ACTION.
            kind = "ACTION"
        if kind == "CHECK":
            item_status = status or ("Concluído" if done else "")
        else:
            item_status = status or ("Concluído" if done else "Não iniciado")
        item = {
            "id": int(r[0] or 0),
            "item_text": str(r[1] or "").strip(),
            "done": done,
            "is_done": done,
            "ord": int(r[3] or 0),
            "kind": kind,
            "owner": str(r[5] or "").strip(),
            "workers": str(r[6] or "").strip(),
            "start_date": str(r[7] or "").strip()[:10],
            "due_date": str(r[8] or "").strip()[:10],
            "status": item_status,
            "evidence": str(r[10] or "").strip(),
            "blocked_reason": str(r[11] or "").strip(),
            "item_uuid": str(r[12] or "").strip(),
            "action_notes": str(r[13] or "").strip(),
        }
        if kind == "ACTION":
            actions.append(item)
        else:
            checks.append(item)
    return actions, checks


def _batch_checklist_by_tasks(db, task_ids: list[str]) -> dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
    ids = [str(x or "").strip() for x in (task_ids or []) if str(x or "").strip()]
    uniq = list(dict.fromkeys(ids))
    out: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {tid: ([], []) for tid in uniq}
    if not uniq:
        return out
    marks = ",".join(["?"] * len(uniq))
    rows: list[Any] = []
    with db.connect() as conn:
        cur = conn.cursor()
        sql_core = (
            "SELECT TaskID, id, COALESCE(item_text,''), COALESCE(done,0), COALESCE([ord],0), COALESCE(kind,'CHECK'), "
            "COALESCE(owner,''), COALESCE(workers,''), COALESCE(start_date,''), COALESCE(due_date,''), "
            "COALESCE(status,''), COALESCE(evidence,''), COALESCE(blocked_reason,''), COALESCE(item_uuid,''), "
            "COALESCE(action_notes,'') FROM dbo.task_checklist WHERE TaskID IN ("
            + marks
            + ") "
        )
        variants = [
            sql_core + "AND ISNULL(is_deleted,0)=0 AND COALESCE(deleted_at,'')='' ORDER BY TaskID, [ord], id;",
            sql_core + "AND ISNULL(is_deleted,0)=0 ORDER BY TaskID, [ord], id;",
            sql_core + "ORDER BY TaskID, [ord], id;",
        ]
        for sql in variants:
            try:
                cur.execute(sql, tuple(uniq))
                rows = cur.fetchall() or []
                break
            except Exception:
                continue
    for r in rows:
        tid = str(r[0] or "").strip()
        if tid not in out:
            continue
        status = str(r[10] or "").strip()
        done = bool(r[3]) or status.lower() in ("concluído", "concluido")
        kind = str(r[5] or "CHECK").strip().upper()
        has_action_meta = _checklist_has_action_meta(r[6], r[7], r[8], r[9], r[10], r[11], r[12])
        if kind not in ("ACTION", "CHECK"):
            kind = "ACTION" if has_action_meta else "CHECK"
        elif kind == "CHECK" and has_action_meta:
            kind = "ACTION"
        if kind == "CHECK":
            item_status = status or ("Concluído" if done else "")
        else:
            item_status = status or ("Concluído" if done else "Não iniciado")
        item = {
            "id": int(r[1] or 0),
            "item_text": str(r[2] or "").strip(),
            "done": done,
            "is_done": done,
            "ord": int(r[4] or 0),
            "kind": kind,
            "owner": str(r[6] or "").strip(),
            "workers": str(r[7] or "").strip(),
            "start_date": str(r[8] or "").strip()[:10],
            "due_date": str(r[9] or "").strip()[:10],
            "status": item_status,
            "evidence": str(r[11] or "").strip(),
            "blocked_reason": str(r[12] or "").strip(),
            "item_uuid": str(r[13] or "").strip(),
            "action_notes": str(r[14] or "").strip(),
        }
        actions, checks = out[tid]
        if kind == "ACTION":
            actions.append(item)
        else:
            checks.append(item)
    return out


def _action_deps_get_sql(db, action_id: int) -> list[dict[str, Any]]:
    aid = int(action_id or 0)
    if aid <= 0:
        return []
    out: list[dict[str, Any]] = []
    with db.connect() as conn:
        cur = conn.cursor()
        tried = [
            """
            SELECT depends_on, COALESCE(dep_type,'FS'), COALESCE(lag_days,0)
            FROM dbo.action_dependencies
            WHERE action_id=? AND ISNULL(is_deleted,0)=0
            ORDER BY depends_on;
            """,
            """
            SELECT depends_on, COALESCE(dep_type,'FS'), COALESCE(lag_days,0)
            FROM dbo.action_dependencies
            WHERE action_id=?
            ORDER BY depends_on;
            """,
        ]
        rows: list[Any] = []
        for sql in tried:
            try:
                cur.execute(sql, (aid,))
                rows = cur.fetchall() or []
                break
            except Exception:
                continue
        for r in rows:
            dep = int(r[0] or 0)
            if dep <= 0:
                continue
            dep_type = str(r[1] or "FS").strip().upper()
            if dep_type not in ("FS", "SS", "FF", "SF"):
                dep_type = "FS"
            try:
                lag = int(r[2] or 0)
            except Exception:
                lag = 0
            out.append({"depends_on": dep, "dep_type": dep_type, "lag_days": lag})
    return out


def _action_deps_set_sql(db, action_id: int, deps: list[dict[str, Any]]) -> dict[str, Any]:
    aid = int(action_id or 0)
    if aid <= 0:
        raise ValueError("Ação inválida")
    norm: list[tuple[int, str, int]] = []
    seen: set[tuple[int, str, int]] = set()
    for d in deps or []:
        try:
            dep = int((d or {}).get("depends_on") or 0)
        except Exception:
            dep = 0
        if dep <= 0 or dep == aid:
            continue
        dep_type = str((d or {}).get("dep_type") or "FS").strip().upper()
        if dep_type not in ("FS", "SS", "FF", "SF"):
            dep_type = "FS"
        try:
            lag = int((d or {}).get("lag_days") or 0)
        except Exception:
            lag = 0
        lag = max(-30, min(365, lag))
        key = (dep, dep_type, lag)
        if key in seen:
            continue
        seen.add(key)
        norm.append(key)
    with db.connect() as conn:
        cur = conn.cursor()
        task_id = ""
        try:
            cur.execute("SELECT TOP 1 TaskID FROM dbo.task_checklist WHERE id=?;", (aid,))
            row_tid = cur.fetchone()
            task_id = str((row_tid[0] if row_tid else "") or "").strip()
        except Exception:
            task_id = ""
        if task_id:
            # Validar dependências apenas entre ações da mesma tarefa.
            try:
                cur.execute(
                    "SELECT id FROM dbo.task_checklist WHERE TaskID=? AND COALESCE(kind,'CHECK')='ACTION' AND ISNULL(is_deleted,0)=0;",
                    (task_id,),
                )
            except Exception:
                cur.execute(
                    "SELECT id FROM dbo.task_checklist WHERE TaskID=? AND COALESCE(kind,'CHECK')='ACTION';",
                    (task_id,),
                )
            action_ids = {int(r[0] or 0) for r in (cur.fetchall() or []) if int(r[0] or 0) > 0}
            norm = [x for x in norm if x[0] in action_ids]
            # Ciclos: substituir arestas do action_id e validar grafo resultante.
            adj: dict[int, set[int]] = {a: set() for a in action_ids}
            if action_ids:
                marks = ",".join(["?"] * len(action_ids))
                params = tuple(action_ids)
                try:
                    cur.execute(
                        f"SELECT action_id, depends_on FROM dbo.action_dependencies WHERE ISNULL(is_deleted,0)=0 AND action_id IN ({marks});",
                        params,
                    )
                except Exception:
                    cur.execute(
                        f"SELECT action_id, depends_on FROM dbo.action_dependencies WHERE action_id IN ({marks});",
                        params,
                    )
                for succ, pred in cur.fetchall() or []:
                    s = int(succ or 0)
                    p = int(pred or 0)
                    if s in action_ids and p in action_ids and s != aid:
                        adj.setdefault(s, set()).add(p)
                adj[aid] = {int(dep or 0) for dep, _dt, _lag in norm if int(dep or 0) in action_ids}

                def _has_cycle() -> bool:
                    seen: set[int] = set()
                    stack: set[int] = set()

                    def _dfs(n: int) -> bool:
                        if n in stack:
                            return True
                        if n in seen:
                            return False
                        seen.add(n)
                        stack.add(n)
                        for nxt in adj.get(n, set()):
                            if _dfs(int(nxt)):
                                return True
                        stack.remove(n)
                        return False

                    for node in adj.keys():
                        if _dfs(int(node)):
                            return True
                    return False

                if _has_cycle():
                    raise ValueError("Ciclo de dependências detetado")
        try:
            cur.execute("DELETE FROM dbo.action_dependencies WHERE action_id=?;", (aid,))
        except Exception:
            cur.execute("DELETE FROM action_dependencies WHERE action_id=?;", (aid,))
        for dep, dep_type, lag in norm:
            try:
                cur.execute(
                    "INSERT INTO dbo.action_dependencies(action_id, depends_on, dep_type, lag_days) VALUES (?,?,?,?);",
                    (aid, dep, dep_type, lag),
                )
            except Exception:
                cur.execute(
                    "INSERT INTO action_dependencies(action_id, depends_on, dep_type, lag_days) VALUES (?,?,?,?);",
                    (aid, dep, dep_type, lag),
                )
        conn.commit()
    return {"ok": True, "action_id": aid, "count": len(norm), "deps": [{"depends_on": d, "dep_type": t, "lag_days": l} for d, t, l in norm]}


def _admin_overview(db, base_mod, username: str, role: str) -> dict:
    if not _is_admin(role):
        raise PermissionError("Apenas admin")
    counts = {
        "tasks": None,
        "archives": None,
        "sessions": None,
        "users": None,
        "device_bindings": None,
    }
    tables_top: list[dict[str, Any]] = []
    with db.connect() as conn:
        counts["tasks"] = _safe_scalar(conn, "SELECT COUNT(*) FROM dbo.tasks;")
        counts["archives"] = _safe_scalar(conn, "SELECT COUNT(*) FROM dbo.archived_tasks;")
        counts["sessions"] = _safe_scalar(conn, "SELECT COUNT(*) FROM dbo.sessions;")
        counts["users"] = _safe_scalar(conn, "SELECT COUNT(*) FROM dbo.users;")
        counts["device_bindings"] = _safe_scalar(conn, "SELECT COUNT(*) FROM dbo.device_bindings;")
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT TOP 8 t.name, SUM(p.rows) AS row_count
                FROM sys.tables t
                JOIN sys.partitions p ON t.object_id = p.object_id
                WHERE p.index_id IN (0,1)
                GROUP BY t.name
                ORDER BY row_count DESC;
                """
            )
            for n, rc in cur.fetchall() or []:
                tables_top.append({"table": str(n or ""), "rows": int(rc or 0)})
        except Exception:
            tables_top = []
    log_path = base_mod.cache_dir() / "web_ui_local.log"
    log_size_mb = round((log_path.stat().st_size / (1024 * 1024)), 2) if log_path.exists() else 0
    return {
        "user": username,
        "role": role,
        "counts": counts,
        "tables_top": tables_top,
        "log": {"path": str(log_path), "size_mb": log_size_mb},
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _admin_sessions(db, limit: int = 120) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with db.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT TOP (?) session_id, username, machine, started_at, last_seen, ended_at
                FROM dbo.sessions
                ORDER BY started_at DESC;
                """,
                (int(limit),),
            )
            for sid, user, machine, started, last_seen, ended in cur.fetchall() or []:
                out.append(
                    {
                        "session_id": str(sid or ""),
                        "username": str(user or ""),
                        "machine": str(machine or ""),
                        "started_at": str(started or ""),
                        "last_seen": str(last_seen or ""),
                        "ended_at": str(ended or ""),
                    }
                )
    except Exception:
        return []
    return out


def _tail_log_lines(path: Path, lines: int = 160) -> list[str]:
    if not path.exists():
        return []
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return data[-max(1, int(lines)) :]
    except Exception:
        return []


def _auth_users_validate_username(username: str) -> str:
    uname = str(username or "").strip()
    if not uname:
        raise ValueError("username em falta")
    if len(uname) > 64:
        raise ValueError("username demasiado longo")
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", uname):
        raise ValueError("username inválido (use letras, números, . _ -)")
    return uname


def _auth_users_validate_role(role: str) -> str:
    role = str(role or "read").strip().lower()
    if role not in ("read", "edit", "admin"):
        raise ValueError("role inválida")
    return role


def _auth_users_ensure_schema(db) -> None:
    specs = (
        ("windows_account", "NVARCHAR(128) NULL"),
        ("primary_machine", "NVARCHAR(128) NULL"),
    )
    try:
        with db.connect() as conn:
            cur = conn.cursor()
            for col, typedef in specs:
                cur.execute(
                    f"""
                    IF COL_LENGTH('dbo.users', '{col}') IS NULL
                    ALTER TABLE dbo.users ADD [{col}] {typedef};
                    """
                )
            conn.commit()
    except Exception:
        pass


def _auth_validate_windows_account(windows_account: str) -> str:
    wa = _auth_normalize_login_username(windows_account)
    if not wa:
        return ""
    if len(wa) > 64:
        raise ValueError("conta Windows demasiado longa")
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", wa):
        raise ValueError("conta Windows inválida (use letras, números, . _ -)")
    return wa


def _auth_users_check_windows_unique(db, windows_account: str, exclude_username: str = "") -> None:
    wa = _auth_validate_windows_account(windows_account)
    if not wa:
        return
    ex = str(exclude_username or "").strip()
    with db.connect() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT TOP 1 username
                FROM dbo.users
                WHERE is_deleted = 0 AND windows_account = ? AND username <> ?;
                """,
                (wa, ex),
            )
        except Exception:
            return
        row = cur.fetchone()
        if row:
            raise ValueError(f"conta Windows já associada ao utilizador {row[0]}")


def _auth_users_sync_primary_machine(
    db,
    username: str,
    primary_machine: str,
    active: bool,
    actor: str,
) -> None:
    m = str(primary_machine or "").strip()
    if not m:
        return
    try:
        _auth_bindings_create(db, m, username, active, actor)
    except ValueError as ex:
        if "binding já existe" in str(ex).lower():
            _auth_bindings_update(db, m, username, active, actor)
        else:
            raise


def _auth_users_list(db, limit: int = 500) -> list[dict[str, Any]]:
    _auth_users_ensure_schema(db)
    out: list[dict[str, Any]] = []
    machines_map: dict[str, list[str]] = {}
    try:
        for row in _auth_bindings_list(db, limit):
            u = str(row.get("username") or "")
            m = str(row.get("machine") or "")
            if u and m:
                machines_map.setdefault(u, [])
                if m not in machines_map[u]:
                    machines_map[u].append(m)
    except Exception:
        machines_map = {}
    with db.connect() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT TOP (?) username, display_name, role, active, windows_account, primary_machine
                FROM dbo.users
                WHERE is_deleted = 0
                ORDER BY username ASC;
                """,
                (int(limit),),
            )
            rows = cur.fetchall() or []
            extended = True
        except Exception:
            cur.execute(
                """
                SELECT TOP (?) username, display_name, role, active
                FROM dbo.users
                WHERE is_deleted = 0
                ORDER BY username ASC;
                """,
                (int(limit),),
            )
            rows = cur.fetchall() or []
            extended = False
        for row in rows:
            if extended:
                u, dn, role, active, wa, pm = row
            else:
                u, dn, role, active = row
                wa, pm = "", ""
            uname = str(u or "")
            out.append(
                {
                    "username": uname,
                    "display_name": str(dn or ""),
                    "role": str(role or "read").strip().lower(),
                    "active": bool(active),
                    "windows_account": str(wa or ""),
                    "primary_machine": str(pm or ""),
                    "machines": list(machines_map.get(uname, [])),
                }
            )
    return out


def _auth_users_set_role(db, username: str, role: str, active: bool) -> None:
    _auth_users_update(db, username, display_name=None, role=role, active=active, updated_by=username)


def _auth_users_update(
    db,
    username: str,
    display_name: str | None,
    role: str | None,
    active: bool | None,
    updated_by: str,
    windows_account: str | None = None,
    primary_machine: str | None = None,
) -> None:
    _auth_users_ensure_schema(db)
    uname = _auth_users_validate_username(username)
    actor = str(updated_by or uname).strip() or uname
    fields: list[str] = []
    params: list[Any] = []
    sync_machine = None
    sync_active = True
    if display_name is not None:
        dn = str(display_name or "").strip() or uname
        fields.append("display_name = ?")
        params.append(dn)
    if role is not None:
        fields.append("role = ?")
        params.append(_auth_users_validate_role(role))
    if active is not None:
        fields.append("active = ?")
        params.append(1 if active else 0)
        sync_active = bool(active)
    if windows_account is not None:
        wa = _auth_validate_windows_account(windows_account)
        _auth_users_check_windows_unique(db, wa, exclude_username=uname)
        fields.append("windows_account = ?")
        params.append(wa or None)
    if primary_machine is not None:
        pm = _auth_bindings_validate_machine(primary_machine) if str(primary_machine or "").strip() else ""
        fields.append("primary_machine = ?")
        params.append(pm or None)
        sync_machine = pm
    if not fields:
        raise ValueError("nada para atualizar")
    fields += ["updated_at = SYSUTCDATETIME()", "updated_by = ?"]
    params.append(actor)
    params.append(uname)
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            UPDATE dbo.users
            SET {", ".join(fields)}
            WHERE username = ? AND is_deleted = 0;
            """,
            tuple(params),
        )
        if int(cur.rowcount or 0) <= 0:
            raise ValueError("utilizador não encontrado")
        conn.commit()
    if sync_machine:
        _auth_users_sync_primary_machine(db, uname, sync_machine, sync_active, actor)


def _auth_users_create(
    db,
    username: str,
    display_name: str,
    role: str,
    active: bool,
    created_by: str,
    windows_account: str = "",
    primary_machine: str = "",
) -> dict[str, Any]:
    _auth_users_ensure_schema(db)
    uname = _auth_users_validate_username(username)
    role = _auth_users_validate_role(role)
    dn = str(display_name or "").strip() or uname
    wa = _auth_validate_windows_account(windows_account)
    pm = _auth_bindings_validate_machine(primary_machine) if str(primary_machine or "").strip() else ""
    _auth_users_check_windows_unique(db, wa, exclude_username=uname)
    actor = str(created_by or uname).strip() or uname
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT TOP 1 username, COALESCE(is_deleted, 0)
            FROM dbo.users
            WHERE username = ?;
            """,
            (uname,),
        )
        row = cur.fetchone()
        if row:
            if int(row[1] or 0) == 0:
                raise ValueError("utilizador já existe")
            try:
                cur.execute(
                    """
                    UPDATE dbo.users
                    SET display_name = ?, role = ?, active = ?, windows_account = ?, primary_machine = ?,
                        is_deleted = 0, updated_at = SYSUTCDATETIME(), updated_by = ?
                    WHERE username = ?;
                    """,
                    (dn, role, 1 if active else 0, wa or None, pm or None, actor, uname),
                )
            except Exception:
                cur.execute(
                    """
                    UPDATE dbo.users
                    SET display_name = ?, role = ?, active = ?, is_deleted = 0,
                        updated_at = SYSUTCDATETIME(), updated_by = ?
                    WHERE username = ?;
                    """,
                    (dn, role, 1 if active else 0, actor, uname),
                )
        else:
            try:
                cur.execute(
                    """
                    INSERT INTO dbo.users(username, display_name, role, active, windows_account, primary_machine, is_deleted, created_at, updated_at, updated_by)
                    VALUES(?, ?, ?, ?, ?, ?, 0, SYSUTCDATETIME(), SYSUTCDATETIME(), ?);
                    """,
                    (uname, dn, role, 1 if active else 0, wa or None, pm or None, actor),
                )
            except Exception:
                cur.execute(
                    """
                    INSERT INTO dbo.users(username, display_name, role, active, is_deleted, created_at, updated_at, updated_by)
                    VALUES(?, ?, ?, ?, 0, SYSUTCDATETIME(), SYSUTCDATETIME(), ?);
                    """,
                    (uname, dn, role, 1 if active else 0, actor),
                )
        conn.commit()
    if pm:
        _auth_users_sync_primary_machine(db, uname, pm, active, actor)
    return {
        "username": uname,
        "display_name": dn,
        "role": role,
        "active": bool(active),
        "windows_account": wa,
        "primary_machine": pm,
    }


def _auth_users_delete(db, username: str, deleted_by: str, current_user: str = "") -> None:
    uname = _auth_users_validate_username(username)
    actor = str(deleted_by or uname).strip() or uname
    cur_user = str(current_user or "").strip().lower()
    if cur_user and cur_user == uname.lower():
        raise ValueError("não pode apagar o seu próprio utilizador")
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE dbo.users
            SET is_deleted = 1, active = 0, updated_at = SYSUTCDATETIME(), updated_by = ?
            WHERE username = ? AND is_deleted = 0;
            """,
            (actor, uname),
        )
        if int(cur.rowcount or 0) <= 0:
            raise ValueError("utilizador não encontrado")
        try:
            cur.execute(
                """
                UPDATE dbo.device_bindings
                SET active = 0, is_deleted = 1, updated_at = SYSUTCDATETIME()
                WHERE username = ? AND COALESCE(is_deleted, 0) = 0;
                """,
                (uname,),
            )
        except Exception:
            pass
        conn.commit()


def _auth_bindings_validate_machine(machine: str) -> str:
    m = _auth_machine_key(machine)
    if not m:
        raise ValueError("máquina em falta")
    if len(m) > 128:
        raise ValueError("nome de máquina demasiado longo")
    if not re.fullmatch(r"[A-Z0-9_.\-]+", m):
        raise ValueError("nome de máquina inválido (use letras, números, . _ -)")
    return m


def _auth_bindings_list(db, limit: int = 500) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT TOP (?) b.machine, b.username, b.active, u.display_name
            FROM dbo.device_bindings b
            LEFT JOIN dbo.users u ON u.username = b.username AND ISNULL(u.is_deleted, 0) = 0
            WHERE ISNULL(b.is_deleted, 0) = 0
            ORDER BY b.machine ASC;
            """,
            (int(limit),),
        )
        for machine, username, active, display_name in cur.fetchall() or []:
            out.append(
                {
                    "machine": str(machine or ""),
                    "username": str(username or ""),
                    "display_name": str(display_name or username or ""),
                    "active": bool(active),
                }
            )
    return out


def _auth_bindings_create(
    db,
    machine: str,
    username: str,
    active: bool,
    created_by: str,
) -> dict[str, Any]:
    m = _auth_bindings_validate_machine(machine)
    uname = _auth_users_validate_username(username)
    _auth_resolve_user(db, uname, require_exists=True)
    actor = str(created_by or uname).strip() or uname
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT TOP 1 machine, COALESCE(is_deleted, 0)
            FROM dbo.device_bindings
            WHERE machine = ?;
            """,
            (m,),
        )
        row = cur.fetchone()
        if row:
            if int(row[1] or 0) == 0:
                raise ValueError("binding já existe para este PC")
            cur.execute(
                """
                UPDATE dbo.device_bindings
                SET username = ?, active = ?, is_deleted = 0,
                    updated_at = SYSUTCDATETIME(), updated_by = ?
                WHERE machine = ?;
                """,
                (uname, 1 if active else 0, actor, m),
            )
        else:
            try:
                cur.execute(
                    """
                    INSERT INTO dbo.device_bindings(machine, username, active, is_deleted, created_at, updated_at, updated_by)
                    VALUES(?, ?, ?, 0, SYSUTCDATETIME(), SYSUTCDATETIME(), ?);
                    """,
                    (m, uname, 1 if active else 0, actor),
                )
            except Exception:
                cur.execute(
                    """
                    INSERT INTO dbo.device_bindings(machine, username, active, is_deleted, created_at)
                    VALUES(?, ?, ?, 0, SYSUTCDATETIME());
                    """,
                    (m, uname, 1 if active else 0),
                )
        conn.commit()
    return {"machine": m, "username": uname, "active": bool(active)}


def _auth_bindings_update(
    db,
    machine: str,
    username: str,
    active: bool,
    updated_by: str,
) -> None:
    m = _auth_bindings_validate_machine(machine)
    uname = _auth_users_validate_username(username)
    _auth_resolve_user(db, uname, require_exists=True)
    actor = str(updated_by or uname).strip() or uname
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE dbo.device_bindings
            SET username = ?, active = ?, updated_at = SYSUTCDATETIME(), updated_by = ?
            WHERE machine = ? AND ISNULL(is_deleted, 0) = 0;
            """,
            (uname, 1 if active else 0, actor, m),
        )
        if int(cur.rowcount or 0) <= 0:
            raise ValueError("binding não encontrado")
        conn.commit()


def _auth_bindings_delete(db, machine: str, deleted_by: str) -> None:
    m = _auth_bindings_validate_machine(machine)
    actor = str(deleted_by or "admin").strip() or "admin"
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE dbo.device_bindings
            SET is_deleted = 1, active = 0, updated_at = SYSUTCDATETIME(), updated_by = ?
            WHERE machine = ? AND ISNULL(is_deleted, 0) = 0;
            """,
            (actor, m),
        )
        if int(cur.rowcount or 0) <= 0:
            raise ValueError("binding não encontrado")
        conn.commit()


def _auth_suggest_login_mode(db, machine: str, windows_user: str, binding: dict[str, Any] | None) -> str:
    if binding and str(binding.get("username") or "").strip():
        return "pc"
    wu = _auth_normalize_login_username(windows_user)
    if wu:
        try:
            _auth_resolve_user(db, wu, require_exists=True)
            return "windows"
        except Exception:
            pass
    return "pc"


def _milestone_label(raw: Any) -> str:
    s = str(raw or "").strip()
    return s if s else "(sem milestone)"


def _milestone_gantt_id(name: str) -> str:
    s = re.sub(r"[^\w\-]", "_", str(name or "sem_milestone").strip())[:96]
    return f"ms_{s}"


def _milestone_progress(tasks: list[dict[str, Any]]) -> int:
    if not tasks:
        return 0
    done = sum(1 for t in tasks if "conclu" in str(t.get("Estado") or "").strip().lower())
    return int(round(100.0 * done / len(tasks)))


def _planning_update_tasks(db, username: str, display: str, role: str, updates: list[dict[str, Any]]) -> dict[str, Any]:
    ok = 0
    fail = 0
    errors: list[dict[str, Any]] = []
    for u in list(updates or []):
        tid = str(u.get("id") or "").strip()
        if not tid or not tid.startswith("Task_"):
            fail += 1
            errors.append({"id": tid, "error": "id inválido"})
            continue
        start = str(u.get("start") or "").strip()[:10]
        end = str(u.get("end") or "").strip()[:10]
        try:
            detail = db.get_task_detail(tid, username, display, role) or {}
            task = dict(detail.get("task") or {})
            if not task:
                raise ValueError("tarefa não encontrada")
            task["InicioPrevisto"] = start
            task["Prazo"] = end
            db.update_task(tid, task, username, display, role)
            ok += 1
        except Exception as ex:
            fail += 1
            errors.append({"id": tid, "error": str(ex)})
    return {"updated": ok, "failed": fail, "errors": errors}


def _parse_achievement_filters(qs: dict[str, list[str]]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "year": _q1(qs, "year", "Todos"),
        "date_from": _q1(qs, "date_from"),
        "date_to": _q1(qs, "date_to"),
        "person": _q1(qs, "person", "Todos"),
        "category": _q1(qs, "category", "Todos"),
        "status": _q1(qs, "status", "Todos"),
        "q": _q1(qs, "q"),
        "project": _q1(qs, "project"),
        "machine": _q1(qs, "machine"),
    }
    out["validated_only"] = _q1(qs, "validated_only") in ("1", "true", "yes", "on")
    return out


def _build_achievements_xlsx(rows: list[dict[str, Any]]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Conquistas"
    cols = [
        ("Data", "achievement_date"),
        ("Pessoa", "owner_display"),
        ("Utilizador", "owner_username"),
        ("Titulo", "title"),
        ("Categoria", "category"),
        ("Estado", "status"),
        ("Impacto EUR", "impact_eur"),
        ("Scrap %", "scrap_reduction_pct"),
        ("Falhas %", "failure_reduction_pct"),
        ("Cycle Time %", "cycle_time_reduction_pct"),
        ("Horas", "hours_saved"),
        ("Projeto", "project"),
        ("Linha", "line"),
        ("Maquina", "machine"),
        ("TaskID", "task_id"),
        ("Evidencia URL", "evidence_url"),
        ("Observacoes", "notes"),
        ("Criado em", "created_at"),
        ("Validado por", "validated_by"),
        ("Validado em", "validated_at"),
        ("Comentario validador", "validator_comment"),
    ]
    ws.append([label for label, _ in cols])
    for row in rows or []:
        ws.append([row.get(key, "") for _, key in cols])
    for idx, (label, _) in enumerate(cols, start=1):
        base_width = max(12, min(52, len(label) + 4))
        ws.column_dimensions[chr(64 + idx)].width = base_width
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def _scheduled_log_path(base_mod: Any) -> Path:
    return base_mod.cache_dir() / "scheduled_ops.jsonl"


def _scheduled_log_append(
    db: Any,
    action: str,
    user: str,
    ok: bool,
    message: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    try:
        svc = getattr(db, "_scheduled", None)
        if svc is None:
            return
        svc.append_op(action, user, ok, message, details)
    except Exception as ex:
        try:
            base_mod.log(f"Aviso: erro ao registar log de Programadas: {ex}")
        except Exception:
            pass


def _scheduled_log_read(db: Any, base_mod: Any, limit: int = 120) -> list[dict[str, Any]]:
    try:
        svc = getattr(db, "_scheduled", None)
        if svc is None:
            return []
        return svc.list_ops(limit, str(_scheduled_log_path(base_mod)))
    except Exception:
        return []


def _scheduled_log_clear(db: Any) -> None:
    try:
        svc = getattr(db, "_scheduled", None)
        if svc is None:
            return
        svc.clear_ops()
    except Exception:
        pass


def _patch_database(Database, base_mod):
    from attachments_service import AttachmentsService
    from notes_service import NotesService

    _orig_init = Database.__init__
    _orig_list_tasks = getattr(Database, "list_tasks", None)
    _orig_get_task_detail = getattr(Database, "get_task_detail", None)

    def __init__(self, cfg):  # noqa: N802
        _orig_init(self, cfg)
        self._notes = NotesService(
            base_dir_fn=base_mod.base_dir,
            log_fn=base_mod.log,
        )
        self._attachments = AttachmentsService(
            da=self,
            cache_dir_fn=base_mod.cache_dir,
        )

    Database.__init__ = __init__  # type: ignore[method-assign]

    if not hasattr(Database, "fetch_task_row"):

        def fetch_task_row(self, conn, task_id: str) -> dict[str, Any] | None:
            tid = str(task_id or "").strip()
            if not tid:
                return None
            cur = conn.cursor()
            cur.execute(
                """
                SELECT TaskID, COALESCE(Private,0), COALESCE(CreatedBy,''), COALESCE(Responsavel,''), COALESCE(Pasta,'')
                FROM dbo.tasks WHERE TaskID=?;
                """,
                (tid,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "TaskID": str(row[0] or "").strip(),
                "Private": int(row[1] or 0),
                "CreatedBy": str(row[2] or "").strip(),
                "Responsavel": str(row[3] or "").strip(),
                "Pasta": str(row[4] or "").strip(),
            }

        Database.fetch_task_row = fetch_task_row  # type: ignore[method-assign]

    def _privacy_map_for_tasks(self, task_ids: list[str]) -> dict[str, tuple[int, str, str]]:
        out: dict[str, tuple[int, str, str]] = {}
        ids = [str(x or "").strip() for x in (task_ids or []) if str(x or "").strip()]
        if not ids:
            return out
        uniq = list(dict.fromkeys(ids))
        marks = ",".join(["?"] * len(uniq))
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT TaskID, COALESCE(Private,0), COALESCE(CreatedBy,''), COALESCE(Responsavel,'') FROM dbo.tasks WHERE TaskID IN ({marks});",
                tuple(uniq),
            )
            for tid, pv, cb, resp in cur.fetchall() or []:
                k = str(tid or "").strip()
                if not k:
                    continue
                try:
                    pvi = 1 if int(pv or 0) else 0
                except Exception:
                    pvi = 0
                out[k] = (pvi, str(cb or "").strip(), str(resp or "").strip())
        return out

    def _inject_private_aliases(row: dict[str, Any], pv: int, cb: str) -> dict[str, Any]:
        r = dict(row or {})
        r["Private"] = int(1 if pv else 0)
        r["private"] = int(1 if pv else 0)
        if cb:
            r["CreatedBy"] = str(r.get("CreatedBy") or cb).strip()
        return r

    if callable(_orig_list_tasks):

        def list_tasks(self, filters: dict, username: str, display: str, role: str):  # type: ignore[override]
            rows = _orig_list_tasks(self, filters, username, display, role) or []
            try:
                is_admin = _normalize_role(str(role or "")) == "admin"
                u = str(username or "").strip().lower()
                d = str(display or "").strip().lower()
                tids: list[str] = []
                for row in rows:
                    if isinstance(row, dict):
                        tid = str(row.get("TaskID") or row.get("task_id") or "").strip()
                        if tid:
                            tids.append(tid)
                pmap = _privacy_map_for_tasks(self, tids)
                out_rows: list[Any] = []
                for row in rows:
                    if not isinstance(row, dict):
                        out_rows.append(row)
                        continue
                    tid = str(row.get("TaskID") or row.get("task_id") or "").strip()
                    if tid in pmap:
                        pv, cb, resp = pmap.get(tid, (0, "", ""))
                    else:
                        raw = row.get("Private", row.get("private", 0))
                        try:
                            pv = 1 if int(raw or 0) else 0
                        except Exception:
                            pv = 0
                        cb = str(row.get("CreatedBy") or "").strip()
                        resp = str(row.get("Responsavel") or row.get("responsavel") or "").strip()
                    cb_l = str(cb or "").strip().lower()
                    resp_l = str(resp or "").strip().lower()
                    visible = (pv == 0) or is_admin or (u and cb_l == u) or (d and resp_l == d)
                    if not visible:
                        continue
                    out_rows.append(_inject_private_aliases(row, pv, cb))
                return out_rows
            except Exception:
                return rows

        Database.list_tasks = list_tasks  # type: ignore[method-assign]

    if callable(_orig_get_task_detail):

        def get_task_detail(self, task_id: str, username: str, display: str, role: str):  # type: ignore[override]
            out = _orig_get_task_detail(self, task_id, username, display, role)
            try:
                if not isinstance(out, dict):
                    return out
                task = out.get("task")
                if not isinstance(task, dict):
                    task = {}
                tid = str(task.get("TaskID") or task_id or "").strip()
                if not tid:
                    out["task"] = task
                    return out
                pmap = _privacy_map_for_tasks(self, [tid])
                pv, cb, resp = pmap.get(tid, (0, "", ""))
                is_admin = _normalize_role(str(role or "")) == "admin"
                u = str(username or "").strip().lower()
                d = str(display or "").strip().lower()
                cb_l = str(cb or "").strip().lower()
                resp_l = str(resp or "").strip().lower()
                visible = (pv == 0) or is_admin or (u and cb_l == u) or (d and resp_l == d)
                if not visible:
                    raise PermissionError("Não tem permissão para ver esta tarefa privada.")
                out["task"] = _inject_private_aliases(task, pv, cb)
            except PermissionError:
                raise
            except Exception:
                pass
            return out

        Database.get_task_detail = get_task_detail  # type: ignore[method-assign]

    def get_user_notes(self, username: str) -> dict:
        return self._notes.read(username)

    def save_user_notes(self, username: str, content: str) -> dict:
        return self._notes.save(username, content)

    Database.get_user_notes = get_user_notes  # type: ignore[method-assign]
    Database.save_user_notes = save_user_notes  # type: ignore[method-assign]

    def preview_scheduled(self, payload: dict, username: str, role: str) -> dict:
        count = int((payload or {}).get("count") or 12)
        return self._scheduled.preview_occurrences(payload or {}, username, count)

    Database.preview_scheduled = preview_scheduled  # type: ignore[method-assign]

    def get_diagnostics(self, username: str, role: str, ui_build: str, host: str, port: int) -> dict:
        from diagnostic_service import run_diagnostics as run_diag_checks
        from files_service import resolve_app_root, validate_onedrive_root

        cfg = dict(self.cfg or {})
        cfg["_web_username"] = username
        root = resolve_app_root(cfg, base_mod.cache_dir)
        ok, msg = validate_onedrive_root(root) if root else (False, "Pasta OneDrive nao configurada.")
        onedrive = {
            "valid": ok,
            "message": msg,
            "onedrive_root": root,
            "needs_setup": not ok,
        }
        return run_diag_checks(
            cfg=cfg,
            user={"username": username, "role": role},
            version=base_mod.APP_VERSION,
            ui_build=ui_build,
            host=host,
            port=port,
            cache_dir=base_mod.cache_dir(),
            log_path=base_mod.cache_dir() / "web_ui_local.log",
            onedrive=onedrive,
            connect_fn=self.connect,
        )

    Database.get_diagnostics = get_diagnostics  # type: ignore[method-assign]

    def attachments_upload_bytes(
        self,
        task_id: str,
        filename: str,
        data: bytes,
        username: str,
        display: str,
        role: str,
        pasta_field: str = "",
    ) -> dict:
        cfg = dict(self.cfg or {})
        cfg["_web_username"] = username
        return self._attachments.save_upload_bytes(
            cfg=cfg,
            task_id=task_id,
            filename=filename,
            data=data,
            username=username,
            display=display,
            role=role,
            pasta_field=pasta_field,
        )

    Database.attachments_upload_bytes = attachments_upload_bytes  # type: ignore[method-assign]

    def _gantt_progress(status: str) -> int:
        st = str(status or "").strip().lower()
        if "conclu" in st:
            return 100
        if ("progres" in st) or ("curso" in st):
            return 50
        if "bloque" in st:
            return 15
        return 0

    def _gantt_norm_dates(start_raw: Any, due_raw: Any) -> tuple[str, str, str]:
        def _p(v: Any):
            s = str(v or "").strip()[:10]
            if not s:
                return "", None
            try:
                return s, datetime.strptime(s, "%Y-%m-%d").date()
            except Exception:
                return s, None

        s_txt, s_dt = _p(start_raw)
        d_txt, d_dt = _p(due_raw)
        if s_txt and s_dt is None:
            return "", "", "start_date inválida"
        if d_txt and d_dt is None:
            return "", "", "due_date inválida"
        if s_dt and d_dt:
            return s_dt.isoformat(), d_dt.isoformat(), ""
        if s_dt and not d_dt:
            one = s_dt.isoformat()
            return one, one, ""
        if d_dt and not s_dt:
            one = d_dt.isoformat()
            return one, one, ""
        return "", "", "sem datas"

    def _gantt_deps_map(conn, action_ids: list[int]) -> dict[int, str]:
        out: dict[int, str] = {}
        ids = [int(x) for x in (action_ids or []) if int(x or 0) > 0]
        if not ids:
            return out
        marks = ",".join(["?"] * len(ids))
        sql = (
            "SELECT action_id, depends_on FROM dbo.action_dependencies "
            f"WHERE COALESCE(is_deleted,0)=0 AND action_id IN ({marks}) ORDER BY action_id, depends_on;"
        )
        cur = conn.cursor()
        try:
            cur.execute(sql, tuple(ids))
        except Exception:
            cur.execute(
                f"SELECT action_id, depends_on FROM dbo.action_dependencies WHERE action_id IN ({marks}) ORDER BY action_id, depends_on;",
                tuple(ids),
            )
        dep_map: dict[int, list[str]] = {}
        for action_id, depends_on in cur.fetchall() or []:
            aid = int(action_id or 0)
            did = int(depends_on or 0)
            if aid <= 0 or did <= 0:
                continue
            dep_map.setdefault(aid, []).append(f"action_{did}")
        for aid, deps in dep_map.items():
            out[aid] = ",".join(deps)
        return out

    def get_task_gantt_data(self, task_id: str, username: str, display: str, role: str) -> dict:
        app_err = getattr(base_mod, "AppError", RuntimeError)
        tid = str(task_id or "").strip()
        if not tid:
            raise app_err("TaskID em falta")
        detail = self.get_task_detail(tid, username, display, role)
        if not isinstance(detail, dict) or not detail.get("task"):
            raise app_err("Tarefa não encontrada")
        task = dict(detail.get("task") or {})
        # Fonte robusta/paritária: ler ações/checks diretamente da mesma consulta usada no detalhe.
        # Evita divergências de status entre bloco "Ações/Checklist" e Gantt.
        try:
            actions, checks = _task_detail_items_sql(self, tid)
        except Exception:
            actions = list(detail.get("actions") or [])
            checks = list(detail.get("checklist") or [])
        all_items: list[dict[str, Any]] = []
        seen_keys: set[str] = set()

        def _norm_item(raw: Any, fallback_kind: str) -> dict[str, Any]:
            it = dict(raw or {})
            kind = str(it.get("kind") or fallback_kind or "").strip().upper()
            has_action_meta = _checklist_has_action_meta(
                it.get("owner"),
                it.get("workers"),
                it.get("start_date"),
                it.get("due_date"),
                it.get("status"),
                it.get("evidence"),
                it.get("blocked_reason"),
            )
            if kind not in ("ACTION", "CHECK"):
                kind = "ACTION" if has_action_meta else "CHECK"
            elif kind == "CHECK" and has_action_meta:
                kind = "ACTION"
            it["kind"] = kind
            if not str(it.get("item_text") or "").strip():
                it["item_text"] = str(it.get("action_text") or it.get("text") or "").strip()
            return it

        def _push(items_src: list[Any], fallback_kind: str) -> None:
            for raw in items_src:
                it = _norm_item(raw, fallback_kind)
                rid = str(it.get("id") or it.get("item_uuid") or "").strip()
                key = rid or (
                    f"{it.get('kind','')}|{str(it.get('item_text') or '').strip()}|"
                    f"{str(it.get('start_date') or '').strip()}|{str(it.get('due_date') or '').strip()}"
                )
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                all_items.append(it)

        _push(actions, "ACTION")
        _push(checks, "CHECK")
        action_ids = [int(x.get("id") or 0) for x in all_items if int(x.get("id") or 0) > 0 and str(x.get("kind") or "").upper() == "ACTION"]
        deps_map: dict[int, str] = {}
        try:
            with self.connect() as conn:
                deps_map = _gantt_deps_map(conn, action_ids)
        except Exception:
            deps_map = {}
        today = datetime.now().strftime("%Y-%m-%d")
        items: list[dict[str, Any]] = []
        undated: list[dict[str, Any]] = []
        for it in all_items:
            kind = str(it.get("kind") or "CHECK").strip().upper()
            if kind not in ("ACTION", "CHECK"):
                continue
            aid = int(it.get("id") or 0)
            name = str(it.get("item_text") or "").strip()
            status = str(it.get("status") or "").strip()
            if not status:
                status = "Concluído" if bool(it.get("is_done")) else "Não iniciado"
            start, end, reason = _gantt_norm_dates(it.get("start_date"), it.get("due_date"))
            if not (start and end):
                undated.append(
                    {
                        "id": f"action_{aid}",
                        "action_id": aid,
                        "name": name,
                        "status": status,
                        "done": bool(it.get("done") or it.get("is_done")),
                        "is_done": bool(it.get("done") or it.get("is_done")),
                        "blocked_reason": str(it.get("blocked_reason") or "").strip(),
                        "owner": str(it.get("owner_display") or it.get("owner") or "").strip(),
                        "workers": str(it.get("workers") or "").strip(),
                        "kind": kind,
                        "reason": reason or "sem datas válidas",
                    }
                )
                continue
            items.append(
                {
                    "id": f"action_{aid}",
                    "action_id": aid,
                    "name": name,
                    "start": start,
                    "end": end,
                    "progress": _gantt_progress(status),
                    "status": status,
                    "done": bool(it.get("done") or it.get("is_done")),
                    "is_done": bool(it.get("done") or it.get("is_done")),
                    "blocked_reason": str(it.get("blocked_reason") or "").strip(),
                    "owner": str(it.get("owner_display") or it.get("owner") or "").strip(),
                    "workers": str(it.get("workers") or "").strip(),
                    "kind": kind,
                    "dependencies": deps_map.get(aid, "") if kind == "ACTION" else "",
                    "is_overdue": bool(end < today and ("conclu" not in str(status).strip().lower())),
                }
            )
        return {
            "task_id": tid,
            "task_title": str(task.get("Tarefa") or tid),
            "items": items,
            "undated_items": undated,
            "permissions": {"can_edit": bool(detail.get("can_edit"))},
        }

    def get_project_gantt_data(
        self,
        projeto: str,
        milestone: str,
        include_actions: bool,
        username: str,
        display: str,
        role: str,
    ) -> dict:
        proj = str(projeto or "Todos").strip() or "Todos"
        ms_filter = _milestone_label(milestone) if str(milestone or "Todos").strip() not in ("", "Todos") else "Todos"
        base_filters: dict[str, Any] = {"projeto": proj, "show_done": True}
        all_rows = self.list_tasks(base_filters, username, display, role) or []
        milestones = sorted({_milestone_label(r.get("Milestone")) for r in all_rows})
        rows = list(all_rows)
        if ms_filter != "Todos":
            rows = [r for r in rows if _milestone_label(r.get("Milestone")) == ms_filter]
        rows.sort(
            key=lambda r: (
                _milestone_label(r.get("Milestone")),
                str(r.get("InicioPrevisto") or r.get("DataRegisto") or "9999"),
                str(r.get("Tarefa") or ""),
            )
        )
        groups: dict[str, list[dict[str, Any]]] = {}
        for task in rows:
            ms = _milestone_label(task.get("Milestone"))
            groups.setdefault(ms, []).append(task)
        task_ids = [str(r.get("TaskID") or "").strip() for r in rows if str(r.get("TaskID") or "").strip()]
        checklist_map: dict[str, tuple[list, list]] = {}
        all_action_ids: list[int] = []
        if include_actions and task_ids:
            checklist_map = _batch_checklist_by_tasks(self, task_ids)
            for acts, _ in checklist_map.values():
                for a in acts:
                    aid = int(a.get("id") or 0)
                    if aid > 0:
                        all_action_ids.append(aid)
        deps_map: dict[int, str] = {}
        if all_action_ids:
            try:
                with self.connect() as conn:
                    deps_map = _gantt_deps_map(conn, all_action_ids)
            except Exception:
                deps_map = {}
        today = datetime.now().strftime("%Y-%m-%d")
        items: list[dict[str, Any]] = []
        undated: list[dict[str, Any]] = []
        can_edit = bool(_can_edit_role(role))

        def _append_actions(tid: str, ms: str) -> None:
            if not include_actions:
                return
            actions, _checks = checklist_map.get(tid, ([], []))
            for it in actions:
                kind = str(it.get("kind") or "ACTION").strip().upper()
                if kind != "ACTION":
                    continue
                aid = int(it.get("id") or 0)
                aname = str(it.get("item_text") or "").strip()
                st = str(it.get("status") or "").strip()
                if not st:
                    st = "Concluído" if bool(it.get("is_done")) else "Não iniciado"
                a_start, a_end, a_reason = _gantt_norm_dates(it.get("start_date"), it.get("due_date"))
                label = ("    ↳ " + aname) if aname else "    ↳ Ação"
                if a_start and a_end:
                    items.append(
                        {
                            "id": f"action_{aid}",
                            "action_id": aid,
                            "task_id": tid,
                            "milestone": ms,
                            "name": label,
                            "start": a_start,
                            "end": a_end,
                            "progress": _gantt_progress(st),
                            "status": st,
                            "done": bool(it.get("done") or it.get("is_done")),
                            "is_done": bool(it.get("done") or it.get("is_done")),
                            "blocked_reason": str(it.get("blocked_reason") or "").strip(),
                            "owner": str(it.get("owner") or "").strip(),
                            "workers": str(it.get("workers") or "").strip(),
                            "item_type": "ACTION",
                            "kind": "ACTION",
                            "dependencies": deps_map.get(aid, ""),
                            "is_overdue": bool(a_end < today and ("conclu" not in st.lower())),
                        }
                    )
                else:
                    undated.append(
                        {
                            "id": f"action_{aid}",
                            "action_id": aid,
                            "task_id": tid,
                            "milestone": ms,
                            "name": label,
                            "status": st,
                            "item_type": "ACTION",
                            "kind": "ACTION",
                            "reason": a_reason or "sem datas",
                        }
                    )

        for ms in sorted(groups.keys()):
            tasks_in_ms = groups[ms]
            ms_starts: list[str] = []
            ms_ends: list[str] = []
            for t in tasks_in_ms:
                s, e, _ = _gantt_norm_dates(t.get("InicioPrevisto") or t.get("DataRegisto"), t.get("Prazo"))
                if s and e:
                    ms_starts.append(s)
                    ms_ends.append(e)
            ms_id = _milestone_gantt_id(ms)
            if ms_starts and ms_ends:
                items.append(
                    {
                        "id": ms_id,
                        "milestone": ms,
                        "name": f"◆ {ms} ({len(tasks_in_ms)})",
                        "start": min(ms_starts),
                        "end": max(ms_ends),
                        "progress": _milestone_progress(tasks_in_ms),
                        "status": f"{len(tasks_in_ms)} tarefa(s)",
                        "item_type": "MILESTONE",
                        "kind": "MILESTONE",
                        "dependencies": "",
                        "task_count": len(tasks_in_ms),
                    }
                )
            else:
                undated.append(
                    {
                        "id": ms_id,
                        "milestone": ms,
                        "name": f"◆ {ms}",
                        "item_type": "MILESTONE",
                        "kind": "MILESTONE",
                        "status": f"{len(tasks_in_ms)} tarefa(s)",
                        "reason": "sem datas nas tarefas",
                    }
                )
            for task in tasks_in_ms:
                tid = str(task.get("TaskID") or "").strip()
                if not tid:
                    continue
                title = str(task.get("Tarefa") or tid).strip()
                status = str(task.get("Estado") or "").strip() or "Não iniciado"
                start, end, reason = _gantt_norm_dates(
                    task.get("InicioPrevisto") or task.get("DataRegisto"),
                    task.get("Prazo"),
                )
                label = "  · " + title
                if start and end:
                    items.append(
                        {
                            "id": tid,
                            "task_id": tid,
                            "milestone": ms,
                            "name": label,
                            "start": start,
                            "end": end,
                            "progress": _gantt_progress(status),
                            "status": status,
                            "item_type": "TASK",
                            "kind": "TASK",
                            "dependencies": "",
                        "is_overdue": bool(end < today and ("conclu" not in status.lower())),
                        "projeto": str(task.get("Projeto") or "").strip(),
                        "milestone": ms,
                        "owner": str(task.get("Responsavel") or "").strip(),
                        "responsavel": str(task.get("Responsavel") or "").strip(),
                    }
                    )
                else:
                    undated.append(
                        {
                            "id": tid,
                            "task_id": tid,
                            "milestone": ms,
                            "name": label,
                            "status": status,
                            "item_type": "TASK",
                            "kind": "TASK",
                            "reason": reason or "sem datas",
                        }
                    )
                _append_actions(tid, ms)
        return {
            "projeto": proj,
            "milestone": ms_filter,
            "milestones": ["Todos"] + milestones,
            "include_actions": bool(include_actions),
            "task_count": len(rows),
            "milestone_count": len(groups),
            "items": items,
            "undated_items": undated,
            "permissions": {"can_edit": can_edit},
        }

    Database.get_task_gantt_data = get_task_gantt_data  # type: ignore[method-assign]
    Database.get_project_gantt_data = get_project_gantt_data  # type: ignore[method-assign]

    def update_action_gantt_dates(
        self, action_id: int, start_date: str, due_date: str, username: str, display: str, role: str
    ) -> dict:
        app_err = getattr(base_mod, "AppError", RuntimeError)
        try:
            aid = int(action_id)
        except Exception:
            raise app_err("action_id inválido")
        if aid <= 0:
            raise app_err("action_id inválido")
        start_s = str(start_date or "").strip()[:10]
        due_s = str(due_date or "").strip()[:10]
        if not start_s and not due_s:
            raise app_err("start_date/due_date em falta")
        if start_s:
            try:
                datetime.strptime(start_s, "%Y-%m-%d")
            except Exception:
                raise app_err("start_date inválida")
        if due_s:
            try:
                datetime.strptime(due_s, "%Y-%m-%d")
            except Exception:
                raise app_err("due_date inválida")
        if not start_s:
            start_s = due_s
        if not due_s:
            due_s = start_s
        old_start = ""
        old_due = ""
        task_id = ""
        action_text = ""
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT TaskID, COALESCE(start_date,''), COALESCE(due_date,''), COALESCE(item_text,''), COALESCE(kind,'CHECK')
                FROM dbo.task_checklist WHERE id=?;
                """,
                (aid,),
            )
            row = cur.fetchone()
            if not row:
                raise app_err("Ação não encontrada")
            task_id = str(row[0] or "").strip()
            old_start = str(row[1] or "").strip()[:10]
            old_due = str(row[2] or "").strip()[:10]
            action_text = str(row[3] or "").strip()
            kind = str(row[4] or "CHECK").strip().upper()
            if kind != "ACTION":
                raise app_err("O item selecionado não é uma Ação")
        self.update_action(
            aid,
            {"start_date": start_s, "due_date": due_s},
            username,
            display,
            role,
        )
        if old_start != start_s or old_due != due_s:
            try:
                with self.connect() as conn_h:
                    ev_user = str(display or username or "-").strip() or "-"
                    details = (
                        f"Ação {aid} alterada no Gantt: início {old_start or '—'} -> {start_s}, "
                        f"prazo {old_due or '—'} -> {due_s}"
                    )
                    if action_text:
                        details = f"{action_text[:90]} | {details}"
                    conn_h.cursor().execute(
                        "INSERT INTO dbo.task_history (ts, TaskID, [user], event, details) VALUES (?,?,?,?,?);",
                        (datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), task_id, ev_user, "gantt_update", details),
                    )
                    conn_h.commit()
            except Exception:
                pass
        return {
            "action_id": aid,
            "task_id": task_id,
            "start_date": start_s,
            "due_date": due_s,
            "old_start_date": old_start,
            "old_due_date": old_due,
        }

    Database.update_action_gantt_dates = update_action_gantt_dates  # type: ignore[method-assign]

    _orig_insert_scheduled = Database.insert_scheduled
    _orig_update_scheduled = Database.update_scheduled
    _orig_toggle_scheduled = Database.toggle_scheduled
    _orig_generate_scheduled = Database.generate_scheduled
    _orig_materialize = Database.materialize_scheduled_pending
    _orig_generate_due = Database.generate_scheduled_due

    def insert_scheduled(self, payload: dict, username: str, role: str) -> int:
        try:
            new_id = _orig_insert_scheduled(self, payload, username, role)
            _scheduled_log_append(
                self, "template_create", username, True,
                f"Template criado #{new_id}",
                {"template_id": int(new_id or 0)},
            )
            return new_id
        except Exception as ex:
            _scheduled_log_append(self, "template_create", username, False, str(ex))
            raise

    def update_scheduled(self, template_id: int, payload: dict, username: str, role: str) -> None:
        try:
            _orig_update_scheduled(self, template_id, payload, username, role)
            _scheduled_log_append(
                self, "template_update", username, True,
                f"Template #{int(template_id)} atualizado",
                {"template_id": int(template_id)},
            )
        except Exception as ex:
            _scheduled_log_append(
                self, "template_update", username, False, str(ex), {"template_id": int(template_id or 0)}
            )
            raise

    def toggle_scheduled(self, template_id: int, username: str, role: str) -> bool:
        try:
            is_active = _orig_toggle_scheduled(self, template_id, username, role)
            _scheduled_log_append(
                self, "template_toggle", username, True,
                f"Template #{int(template_id)} {'ativado' if is_active else 'desativado'}",
                {"template_id": int(template_id), "is_active": bool(is_active)},
            )
            return is_active
        except Exception as ex:
            _scheduled_log_append(
                self, "template_toggle", username, False, str(ex), {"template_id": int(template_id or 0)}
            )
            raise

    def generate_scheduled(self, template_id: int, username: str, display: str, role: str) -> str:
        try:
            msg = _orig_generate_scheduled(self, template_id, username, display, role)
            _scheduled_log_append(
                self, "generate_now", username, True, str(msg or ""),
                {"template_id": int(template_id)},
            )
            return msg
        except Exception as ex:
            _scheduled_log_append(
                self, "generate_now", username, False, str(ex), {"template_id": int(template_id or 0)}
            )
            raise

    def materialize_scheduled_pending(self, template_id: int, username: str, display: str, role: str) -> str:
        try:
            msg = _orig_materialize(self, template_id, username, display, role)
            _scheduled_log_append(
                self, "materialize_pending", username, True, str(msg or ""),
                {"template_id": int(template_id)},
            )
            return msg
        except Exception as ex:
            _scheduled_log_append(
                self, "materialize_pending", username, False, str(ex), {"template_id": int(template_id or 0)}
            )
            raise

    def generate_scheduled_due(self, username: str, display: str, role: str, dry_run: bool = False) -> dict:
        try:
            rep = _orig_generate_due(self, username, display, role, dry_run=dry_run)
            _scheduled_log_append(
                self,
                "process_due",
                username,
                True,
                f"Vencidas processadas (dry_run={bool(dry_run)})",
                {
                    "dry_run": bool(dry_run),
                    "processed": int((rep or {}).get("processed") or 0),
                    "auto_created": int((rep or {}).get("auto_created") or 0),
                    "manual_pending": int((rep or {}).get("manual_pending") or 0),
                    "errors": len((rep or {}).get("errors") or []),
                },
            )
            return rep
        except Exception as ex:
            _scheduled_log_append(self, "process_due", username, False, str(ex), {"dry_run": bool(dry_run)})
            raise

    Database.insert_scheduled = insert_scheduled  # type: ignore[method-assign]
    Database.update_scheduled = update_scheduled  # type: ignore[method-assign]
    Database.toggle_scheduled = toggle_scheduled  # type: ignore[method-assign]
    Database.generate_scheduled = generate_scheduled  # type: ignore[method-assign]
    Database.materialize_scheduled_pending = materialize_scheduled_pending  # type: ignore[method-assign]
    Database.generate_scheduled_due = generate_scheduled_due  # type: ignore[method-assign]

    _orig_insert_machine = getattr(Database, "insert_machine", None)
    _orig_update_machine = getattr(Database, "update_machine", None)

    if callable(_orig_insert_machine):

        def insert_machine(self, values, role, username="", display=""):  # type: ignore[override]
            cat = getattr(self, "_catalog", None)
            if cat is not None and hasattr(cat, "insert_machine"):
                return cat.insert_machine(values, role, username=str(username or ""), display=str(display or ""))
            return _orig_insert_machine(self, values, role)

        Database.insert_machine = insert_machine  # type: ignore[method-assign]

    if callable(_orig_update_machine):

        def update_machine(self, machine_id, values, role, username="", display=""):  # type: ignore[override]
            cat = getattr(self, "_catalog", None)
            if cat is not None and hasattr(cat, "update_machine"):
                return cat.update_machine(
                    machine_id,
                    values,
                    role,
                    username=str(username or ""),
                    display=str(display or ""),
                )
            return _orig_update_machine(self, machine_id, values, role)

        Database.update_machine = update_machine  # type: ignore[method-assign]

    def submit_machine_field(self, machine_id, field, role, username="", display=""):
        return self._catalog.submit_machine_field(
            machine_id, field, role, username=str(username or ""), display=str(display or "")
        )

    def approve_machine_field(self, machine_id, field, role, username="", display=""):
        return self._catalog.approve_machine_field(
            machine_id, field, role, username=str(username or ""), display=str(display or "")
        )

    def revert_machine_field(self, machine_id, field, role, username="", display=""):
        return self._catalog.revert_machine_field(
            machine_id, field, role, username=str(username or ""), display=str(display or "")
        )

    Database.submit_machine_field = submit_machine_field  # type: ignore[method-assign]
    Database.approve_machine_field = approve_machine_field  # type: ignore[method-assign]
    Database.revert_machine_field = revert_machine_field  # type: ignore[method-assign]


def _patch_handler(Handler, STATE, parse_path, AppError, PermissionError, base_mod):
    _orig_get = Handler.do_GET
    _orig_post = Handler.do_POST

    def _task_cols_defaults() -> list[str]:
        cols = list(getattr(base_mod, "COLUMNS", []) or [])
        hidden = set(getattr(base_mod, "_HIDDEN_LEGACY_COLUMNS", set()) or set())
        out = [str(c).strip() for c in cols if str(c).strip() and str(c).strip() not in hidden]
        if out:
            if "DataConclusao" not in out:
                if "Estado" in out:
                    out.insert(out.index("Estado") + 1, "DataConclusao")
                elif "Prazo" in out:
                    out.insert(out.index("Prazo") + 1, "DataConclusao")
                else:
                    out.append("DataConclusao")
            return out
        return [
            "TaskID",
            "Tarefa",
            "NotifEmoji",
            "Notificacoes",
            "Milestone",
            "Assunto",
            "DataRegisto",
            "Prazo",
            "Responsavel",
            "Workers",
            "Estado",
            "DataConclusao",
            "Prioridade",
        ]

    def _task_cols_normalize(cols: Any, allowed: list[str]) -> list[str]:
        out: list[str] = []
        allow = set(allowed or [])
        for c in cols or []:
            k = str(c or "").strip()
            if k and k in allow and k not in out:
                out.append(k)
        return out

    def _prefs_get(st: Any, username: str) -> dict[str, Any]:
        db = getattr(st, "db", None)
        if db is None or not hasattr(db, "connect"):
            return {}
        with db.connect() as conn:
            cur = conn.execute(
                """
                SELECT theme, view_density, font_size, zebra_intensity, show_statusbar, accent_theme,
                       COALESCE(filters_json,''), COALESCE(col_layout_json,''), COALESCE(visible_columns_json,''),
                       COALESCE(window_states_json,''), COALESCE(board_filters_json,'')
                FROM user_prefs WHERE username=?;
                """,
                (username.strip(),),
            )
            row = cur.fetchone()
            if not row:
                return {}
            keys = [
                "theme",
                "view_density",
                "font_size",
                "zebra_intensity",
                "show_statusbar",
                "accent_theme",
                "filters_json",
                "col_layout_json",
                "visible_columns_json",
                "window_states_json",
                "board_filters_json",
            ]
            return dict(zip(keys, row))

    def _prefs_upsert(st: Any, username: str, prefs: dict[str, Any]) -> None:
        db = getattr(st, "db", None)
        if db is None or not hasattr(db, "connect"):
            raise RuntimeError("Persistência de preferências indisponível")
        values = (
            (username or "-").strip(),
            prefs.get("theme"),
            prefs.get("view_density"),
            int(prefs.get("font_size") or 10),
            prefs.get("zebra_intensity"),
            1 if bool(prefs.get("show_statusbar", True)) else 0,
            prefs.get("accent_theme"),
            prefs.get("filters_json"),
            prefs.get("col_layout_json"),
            prefs.get("visible_columns_json"),
            prefs.get("window_states_json"),
            prefs.get("board_filters_json"),
        )
        with db.connect() as conn:
            cur = conn.execute(
                """
                UPDATE user_prefs
                SET theme=?,
                    view_density=?,
                    font_size=?,
                    zebra_intensity=?,
                    show_statusbar=?,
                    accent_theme=?,
                    filters_json=COALESCE(?, filters_json),
                    col_layout_json=COALESCE(?, col_layout_json),
                    visible_columns_json=COALESCE(?, visible_columns_json),
                    window_states_json=COALESCE(?, window_states_json),
                    board_filters_json=COALESCE(?, board_filters_json)
                WHERE username=?;
                """,
                (
                    values[1],
                    values[2],
                    values[3],
                    values[4],
                    values[5],
                    values[6],
                    values[7],
                    values[8],
                    values[9],
                    values[10],
                    values[11],
                    values[0],
                ),
            )
            if int(getattr(cur, "rowcount", 0) or 0) <= 0:
                conn.execute(
                    """
                    INSERT INTO user_prefs(
                        username, theme, view_density, font_size, zebra_intensity,
                        show_statusbar, accent_theme, filters_json, col_layout_json,
                        visible_columns_json, window_states_json, board_filters_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    values,
                )
            conn.commit()

    _TASK_GANTT_VENDOR = {
        "/web/vendor/frappe-gantt/frappe-gantt.umd.js": (
            _BASE_DIR.parent / "web" / "vendor" / "frappe-gantt" / "frappe-gantt.umd.js",
            "application/javascript; charset=utf-8",
        ),
        "/web/vendor/frappe-gantt/frappe-gantt.css": (
            _BASE_DIR.parent / "web" / "vendor" / "frappe-gantt" / "frappe-gantt.css",
            "text/css; charset=utf-8",
        ),
        "/web/vendor/frappe-gantt/LICENSE.txt": (
            _BASE_DIR.parent / "web" / "vendor" / "frappe-gantt" / "LICENSE.txt",
            "text/plain; charset=utf-8",
        ),
    }

    def _serve_vendor_asset(handler, path: str) -> bool:
        info = _TASK_GANTT_VENDOR.get(path)
        if not info:
            return False
        file_path, content_type = info
        try:
            data = file_path.read_bytes()
        except Exception:
            handler.err(404, "Ficheiro vendor não encontrado")
            return True
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Cache-Control", "public, max-age=3600")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
        return True

    def do_GET(self):  # noqa: N802
        p, q = parse_path(self.path)
        if _serve_vendor_asset(self, p):
            return
        token, sess = _session_get_from_headers(getattr(self, "headers", None))
        st_obj = base_mod.STATE
        if p == "/api/auth/session":
            if not sess:
                return self.json({"authenticated": False, "idle_sec": _SESSION_IDLE_SEC})
            return self.json(
                {
                    "authenticated": True,
                    "user": {
                        "username": sess.get("username"),
                        "display_name": sess.get("display_name"),
                        "role": sess.get("role"),
                        "machine": sess.get("machine"),
                    },
                    "idle_sec": _SESSION_IDLE_SEC,
                }
            )
        if p == "/api/health":
            if not sess:
                return self.err(401, "Sessão inválida. Faça login.")
            return self.json(
                {
                    "ok": True,
                    "version": str(getattr(base_mod, "APP_VERSION", "") or ""),
                    "app_version": str(getattr(base_mod, "APP_VERSION", "") or ""),
                    "ui_build": APP_VERSION,
                    "user": {
                        "username": sess.get("username"),
                        "display_name": sess.get("display_name"),
                        "role": sess.get("role"),
                        "machine": sess.get("machine"),
                    },
                }
            )
        if p == "/api/auth/meta":
            try:
                origin = _auth_origin_read(base_mod)
                machine = _auth_machine_key(_q1(q or {}, "machine", ""))
                windows_user = os.getenv("USERNAME") or getpass.getuser() or ""
                windows_user_normalized = _auth_normalize_login_username(windows_user)
                binding = None
                suggested_mode = "pc"
                st = base_mod.STATE
                if st is not None and getattr(st, "db", None) is not None:
                    if machine:
                        binding = _auth_machine_binding(st.db, machine)
                    suggested_mode = _auth_suggest_login_mode(st.db, machine, windows_user, binding)
                return self.json(
                    {
                        "machine": machine,
                        "cached_machine": str(origin.get("machine") or ""),
                        "server_machine": os.getenv("COMPUTERNAME") or socket.gethostname(),
                        "windows_user": windows_user,
                        "windows_user_normalized": windows_user_normalized,
                        "binding": binding or {},
                        "suggested_mode": suggested_mode,
                    }
                )
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p.startswith("/api/") and p not in ("/api/health", "/api/auth/session", "/api/auth/meta") and not sess:
            return self.err(401, "Sessão inválida. Faça login.")
        if sess and st_obj is not None:
            try:
                st_obj.username = str(sess.get("username") or "")
                st_obj.display_name = str(sess.get("display_name") or st_obj.username)
                st_obj.role = _normalize_role(str(sess.get("role") or "read"))
                last_touch = float(sess.get("last_db_touch") or 0.0)
                now_t = time.time()
                if (now_t - last_touch) >= 30.0 and getattr(st_obj, "db", None) is not None and sess.get("session_id"):
                    _session_db_touch(st_obj.db, str(sess.get("session_id")))
                    sess["last_db_touch"] = now_t
            except Exception:
                pass
        if p == "/api/tasks/columns/prefs":
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                username = str(getattr(st, "username", "") or "").strip()
                if not username:
                    return self.err(400, "Utilizador inválido")
                allowed = _task_cols_defaults()
                cols = list(allowed)
                widths: dict[str, int] = {}
                up = _prefs_get(st, username) or {}
                raw = str(up.get("visible_columns_json") or "")
                if raw:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        cols = _task_cols_normalize(data.get("columns") or [], allowed) or cols
                        raw_w = data.get("widths")
                        if isinstance(raw_w, dict):
                            for k, v in raw_w.items():
                                kk = str(k or "").strip()
                                if kk in allowed:
                                    try:
                                        w = int(v)
                                        if 40 <= w <= 900:
                                            widths[kk] = w
                                    except Exception:
                                        pass
                return self.json({"columns": cols, "available_columns": allowed, "widths": widths})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/dashboard/charts":
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                mode = _q1(q or {}, "mode", "executivo").strip().lower()
                if mode == "eficiencia":
                    return self.json(_dashboard_efficiency_charts(base_mod, st, q or {}))
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/dashboard/prefs":
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                username = str(getattr(st, "username", "") or "").strip()
                if not username:
                    return self.err(400, "Utilizador inválido")
                up = _prefs_get(st, username) or {}
                raw = str(up.get("filters_json") or "").strip()
                prefs = {}
                if raw:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        prefs = data.get("dashboard_v1") if isinstance(data.get("dashboard_v1"), dict) else {}
                return self.json({"prefs": prefs or {}})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/achievements/export.xlsx":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                filters = _parse_achievement_filters(q or {})
                rows = st.db.list(filters)
                body = _build_achievements_xlsx(rows)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                headers = {
                    "Content-Disposition": f'attachment; filename="conquistas_{stamp}.xlsx"',
                }
                return self.sendb(
                    200,
                    body,
                    ct="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers=headers,
                )
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/notes":
            try:
                st = base_mod.STATE
                return self.json(st.db.get_user_notes(st.username))
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/my-day/summary":
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                display = getattr(st, "display_name", st.username) or st.username
                username_l = str(st.username or "").strip().lower()
                display_l = str(display or "").strip().lower()
                rows = st.db.list_tasks({}, st.username, display, st.role) or []
                today = datetime.now().date()
                week_key = today.isocalendar()[:2]

                def _to_date(v: Any):
                    s = str(v or "").strip()[:10]
                    if not s:
                        return None
                    try:
                        return datetime.strptime(s, "%Y-%m-%d").date()
                    except Exception:
                        return None

                def _is_open(st_txt: str) -> bool:
                    s = str(st_txt or "").strip().lower()
                    return ("conclu" not in s) and ("fechad" not in s)

                def _workers_has_user(workers_txt: str) -> bool:
                    raw = str(workers_txt or "").strip().lower()
                    if not raw:
                        return False
                    tok = re.split(r"[;,|/\n]+", raw)
                    cleaned = [t.strip() for t in tok if t.strip()]
                    if any(t == display_l or t == username_l for t in cleaned):
                        return True
                    return (display_l in raw) or (username_l in raw)

                def _is_mine(row: dict[str, Any]) -> bool:
                    resp = str(row.get("Responsavel") or row.get("responsavel") or "").strip().lower()
                    workers = str(row.get("Workers") or row.get("workers") or "").strip().lower()
                    created = str(row.get("CreatedBy") or row.get("created_by") or "").strip().lower()
                    if resp and (resp == display_l or resp == username_l):
                        return True
                    if _workers_has_user(workers):
                        return True
                    if created and created == username_l:
                        return True
                    return False

                def _prio_weight(prio_txt: str) -> int:
                    p = str(prio_txt or "").strip().lower()
                    if p.startswith("alta"):
                        return 0
                    if p.startswith("m"):
                        return 1
                    return 2

                mine = [r for r in rows if isinstance(r, dict) and _is_mine(r)]
                open_rows = [r for r in mine if _is_open(r.get("Estado"))]
                overdue_n = 0
                blocked_n = 0
                due7_n = 0
                progress_n = 0
                highprio_n = 0
                immediate: list[dict[str, Any]] = []
                due_next_7: list[dict[str, Any]] = []
                top_priorities: list[dict[str, Any]] = []
                involved: list[dict[str, Any]] = []
                in_development: list[dict[str, Any]] = []
                week_results: list[dict[str, Any]] = []
                for r in open_rows:
                    st_txt = str(r.get("Estado") or "").strip()
                    st_low = st_txt.lower()
                    prio = str(r.get("Prioridade") or "").strip()
                    prio_low = prio.lower()
                    tid = str(r.get("TaskID") or "").strip()
                    title = str(r.get("Tarefa") or "").strip()
                    project = str(r.get("Projeto") or "").strip()
                    owner = str(r.get("Responsavel") or r.get("responsavel") or "").strip()
                    due = _to_date(r.get("Prazo"))
                    is_overdue = bool(r.get("is_overdue")) or (bool(due) and due < today)
                    is_blocked = "bloque" in st_low
                    in_progress = ("progres" in st_low) or ("curso" in st_low)
                    is_high = prio_low.startswith("alta")
                    days_to_due = None if not due else int((due - today).days)
                    if is_overdue:
                        overdue_n += 1
                    if is_blocked:
                        blocked_n += 1
                    if in_progress:
                        progress_n += 1
                    if is_high:
                        highprio_n += 1
                    if due and 0 <= (due - today).days <= 7:
                        due7_n += 1
                    needs_now = is_overdue or is_blocked or is_high or (days_to_due is not None and days_to_due <= 2)
                    if needs_now:
                        if is_overdue:
                            reason = "Atrasada"
                            sev = 0
                        elif is_blocked:
                            reason = "Bloqueada"
                            sev = 1
                        elif days_to_due is not None and days_to_due <= 2:
                            reason = "Prazo iminente"
                            sev = 2
                        else:
                            reason = "Prioridade alta"
                            sev = 3
                        immediate.append(
                            {
                                "task_id": tid,
                                "title": title,
                                "project": project,
                                "status": st_txt,
                                "priority": prio,
                                "due_date": due.isoformat() if due else "",
                                "days_to_due": days_to_due,
                                "reason": reason,
                                "_sev": sev,
                            }
                        )
                    top_priorities.append(
                        {
                            "task_id": tid,
                            "title": title,
                            "project": project,
                            "status": st_txt,
                            "priority": prio,
                            "due_date": due.isoformat() if due else "",
                            "days_to_due": days_to_due,
                            "_pw": _prio_weight(prio),
                            "_ov": 0 if is_overdue else 1,
                            "_bl": 0 if is_blocked else 1,
                        }
                    )
                    workers = str(r.get("Workers") or r.get("workers") or "").strip()
                    owner_l = owner.lower()
                    if workers and _workers_has_user(workers) and owner_l not in (display_l, username_l):
                        involved.append(
                            {
                                "task_id": tid,
                                "title": title,
                                "project": project,
                                "status": st_txt,
                                "priority": prio,
                                "due_date": due.isoformat() if due else "",
                                "days_to_due": days_to_due,
                                "owner": owner,
                            }
                        )
                    if in_progress:
                        in_development.append(
                            {
                                "task_id": tid,
                                "title": title,
                                "project": project,
                                "status": st_txt,
                                "priority": prio,
                                "due_date": due.isoformat() if due else "",
                                "days_to_due": days_to_due,
                                "owner": owner,
                            }
                        )
                immediate.sort(
                    key=lambda x: (
                        int(x.get("_sev", 9)),
                        9999 if x.get("days_to_due") is None else int(x.get("days_to_due") or 0),
                        str(x.get("due_date") or "9999-12-31"),
                        str(x.get("title") or "").lower(),
                    )
                )
                for it in immediate:
                    it.pop("_sev", None)
                top_priorities.sort(
                    key=lambda x: (
                        int(x.get("_pw", 9)),
                        int(x.get("_ov", 9)),
                        int(x.get("_bl", 9)),
                        9999 if x.get("days_to_due") is None else int(x.get("days_to_due") or 0),
                        str(x.get("due_date") or "9999-12-31"),
                        str(x.get("title") or "").lower(),
                    )
                )
                for it in top_priorities:
                    it.pop("_pw", None)
                    it.pop("_ov", None)
                    it.pop("_bl", None)
                for r in open_rows:
                    due = _to_date(r.get("Prazo"))
                    if not due:
                        continue
                    d = int((due - today).days)
                    if d < 0 or d > 7:
                        continue
                    due_next_7.append(
                        {
                            "task_id": str(r.get("TaskID") or "").strip(),
                            "title": str(r.get("Tarefa") or "").strip(),
                            "project": str(r.get("Projeto") or "").strip(),
                            "status": str(r.get("Estado") or "").strip(),
                            "priority": str(r.get("Prioridade") or "").strip(),
                            "due_date": due.isoformat(),
                            "days_to_due": d,
                        }
                    )
                due_next_7.sort(
                    key=lambda x: (
                        int(x.get("days_to_due", 9999)),
                        str(x.get("due_date") or "9999-12-31"),
                        str(x.get("title") or "").lower(),
                    )
                )
                involved.sort(
                    key=lambda x: (
                        9999 if x.get("days_to_due") is None else int(x.get("days_to_due") or 0),
                        str(x.get("due_date") or "9999-12-31"),
                        str(x.get("title") or "").lower(),
                    )
                )
                in_development.sort(
                    key=lambda x: (
                        9999 if x.get("days_to_due") is None else int(x.get("days_to_due") or 0),
                        str(x.get("due_date") or "9999-12-31"),
                        str(x.get("title") or "").lower(),
                    )
                )
                due_week_n = 0
                for r in open_rows:
                    due = _to_date(r.get("Prazo"))
                    if due and due.isocalendar()[:2] == week_key:
                        due_week_n += 1
                week_done_n = 0
                week_open_n = 0
                week_recovered_n = 0
                for r in mine:
                    due = _to_date(r.get("Prazo"))
                    if not due or due.isocalendar()[:2] != week_key:
                        continue
                    st_txt = str(r.get("Estado") or "").strip()
                    is_done = not _is_open(st_txt)
                    was_late = due < today
                    if is_done:
                        week_done_n += 1
                        if was_late:
                            week_recovered_n += 1
                    else:
                        week_open_n += 1
                    week_results.append(
                        {
                            "task_id": str(r.get("TaskID") or "").strip(),
                            "title": str(r.get("Tarefa") or "").strip(),
                            "project": str(r.get("Projeto") or "").strip(),
                            "status": st_txt,
                            "priority": str(r.get("Prioridade") or "").strip(),
                            "due_date": due.isoformat(),
                            "done": bool(is_done),
                        }
                    )
                week_results.sort(
                    key=lambda x: (
                        0 if bool(x.get("done")) else 1,
                        str(x.get("due_date") or "9999-12-31"),
                        str(x.get("title") or "").lower(),
                    )
                )
                week_impact = 0.0
                def _to_num(v: Any) -> float:
                    s = str(v or "").strip().replace("€", "").replace(" ", "")
                    if not s:
                        return 0.0
                    if "," in s and "." in s:
                        if s.rfind(",") > s.rfind("."):
                            s = s.replace(".", "").replace(",", ".")
                        else:
                            s = s.replace(",", "")
                    elif "," in s:
                        s = s.replace(".", "").replace(",", ".")
                    try:
                        return float(s)
                    except Exception:
                        return 0.0
                try:
                    if hasattr(st.db, "list"):
                        ach_rows = st.db.list({}) or []
                        for ar in ach_rows:
                            if not isinstance(ar, dict):
                                continue
                            date_raw = (
                                ar.get("DataRegisto")
                                or ar.get("Data")
                                or ar.get("Date")
                                or ar.get("created_at")
                                or ar.get("ts")
                            )
                            d = _to_date(date_raw)
                            if not d or d.isocalendar()[:2] != week_key:
                                continue
                            val = (
                                ar.get("Impacto")
                                or ar.get("ImpactoEUR")
                                or ar.get("impact")
                                or ar.get("impact_eur")
                                or ar.get("Valor")
                                or ar.get("ValorEUR")
                                or ar.get("ValorEconomico")
                                or 0
                            )
                            week_impact += _to_num(val)
                except Exception:
                    week_impact = 0.0
                dev_progress_pct = int(round((progress_n * 100.0) / max(1, len(open_rows))))
                return self.json(
                    {
                        "kpis": {
                            "my_tasks": len(open_rows),
                            "overdue": overdue_n,
                            "blocked": blocked_n,
                            "due_7d": due7_n,
                            "in_progress": progress_n,
                            "high_priority_open": highprio_n,
                            "due_this_week": due_week_n,
                        },
                        "immediate": immediate[:12],
                        "due_next_7": due_next_7[:5],
                        "top_priorities": top_priorities[:5],
                        "involved": involved[:5],
                        "in_development": in_development[:5],
                        "week_results": week_results[:5],
                        "week_done": week_done_n,
                        "week_open": week_open_n,
                        "week_recovered": week_recovered_n,
                        "week_impact": round(week_impact, 2),
                        "dev_progress_pct": dev_progress_pct,
                        "scope": "user_authenticated",
                        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/auth/users":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _is_admin(st.role):
                    return self.err(403, "Apenas admin")
                limit = int(_q1(q or {}, "limit", "500") or "500")
                limit = max(1, min(2000, limit))
                return self.json({"rows": _auth_users_list(st.db, limit)})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/auth/bindings":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _is_admin(st.role):
                    return self.err(403, "Apenas admin")
                limit = int(_q1(q or {}, "limit", "500") or "500")
                limit = max(1, min(2000, limit))
                return self.json({"rows": _auth_bindings_list(st.db, limit)})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/admin/overview":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _is_admin(st.role):
                    return self.err(403, "Apenas admin")
                return self.json(_admin_overview(st.db, base_mod, st.username, st.role))
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/admin/settings":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _is_admin(st.role):
                    return self.err(403, "Apenas admin")
                db_cfg = getattr(st.db, "cfg", {}) if getattr(st, "db", None) is not None else {}
                return self.json(_admin_settings_get(base_mod, db_cfg))
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/system/lists":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                return self.json({"lists": _admin_lists_get(st.db), "can_edit": bool(_can_edit_role(st.role))})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/admin/archives":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _is_admin(st.role):
                    return self.err(403, "Apenas admin")
                limit = int(_q1(q or {}, "limit", "120") or "120")
                limit = max(1, min(1000, limit))
                return self.json({"rows": st.db.list_archives(limit)})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/admin/sessions":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _is_admin(st.role):
                    return self.err(403, "Apenas admin")
                limit = int(_q1(q or {}, "limit", "120") or "120")
                limit = max(1, min(1000, limit))
                return self.json({"rows": _admin_sessions(st.db, limit)})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/admin/logs":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _is_admin(st.role):
                    return self.err(403, "Apenas admin")
                lines = int(_q1(q or {}, "lines", "160") or "160")
                lines = max(20, min(2000, lines))
                path = base_mod.cache_dir() / "web_ui_local.log"
                return self.json({"lines": _tail_log_lines(path, lines), "path": str(path)})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/system/diagnostics":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                db = getattr(st, "db", None)
                cfg = dict(base_mod.load_config() if db is None else db.cfg)
                cfg["_web_username"] = st.username
                from files_service import resolve_app_root, validate_onedrive_root
                from diagnostic_service import run_diagnostics as run_diag_checks

                root = resolve_app_root(cfg, base_mod.cache_dir)
                ok_od, msg_od = validate_onedrive_root(root) if root else (False, "Pasta OneDrive nao configurada.")
                onedrive = {
                    "valid": ok_od,
                    "message": msg_od,
                    "onedrive_root": root,
                    "needs_setup": not ok_od,
                }
                connect_fn = db.connect if db is not None else None
                out = run_diag_checks(
                    cfg=cfg,
                    user={
                        "username": st.username,
                        "role": st.role,
                        "display_name": getattr(st, "display_name", st.username),
                    },
                    version=base_mod.APP_VERSION,
                    ui_build=APP_VERSION,
                    host=base_mod.DEFAULT_HOST,
                    port=base_mod.DEFAULT_PORT,
                    cache_dir=base_mod.cache_dir(),
                    log_path=base_mod.cache_dir() / "web_ui_local.log",
                    onedrive=onedrive,
                    connect_fn=connect_fn,
                )
                if db is None:
                    out["checks"].insert(
                        0,
                        {"name": "Base de dados", "ok": False, "detail": "DB não inicializada (ver log)", "level": "error"},
                    )
                    out["ok"] = False
                return self.json(out)
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/scheduled/logs":
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                limit = int(_q1(q or {}, "limit", "120") or "120")
                limit = max(1, min(500, limit))
                return self.json({"rows": _scheduled_log_read(st.db, base_mod, limit)})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/scheduled/prefs":
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                username = str(getattr(st, "username", "") or "").strip()
                if not username:
                    return self.err(400, "Utilizador inválido")
                up = _prefs_get(st, username) or {}
                raw = str(up.get("filters_json") or "").strip()
                prefs: dict[str, Any] = {}
                if raw:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        sv = data.get("scheduled_v1")
                        if isinstance(sv, dict):
                            prefs = sv
                return self.json({"prefs": prefs or {}})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/board/prefs":
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                username = str(getattr(st, "username", "") or "").strip()
                if not username:
                    return self.err(400, "Utilizador inválido")
                up = _prefs_get(st, username) or {}
                raw = str(up.get("board_filters_json") or "").strip()
                prefs: dict[str, Any] = {}
                if raw:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        prefs = _board_prefs_normalize(data)
                return self.json({"prefs": prefs or {}})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/board":
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                display = getattr(st, "display_name", st.username) or st.username
                bf = _parse_board_filters(q or {})
                out = st.db.list_board(bf, st.username, display, st.role)
                return self.json(out)
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/projects/gantt-data":
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                projeto = _q1(q or {}, "projeto", "Todos")
                milestone = _q1(q or {}, "milestone", "Todos")
                include_actions = _q1(q or {}, "include_actions", "0") in ("1", "true", "yes", "on")
                display = getattr(st, "display_name", st.username) or st.username
                out = st.db.get_project_gantt_data(
                    projeto, milestone, include_actions, st.username, display, st.role
                )
                return self.json(out)
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        m_gantt_task = re.fullmatch(r"/api/tasks/([^/]+)/gantt-data", p or "")
        if m_gantt_task:
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                tid = unquote(str(m_gantt_task.group(1) or "")).strip()
                display = getattr(st, "display_name", st.username) or st.username
                out = st.db.get_task_gantt_data(tid, st.username, display, st.role)
                return self.json(out)
            except PermissionError as e:
                return self.err(403, str(e) or "Sem permissão")
            except AppError as e:
                msg = str(e or "")
                if "não encontrada" in msg.lower() or "nao encontrada" in msg.lower():
                    return self.err(404, msg or "Tarefa não encontrada")
                return self.err(400, msg or "Pedido inválido")
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        m_task_detail = re.fullmatch(r"/api/tasks/([^/]+)/detail", p or "")
        if m_task_detail:
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                tid = unquote(str(m_task_detail.group(1) or "")).strip()
                display = getattr(st, "display_name", st.username) or st.username
                out = st.db.get_task_detail(tid, st.username, display, st.role) or {}
                if isinstance(out, dict):
                    try:
                        actions, checks = _task_detail_items_sql(st.db, tid)
                        # Fonte oficial para checklist/actions na Web: leitura direta SQL (paridade Desktop).
                        out["actions"] = actions
                        out["checklist"] = checks
                        total = len(actions) + len(checks)
                        done_n = sum(1 for it in (actions + checks) if bool(it.get("done") or it.get("is_done")))
                        out["actions_progress"] = {
                            "done": done_n,
                            "total": total,
                            "percent": int(round((done_n * 100.0) / total)) if total else 0,
                        }
                    except Exception:
                        # Fallback seguro: manter payload original do backend base.
                        pass
                    # Paridade Desktop: nunca elevar permissões; apenas refinar o can_edit do backend.
                    out["can_edit"] = bool(out.get("can_edit")) and bool(_can_edit_role(st.role))
                return self.json(out if isinstance(out, dict) else {})
            except PermissionError as e:
                return self.err(403, str(e) or "Sem permissão")
            except AppError as e:
                msg = str(e or "")
                if "não encontrada" in msg.lower() or "nao encontrada" in msg.lower():
                    return self.err(404, msg or "Tarefa não encontrada")
                return self.err(400, msg or "Pedido inválido")
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        m_action_deps = re.fullmatch(r"/api/actions/(\d+)/deps", p or "")
        if m_action_deps:
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                aid = int(str(m_action_deps.group(1) or "0"))
                deps = _action_deps_get_sql(st.db, aid)
                return self.json({"action_id": aid, "deps": deps})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/tasks/extras":
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                return self.json(_tasks_extras_payload(st, q or {}, base_mod))
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        return _orig_get(self)

    def do_POST(self):  # noqa: N802
        p, _q = parse_path(self.path)
        token, sess = _session_get_from_headers(getattr(self, "headers", None))
        st_obj = base_mod.STATE
        if p == "/api/auth/login":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                mode = str(payload.get("mode") or "pc").strip().lower()
                machine = ""
                if str(payload.get("machine") or "").strip():
                    machine = _auth_bindings_validate_machine(str(payload.get("machine") or ""))
                username = str(payload.get("username") or "").strip()
                role_hint = str(payload.get("role_hint") or "read")
                password = str(payload.get("password") or "")
                if mode == "pc":
                    if not machine:
                        return self.err(400, "Máquina/PC em falta")
                    bind = _auth_machine_binding(st.db, machine)
                    if not bind or not bind.get("username"):
                        return self.err(403, "Este PC não tem utilizador associado")
                    if not bool(bind.get("binding_active", True)):
                        return self.err(403, "Binding do PC inativo")
                    username = str(bind.get("username") or "")
                    u = _auth_resolve_user(st.db, username, role_hint=role_hint, require_exists=True)
                elif mode == "pass":
                    user_in = username or "admin"
                    if user_in.strip().lower() != "admin":
                        return self.err(403, "Login por password apenas para admin")
                    if not _check_admin_password(st.db.cfg or {}, password):
                        return self.err(403, "Password inválida")
                    u = _auth_resolve_user(st.db, "admin", role_hint="admin", require_exists=True)
                    u["role"] = "admin"
                else:
                    user_in = _auth_normalize_login_username(username)
                    u = _auth_resolve_user(st.db, user_in, role_hint=role_hint, require_exists=True)
                if not bool(u.get("active")):
                    return self.err(403, "Utilizador inativo")
                tok, sess_new = _session_create(str(u["username"]), str(u["display_name"]), str(u["role"]), machine=machine)
                if machine:
                    _auth_origin_write(base_mod, machine=machine, username=str(u.get("username") or ""), source="login")
                _session_db_start(st.db, str(sess_new.get("session_id") or ""), str(sess_new.get("display_name") or u["username"]), machine)
                cookie = f"{_SESSION_COOKIE}={tok}; Path=/; SameSite=Lax"
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Set-Cookie", cookie)
                self.end_headers()
                body = json.dumps(
                    {
                        "authenticated": True,
                        "user": {
                            "username": sess_new["username"],
                            "display_name": sess_new["display_name"],
                            "role": sess_new["role"],
                        },
                        "user_machine": machine,
                        "idle_sec": _SESSION_IDLE_SEC,
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                self.wfile.write(body)
                return
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/auth/logout":
            try:
                if sess and st_obj is not None and getattr(st_obj, "db", None) is not None and sess.get("session_id"):
                    _session_db_end(st_obj.db, str(sess.get("session_id")))
                _session_destroy(token)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Set-Cookie", f"{_SESSION_COOKIE}=; Path=/; Max-Age=0; SameSite=Lax")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8"))
                return
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p.startswith("/api/") and p not in ("/api/auth/login", "/api/auth/logout") and not sess:
            return self.err(401, "Sessão inválida. Faça login.")
        if sess and st_obj is not None:
            try:
                st_obj.username = str(sess.get("username") or "")
                st_obj.display_name = str(sess.get("display_name") or st_obj.username)
                st_obj.role = _normalize_role(str(sess.get("role") or "read"))
                last_touch = float(sess.get("last_db_touch") or 0.0)
                now_t = time.time()
                if (now_t - last_touch) >= 30.0 and getattr(st_obj, "db", None) is not None and sess.get("session_id"):
                    _session_db_touch(st_obj.db, str(sess.get("session_id")))
                    sess["last_db_touch"] = now_t
            except Exception:
                pass
        m_task_att_upload = re.fullmatch(r"/api/tasks/([^/]+)/attachments/upload", p or "")
        if m_task_att_upload:
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                tid = unquote(str(m_task_att_upload.group(1) or "")).strip()
                if not tid:
                    return self.err(400, "TaskID em falta")
                ct = str((getattr(self, "headers", None) and self.headers.get("Content-Type")) or "").strip()
                if "multipart/form-data" not in ct.lower():
                    return self.err(400, "Content-Type inválido (esperado multipart/form-data)")
                try:
                    clen = int(str((getattr(self, "headers", None) and self.headers.get("Content-Length")) or "0"))
                except Exception:
                    clen = 0
                if clen <= 0:
                    return self.err(400, "Body multipart vazio")
                if clen > (300 * 1024 * 1024):
                    return self.err(413, "Upload excede o limite do servidor (300 MB)")
                raw = self.rfile.read(clen)
                filename, data = _parse_multipart_first_file(ct, raw)
                display = getattr(st, "display_name", st.username) or st.username
                pasta = ""
                try:
                    detail = st.db.get_task_detail(tid, st.username, display, st.role) or {}
                    task = detail.get("task") if isinstance(detail, dict) else {}
                    if isinstance(task, dict):
                        pasta = str(task.get("Pasta") or "").strip()
                except Exception:
                    pasta = ""
                item = st.db.attachments_upload_bytes(
                    task_id=tid,
                    filename=filename,
                    data=data,
                    username=st.username,
                    display=display,
                    role=st.role,
                    pasta_field=pasta,
                )
                return self.json({"ok": True, "item": item})
            except PermissionError as e:
                return self.err(403, str(e) or "Sem permissão")
            except AppError as e:
                return self.err(400, str(e) or "Upload inválido")
            except ValueError as e:
                return self.err(400, str(e) or "Multipart inválido")
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/tasks/columns/prefs":
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                username = str(getattr(st, "username", "") or "").strip()
                if not username:
                    return self.err(400, "Utilizador inválido")
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                allowed = _task_cols_defaults()
                cols = _task_cols_normalize(payload.get("columns") or [], allowed)
                if not cols:
                    return self.err(400, "Tem de manter pelo menos uma coluna visível")
                current = _prefs_get(st, username) or {}
                vis_widths: dict[str, Any] = {}
                try:
                    cur_vis = json.loads(str(current.get("visible_columns_json") or "") or "{}")
                    if isinstance(cur_vis, dict) and isinstance(cur_vis.get("widths"), dict):
                        vis_widths = dict(cur_vis.get("widths") or {})
                except Exception:
                    vis_widths = {}
                raw_widths = payload.get("widths")
                if isinstance(raw_widths, dict):
                    for k, v in raw_widths.items():
                        kk = str(k or "").strip()
                        if kk not in allowed:
                            continue
                        try:
                            w = int(v)
                            if 40 <= w <= 900:
                                vis_widths[kk] = w
                        except Exception:
                            pass
                vis_widths = {k: vis_widths[k] for k in cols if k in vis_widths}
                try:
                    font_size = int(current.get("font_size") or 10)
                except Exception:
                    font_size = 10
                prefs_payload = {
                    "theme": current.get("theme") or (getattr(st.db, "cfg", {}) or {}).get("theme"),
                    "view_density": current.get("view_density") or (getattr(st.db, "cfg", {}) or {}).get("view_density"),
                    "font_size": font_size,
                    "zebra_intensity": current.get("zebra_intensity") or (getattr(st.db, "cfg", {}) or {}).get("zebra_intensity"),
                    "show_statusbar": bool(current.get("show_statusbar", True)),
                    "accent_theme": current.get("accent_theme") or (getattr(st.db, "cfg", {}) or {}).get("accent_theme"),
                    "filters_json": current.get("filters_json"),
                    "col_layout_json": current.get("col_layout_json"),
                    "visible_columns_json": json.dumps({"columns": cols, "widths": vis_widths}, ensure_ascii=False),
                    "window_states_json": current.get("window_states_json"),
                    "board_filters_json": current.get("board_filters_json"),
                }
                _prefs_upsert(st, username, prefs_payload)
                return self.json({"ok": True, "columns": cols, "widths": vis_widths})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/dashboard/prefs":
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                username = str(getattr(st, "username", "") or "").strip()
                if not username:
                    return self.err(400, "Utilizador inválido")
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                current = _prefs_get(st, username) or {}
                raw = str(current.get("filters_json") or "").strip()
                all_filters = {}
                if raw:
                    try:
                        obj = json.loads(raw)
                        if isinstance(obj, dict):
                            all_filters = obj
                    except Exception:
                        all_filters = {}
                all_filters["dashboard_v1"] = {
                    "mode": str(payload.get("mode") or "executivo"),
                    "estado": str(payload.get("estado") or "Todos"),
                    "prioridade": str(payload.get("prioridade") or "Todos"),
                    "responsavel": str(payload.get("responsavel") or "Todos"),
                    "projeto": str(payload.get("projeto") or "Todos"),
                    "only_open": bool(payload.get("only_open", False)),
                }
                try:
                    font_size = int(current.get("font_size") or 10)
                except Exception:
                    font_size = 10
                prefs_payload = {
                    "theme": current.get("theme") or (getattr(st.db, "cfg", {}) or {}).get("theme"),
                    "view_density": current.get("view_density") or (getattr(st.db, "cfg", {}) or {}).get("view_density"),
                    "font_size": font_size,
                    "zebra_intensity": current.get("zebra_intensity") or (getattr(st.db, "cfg", {}) or {}).get("zebra_intensity"),
                    "show_statusbar": bool(current.get("show_statusbar", True)),
                    "accent_theme": current.get("accent_theme") or (getattr(st.db, "cfg", {}) or {}).get("accent_theme"),
                    "filters_json": json.dumps(all_filters, ensure_ascii=False),
                    "col_layout_json": current.get("col_layout_json"),
                    "visible_columns_json": current.get("visible_columns_json"),
                    "window_states_json": current.get("window_states_json"),
                    "board_filters_json": current.get("board_filters_json"),
                }
                _prefs_upsert(st, username, prefs_payload)
                return self.json({"ok": True})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/projects/planning/update":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _can_edit_role(st.role):
                    return self.err(403, "Sem permissões para editar planeamento")
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                updates = payload.get("updates")
                if not isinstance(updates, list):
                    updates = []
                display = getattr(st, "display_name", st.username) or st.username
                out = _planning_update_tasks(st.db, st.username, display, st.role, updates)
                return self.json(out)
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        m_action_gantt = re.fullmatch(r"/api/actions/(\d+)/gantt-update", p or "")
        if m_action_gantt:
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                aid = int(str(m_action_gantt.group(1) or "0"))
                start_s = str(payload.get("start_date") or payload.get("start") or "").strip()[:10]
                due_s = str(payload.get("due_date") or payload.get("end") or "").strip()[:10]
                display = getattr(st, "display_name", st.username) or st.username
                out = st.db.update_action_gantt_dates(aid, start_s, due_s, st.username, display, st.role)
                return self.json(out)
            except PermissionError as e:
                return self.err(403, str(e) or "Sem permissão")
            except AppError as e:
                msg = str(e or "")
                if "não encontrada" in msg.lower() or "nao encontrada" in msg.lower():
                    return self.err(404, msg or "Ação não encontrada")
                return self.err(400, msg or "Pedido inválido")
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        m_action_deps = re.fullmatch(r"/api/actions/(\d+)/deps", p or "")
        if m_action_deps:
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                role = _normalize_role(str(getattr(st, "role", "") or ""))
                if not _can_edit_role(role):
                    return self.err(403, "Sem permissões para editar dependências")
                aid = int(str(m_action_deps.group(1) or "0"))
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                deps = payload.get("deps")
                if not isinstance(deps, list):
                    deps = []
                out = _action_deps_set_sql(st.db, aid, deps)
                return self.json(out)
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/auth/users/role":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _is_admin(st.role):
                    return self.err(403, "Apenas admin")
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                username = str(payload.get("username") or "").strip()
                role = str(payload.get("role") or "read").strip().lower()
                active = bool(payload.get("active", True))
                _auth_users_update(st.db, username, display_name=None, role=role, active=active, updated_by=st.username)
                return self.json({"ok": True})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/auth/users":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _is_admin(st.role):
                    return self.err(403, "Apenas admin")
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                out = _auth_users_create(
                    st.db,
                    username=str(payload.get("username") or ""),
                    display_name=str(payload.get("display_name") or ""),
                    role=str(payload.get("role") or "read"),
                    active=bool(payload.get("active", True)),
                    created_by=st.username,
                    windows_account=str(payload.get("windows_account") or ""),
                    primary_machine=str(payload.get("primary_machine") or ""),
                )
                return self.json({"ok": True, **out})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/auth/users/update":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _is_admin(st.role):
                    return self.err(403, "Apenas admin")
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                _auth_users_update(
                    st.db,
                    username=str(payload.get("username") or ""),
                    display_name=str(payload.get("display_name") or ""),
                    role=str(payload.get("role") or "read"),
                    active=bool(payload.get("active", True)),
                    updated_by=st.username,
                    windows_account=str(payload.get("windows_account") or ""),
                    primary_machine=str(payload.get("primary_machine") or ""),
                )
                return self.json({"ok": True})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/auth/users/delete":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _is_admin(st.role):
                    return self.err(403, "Apenas admin")
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                _auth_users_delete(
                    st.db,
                    username=str(payload.get("username") or ""),
                    deleted_by=st.username,
                    current_user=st.username,
                )
                return self.json({"ok": True})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/auth/bindings":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _is_admin(st.role):
                    return self.err(403, "Apenas admin")
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                out = _auth_bindings_create(
                    st.db,
                    machine=str(payload.get("machine") or ""),
                    username=str(payload.get("username") or ""),
                    active=bool(payload.get("active", True)),
                    created_by=st.username,
                )
                return self.json({"ok": True, **out})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/auth/bindings/update":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _is_admin(st.role):
                    return self.err(403, "Apenas admin")
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                _auth_bindings_update(
                    st.db,
                    machine=str(payload.get("machine") or ""),
                    username=str(payload.get("username") or ""),
                    active=bool(payload.get("active", True)),
                    updated_by=st.username,
                )
                return self.json({"ok": True})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/auth/bindings/delete":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _is_admin(st.role):
                    return self.err(403, "Apenas admin")
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                _auth_bindings_delete(
                    st.db,
                    machine=str(payload.get("machine") or ""),
                    deleted_by=st.username,
                )
                return self.json({"ok": True})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/admin/settings/password":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _is_admin(st.role):
                    return self.err(403, "Apenas admin")
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                out = _admin_settings_set_password(str(payload.get("password") or ""))
                try:
                    if isinstance(getattr(st.db, "cfg", None), dict):
                        st.db.cfg["admin_password_sha256"] = str(_project_cache_config_read().get("admin_password_sha256") or "")
                except Exception:
                    pass
                return self.json(out)
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/admin/settings/emojis":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _is_admin(st.role):
                    return self.err(403, "Apenas admin")
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                out = _admin_settings_set_emojis(payload)
                try:
                    if isinstance(getattr(st.db, "cfg", None), dict):
                        st.db.cfg.update({k: out.get(k) for k in ("emoji_bloqueado", "emoji_new", "emoji_atraso")})
                except Exception:
                    pass
                return self.json(out)
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/system/lists":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _can_edit_role(st.role):
                    return self.err(403, "Sem permissões para editar listas")
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                out = _admin_lists_save(st.db, payload.get("lists") or {})
                return self.json(out)
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/admin/archives/restore":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _is_admin(st.role):
                    return self.err(403, "Apenas admin")
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                archive_id = int(payload.get("archive_id") or 0)
                if archive_id <= 0:
                    return self.err(400, "archive_id inválido")
                display = getattr(st, "display_name", st.username) or st.username
                msg = st.db.restore_archive(archive_id, st.username, display, st.role)
                return self.json({"ok": True, "message": msg})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/admin/archives/delete":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _is_admin(st.role):
                    return self.err(403, "Apenas admin")
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                archive_id = int(payload.get("archive_id") or 0)
                if archive_id <= 0:
                    return self.err(400, "archive_id inválido")
                st.db.delete_archive(archive_id, st.role)
                return self.json({"ok": True})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/admin/maintenance/backup":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _is_admin(st.role):
                    return self.err(403, "Apenas admin")
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                out_path = base_mod.cache_dir() / f"admin_backup_{stamp}.json"
                payload = {
                    "overview": _admin_overview(st.db, base_mod, st.username, st.role),
                    "archives": st.db.list_archives(300),
                }
                out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                return self.json({"ok": True, "path": str(out_path), "message": f"Backup criado: {out_path.name}"})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/admin/maintenance/cleanup":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                if not _is_admin(st.role):
                    return self.err(403, "Apenas admin")
                _scheduled_log_clear(st.db)
                log_path = base_mod.cache_dir() / "web_ui_local.log"
                kept = 0
                if log_path.exists():
                    lines = _tail_log_lines(log_path, 1500)
                    kept = len(lines)
                    log_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
                return self.json({"ok": True, "message": f"Limpeza concluída (log linhas: {kept})"})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/scheduled/logs/clear":
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                _scheduled_log_clear(st.db)
                return self.json({"ok": True})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/scheduled/prefs":
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                username = str(getattr(st, "username", "") or "").strip()
                if not username:
                    return self.err(400, "Utilizador inválido")
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                current = _prefs_get(st, username) or {}
                raw = str(current.get("filters_json") or "").strip()
                all_filters: dict[str, Any] = {}
                if raw:
                    try:
                        obj = json.loads(raw)
                        if isinstance(obj, dict):
                            all_filters = obj
                    except Exception:
                        all_filters = {}
                all_filters["scheduled_v1"] = {
                    "q": str(payload.get("q") or ""),
                    "pending": bool(payload.get("pending")),
                    "active": bool(payload.get("active", True)),
                    "failed": bool(payload.get("failed")),
                    "rec": str(payload.get("rec") or "Todos"),
                    "mode": str(payload.get("mode") or "Todos"),
                }
                try:
                    font_size = int(current.get("font_size") or 10)
                except Exception:
                    font_size = 10
                prefs_payload = {
                    "theme": current.get("theme") or (getattr(st.db, "cfg", {}) or {}).get("theme"),
                    "view_density": current.get("view_density") or (getattr(st.db, "cfg", {}) or {}).get("view_density"),
                    "font_size": font_size,
                    "zebra_intensity": current.get("zebra_intensity") or (getattr(st.db, "cfg", {}) or {}).get("zebra_intensity"),
                    "show_statusbar": bool(current.get("show_statusbar", True)),
                    "accent_theme": current.get("accent_theme") or (getattr(st.db, "cfg", {}) or {}).get("accent_theme"),
                    "filters_json": json.dumps(all_filters, ensure_ascii=False),
                    "col_layout_json": current.get("col_layout_json"),
                    "visible_columns_json": current.get("visible_columns_json"),
                    "window_states_json": current.get("window_states_json"),
                    "board_filters_json": current.get("board_filters_json"),
                }
                _prefs_upsert(st, username, prefs_payload)
                return self.json({"ok": True})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/board/prefs":
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                username = str(getattr(st, "username", "") or "").strip()
                if not username:
                    return self.err(400, "Utilizador inválido")
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                current = _prefs_get(st, username) or {}
                try:
                    font_size = int(current.get("font_size") or 10)
                except Exception:
                    font_size = 10
                prefs_payload = {
                    "theme": current.get("theme") or (getattr(st.db, "cfg", {}) or {}).get("theme"),
                    "view_density": current.get("view_density") or (getattr(st.db, "cfg", {}) or {}).get("view_density"),
                    "font_size": font_size,
                    "zebra_intensity": current.get("zebra_intensity") or (getattr(st.db, "cfg", {}) or {}).get("zebra_intensity"),
                    "show_statusbar": bool(current.get("show_statusbar", True)),
                    "accent_theme": current.get("accent_theme") or (getattr(st.db, "cfg", {}) or {}).get("accent_theme"),
                    "filters_json": current.get("filters_json"),
                    "col_layout_json": current.get("col_layout_json"),
                    "visible_columns_json": current.get("visible_columns_json"),
                    "window_states_json": current.get("window_states_json"),
                    "board_filters_json": json.dumps(_board_prefs_normalize(payload), ensure_ascii=False),
                }
                _prefs_upsert(st, username, prefs_payload)
                return self.json({"ok": True})
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/scheduled/preview":
            try:
                st = base_mod.STATE
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                out = st.db.preview_scheduled(payload, st.username, st.role)
                return self.json(out)
            except AppError as e:
                return self.err(400, str(e))
            except PermissionError as e:
                return self.err(403, str(e))
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        m_mc_field = re.fullmatch(r"/api/machines/([^/]+)/fields/([^/]+)/(submit|approve|revert)", p or "")
        if m_mc_field:
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                mid = unquote(str(m_mc_field.group(1) or "")).strip()
                field = unquote(str(m_mc_field.group(2) or "")).strip()
                action = str(m_mc_field.group(3) or "").strip().lower()
                display = getattr(st, "display_name", st.username) or st.username
                if action == "submit":
                    item = st.db.submit_machine_field(mid, field, st.role, st.username, display)
                elif action == "approve":
                    item = st.db.approve_machine_field(mid, field, st.role, st.username, display)
                else:
                    item = st.db.revert_machine_field(mid, field, st.role, st.username, display)
                return self.json({"ok": True, "item": item})
            except PermissionError as e:
                return self.err(403, str(e) or "Sem permissão")
            except AppError as e:
                return self.err(400, str(e) or "Pedido inválido")
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/folders/onedrive/pick":
            try:
                st = base_mod.STATE
                if st is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                initial = str(payload.get("initial") or payload.get("path") or "").strip()
                title = str(payload.get("title") or "Configurar OneDrive — 06 Pasta da App").strip()
                chosen = base_mod.pick_folder_dialog(title, initial)
                if not chosen:
                    return self.err(400, "Selecao cancelada")
                from files_service import validate_onedrive_root

                ok, msg = validate_onedrive_root(chosen)
                return self.json(
                    {
                        "path": chosen,
                        "onedrive_root": chosen,
                        "ok": True,
                        "valid": ok,
                        "message": msg,
                    }
                )
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        return _orig_post(self)

    def read_multipart(self):  # noqa: N802
        return _read_multipart_no_cgi(self)

    Handler.read_multipart = read_multipart  # type: ignore[method-assign]
    Handler.do_GET = do_GET  # type: ignore[method-assign]
    Handler.do_POST = do_POST  # type: ignore[method-assign]


def _patch_handler_notes_put(Handler, STATE, parse_path, base_mod):
    _orig_put = Handler.do_PUT

    def do_PUT(self):  # noqa: N802
        p, _ = parse_path(self.path)
        m_action = re.fullmatch(r"/api/actions/(\d+)", p or "")
        if m_action:
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                role = _normalize_role(str(getattr(st, "role", "") or ""))
                if not _can_edit_role(role):
                    return self.err(403, "Sem permissões para editar ações")
                aid = int(str(m_action.group(1) or "0"))
                if aid <= 0:
                    return self.err(400, "Ação inválida")
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                # Garantir apenas campos suportados no update_action
                allowed = {
                    "item_text",
                    "owner",
                    "status",
                    "start_date",
                    "due_date",
                    "workers",
                    "evidence",
                    "blocked_reason",
                    "action_notes",
                }
                patch = {k: payload.get(k) for k in allowed if k in payload}
                if "item_text" in patch:
                    txt = str(patch.get("item_text") or "").strip()
                    if not txt:
                        return self.err(400, "Texto da ação é obrigatório")
                    patch["item_text"] = txt
                action_notes = str(patch.pop("action_notes", "") or "").strip()
                display = getattr(st, "display_name", st.username) or st.username
                out = st.db.update_action(aid, patch, st.username, display, st.role)
                if ("action_notes" in payload) or action_notes:
                    with st.db.connect() as conn:
                        cur = conn.cursor()
                        try:
                            cur.execute(
                                """
                                IF COL_LENGTH('dbo.task_checklist','action_notes') IS NULL
                                    ALTER TABLE dbo.task_checklist ADD action_notes NVARCHAR(MAX) NULL;
                                """
                            )
                        except Exception:
                            pass
                        try:
                            cur.execute("UPDATE dbo.task_checklist SET action_notes=? WHERE id=?;", (action_notes, aid))
                        except Exception:
                            cur.execute("UPDATE task_checklist SET action_notes=? WHERE id=?;", (action_notes, aid))
                        conn.commit()
                return self.json(out if isinstance(out, dict) else {"ok": True, "id": aid})
            except PermissionError as e:
                return self.err(403, str(e) or "Sem permissão")
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        m_mc = re.fullmatch(r"/api/machines/([^/]+)", p or "")
        if m_mc:
            try:
                st = base_mod.STATE
                if st is None or getattr(st, "db", None) is None:
                    return self.err(503, "Servidor a inicializar — aguarde alguns segundos")
                mid = unquote(str(m_mc.group(1) or "")).strip()
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                display = getattr(st, "display_name", st.username) or st.username
                item = st.db.update_machine(mid, payload, st.role, st.username, display)
                return self.json({"ok": True, "item": item})
            except PermissionError as e:
                return self.err(403, str(e) or "Sem permissão")
            except base_mod.AppError as e:
                return self.err(400, str(e) or "Pedido inválido")
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        if p == "/api/notes":
            try:
                st = base_mod.STATE
                payload = self.read()
                if not isinstance(payload, dict):
                    payload = {}
                content = str(payload.get("content") or "")
                out = st.db.save_user_notes(st.username, content)
                return self.json(out)
            except Exception as e:
                traceback.print_exc()
                return self.err(500, str(e))
        return _orig_put(self)

    Handler.do_PUT = do_PUT  # type: ignore[method-assign]


def _patch_task_filters(base_mod: Any) -> None:
    _orig = base_mod.task_filters

    def task_filters(qs):  # noqa: N802
        f = dict(_orig(qs) or {})
        one = lambda k, d="": str(((qs.get(k) or [d])[0] or d)).strip()
        f["show_done"] = one("show_done") in ("1", "true", "True")
        return f

    base_mod.task_filters = task_filters  # type: ignore[attr-defined]


def _patch_load_config(base_mod: Any) -> None:
    """Garante que o .exe encontra config.json do projeto (credenciais SQL)."""
    import json

    _orig = base_mod.load_config

    def _merge_file(cfg: dict, path: Path) -> dict:
        if not path.is_file():
            return cfg
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return cfg
            cfg.update(data)
            if isinstance(data.get("sqlserver"), dict):
                ss = dict((base_mod.DEFAULT_CONFIG or {}).get("sqlserver") or {})
                ss.update(data["sqlserver"])
                cfg["sqlserver"] = ss
            for key in ("onedrive_app_root", "onedrive_app_folder"):
                val = str(data.get(key) or "").strip()
                if val:
                    cfg[key] = val
            folder = str(cfg.get("onedrive_app_folder") or cfg.get("onedrive_app_root") or "").strip()
            if folder:
                cfg["onedrive_app_folder"] = folder
        except Exception as ex:
            base_mod.log(f"Aviso: config extra {path}: {ex}")
        return cfg

    def _extra_config_paths() -> list[Path]:
        paths: list[Path] = [_BASE_DIR / "AppEngenhariaCache" / "config.json"]
        if getattr(sys, "frozen", False):
            paths.append(
                Path(sys.executable).resolve().parent.parent / "AppEngenhariaCache" / "config.json"
            )
        seen: set[str] = set()
        out: list[Path] = []
        for p in paths:
            key = str(p).lower()
            if key not in seen:
                seen.add(key)
                out.append(p)
        return out

    def load_config():  # noqa: N802
        cfg = _orig()
        for path in _extra_config_paths():
            if path.is_file():
                cfg = _merge_file(cfg, path)
                base_mod.log(f"Config do projeto aplicada: {path}")
                break
        return cfg

    base_mod.load_config = load_config  # type: ignore[method-assign]


def _apply_patches(mod: Any) -> None:
    mod.APP_VERSION = APP_VERSION
    mod.HTML = _patch_html(mod.HTML)
    _patch_load_config(mod)
    _patch_task_filters(mod)
    _patch_pick_folder_dialog(mod)
    _patch_database(mod.Database, mod)
    _patch_handler(mod.Handler, mod.STATE, mod.parse_path, mod.AppError, PermissionError, mod)
    _patch_handler_notes_put(mod.Handler, mod.STATE, mod.parse_path, mod)


_base = _load_base()
_apply_patches(_base)

# Re-exportar símbolos usados externamente / pelo arranque
run = _base.run
parse_args = _base.parse_args
Handler = _base.Handler
Database = _base.Database
HTML = _base.HTML

for _name in (
    "APP_NAME", "DEFAULT_HOST", "DEFAULT_PORT", "STATE", "ScheduledService",
    "BoardService", "DashboardService", "ProjectService", "SystemService",
):
    if hasattr(_base, _name):
        globals()[_name] = getattr(_base, _name)


if __name__ == "__main__":
    run()
