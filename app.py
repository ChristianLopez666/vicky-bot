# app.py — Vicky SECOM
# Rutas preservadas: /webhook, /ext/health, /ext/test-send, /ext/send-promo
# Mejora: RAG (Drive Docs/PDF) + envío masivo robusto en background con log a Sheets
# Compatible con core_whatsapp.py si existe (preferido); fallback a HTTP directo.

from __future__ import annotations
import os, io, json, time, logging, threading, re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, request, jsonify

# ==============================
# Logging
# ==============================
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
log = logging.getLogger("vicky")

# ==============================
# Flask app
# ==============================
app = Flask(__name__)

# ==============================
# Entorno
# ==============================
META_TOKEN        = os.getenv("META_TOKEN", "")
WABA_PHONE_ID     = os.getenv("WABA_PHONE_ID", os.getenv("PHONE_NUMBER_ID", ""))
VERIFY_TOKEN      = os.getenv("VERIFY_TOKEN", "")
WA_API_VERSION    = os.getenv("WA_API_VERSION", "v20.0")
WHATSAPP_API_URL  = f"https://graph.facebook.com/{WA_API_VERSION}/{WABA_PHONE_ID}/messages" if WABA_PHONE_ID else None

OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_APIKEY") or os.getenv("OPENAI_API_KEY".lower())
GPT_MODEL         = os.getenv("GPT_MODEL", "gpt-4o-mini")

GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
GSHEET_PROSPECTS_ID     = os.getenv("SHEETS_ID_LEADS", os.getenv("GSHEET_PROSPECTS_ID", ""))
SHEETS_TITLE_LEADS      = os.getenv("SHEETS_TITLE_LEADS", "Prospectos SECOM Auto")

DRIVE_FOLDER_ID          = os.getenv("DRIVE_FOLDER_ID")                 # opcional
MANUALES_VICKY_FOLDER_ID = os.getenv("MANUALES_VICKY_FOLDER_ID")        # opcional

ADVISOR_NUMBER    = os.getenv("ADVISOR_WHATSAPP", os.getenv("ADVISOR_NUMBER", ""))

# ==============================
# WhatsApp helpers
# ==============================
_use_core = False
send_text_impl = None
send_template_impl = None

try:
    # Preferimos usar tus helpers reales si existen
    import core_whatsapp  # type: ignore
    if hasattr(core_whatsapp, "send_text"):
        def _core_send_text(to: str, text: str) -> bool:
            try:
                core_whatsapp.send_text(to, text)  # type: ignore
                return True
            except Exception as e:
                log.exception("core_whatsapp.send_text fallo: %s", e)
                return False
        send_text_impl = _core_send_text
        _use_core = True
    if hasattr(core_whatsapp, "send_template"):
        def _core_send_tpl(to: str, name: str, params: List[str]) -> bool:
            try:
                core_whatsapp.send_template(to, name, params)  # type: ignore
                return True
            except Exception as e:
                log.exception("core_whatsapp.send_template fallo: %s", e)
                return False
        send_template_impl = _core_send_tpl
except Exception:
    _use_core = False

def _http_send_text(to: str, text: str) -> bool:
    if not (WHATSAPP_API_URL and META_TOKEN):
        return False
    payload = {
        "messaging_product":"whatsapp",
        "recipient_type":"individual",
        "to": to,
        "type":"text",
        "text":{"body": text}
    }
    try:
        r = requests.post(WHATSAPP_API_URL, headers={
            "Authorization": f"Bearer {META_TOKEN}",
            "Content-Type": "application/json"
        }, json=payload, timeout=20)
        ok = (200 <= r.status_code < 300)
        if not ok:
            log.warning("WA send_text %s -> %s %s", to, r.status_code, r.text[:200])
        return ok
    except Exception as e:
        log.exception("WA send_text error: %s", e)
        return False

def _http_send_template(to: str, name: str, params: List[str]) -> bool:
    if not (WHATSAPP_API_URL and META_TOKEN):
        return False
    components = []
    if params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in params]
        })
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {"name": name, "language": {"code": "es_MX"}, "components": components}
    }
    try:
        r = requests.post(WHATSAPP_API_URL, headers={
            "Authorization": f"Bearer {META_TOKEN}",
            "Content-Type": "application/json"
        }, json=payload, timeout=20)
        ok = (200 <= r.status_code < 300)
        if not ok:
            log.warning("WA send_template %s -> %s %s", to, r.status_code, r.text[:200])
        return ok
    except Exception as e:
        log.exception("WA send_template error: %s", e)
        return False

def send_message(to: str, text: str) -> bool:
    if send_text_impl:
        return send_text_impl(to, text)
    return _http_send_text(to, text)

def send_template(to: str, name: str, params: List[str]) -> bool:
    if send_template_impl:
        return send_template_impl(to, name, params)
    return _http_send_template(to, name, params)

# ==============================
# Google clients (Drive + Sheets)
# ==============================
from google.oauth2 import service_account  # type: ignore
from googleapiclient.discovery import build  # type: ignore
from googleapiclient.http import MediaIoBaseDownload  # type: ignore

def _google_clients():
    if not GOOGLE_CREDENTIALS_JSON:
        return None, None
    try:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        sheets = build("sheets","v4",credentials=creds)
        drive  = build("drive", "v3", credentials=creds)
        return sheets, drive
    except Exception as e:
        log.exception("Google clients error: %s", e)
        return None, None

# ==============================
# RAG: índice híbrido (BM25 + embeddings OpenAI)
# ==============================
import numpy as np  # type: ignore
from rank_bm25 import BM25Okapi  # type: ignore
try:
    import openai  # type: ignore
except Exception:
    openai = None

VX_TTL = 1800  # 30 min

class RAGIndex:
    def __init__(self):
        self.docs_meta: List[Dict[str,Any]] = []
        self.chunks: List[str] = []
        self.meta: List[Dict[str,Any]] = []
        self.bm25 = None
        self.emb = None
        self.hash = ""
        self.built = 0.0
        self._lock = threading.Lock()

    # ---- utils ----
    @staticmethod
    def _split_chunks(text: str, target_tokens=1000, overlap_tokens=120) -> List[str]:
        approx = target_tokens*4; over = overlap_tokens*4
        res, i, L = [], 0, len(text)
        while i < L:
            j = min(i+approx, L)
            cut = text.rfind("\\n", i, j)
            if cut == -1 or cut <= i + int(0.3*approx): cut = j
            chunk = text[i:cut].strip()
            if chunk: res.append(chunk)
            i = max(cut - over, i+1)
        return res

    @staticmethod
    def _clean_text(t: str) -> str:
        if not t: return ""
        lines = [ln.strip("\\x0c") for ln in t.splitlines()]
        freq, total = {}, max(len(lines),1)
        for ln in lines:
            if ln: freq[ln] = freq.get(ln,0)+1
        out = []
        for ln in lines:
            if ln and len(ln)<=60 and freq.get(ln,0)/total>0.4:
                continue
            out.append(ln)
        return "\\n".join(out).strip()

    @staticmethod
    def _hash(files: List[Dict[str,Any]]) -> str:
        return json.dumps([(f["id"], f.get("modifiedTime",""), f.get("etag","")) for f in files], sort_keys=True)

    def _embed_list(self, texts: List[str]) -> np.ndarray:
        if not (openai and OPENAI_API_KEY):
            raise RuntimeError("Embeddings no disponibles")
        openai.api_key = OPENAI_API_KEY
        vecs = []
        B = 64
        for i in range(0, len(texts), B):
            batch = texts[i:i+B]
            r = openai.Embedding.create(model="text-embedding-3-large", input=batch)
            vecs.extend([np.array(d["embedding"], dtype=np.float32) for d in r["data"]])
        return np.vstack(vecs)

    @staticmethod
    def _cos(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        a = a / (np.linalg.norm(a,axis=1,keepdims=True)+1e-9)
        b = b / (np.linalg.norm(b,axis=1,keepdims=True)+1e-9)
        return a @ b.T

    # ---- build / refresh ----
    def needs_refresh(self, files: List[Dict[str,Any]]) -> bool:
        if time.time() - self.built > VX_TTL: return True
        return self._hash(files) != self.hash

    def rebuild(self, payload: Dict[str,Any]):
        with self._lock:
            self.docs_meta.clear(); self.chunks.clear(); self.meta.clear()
            files = payload.get("files", [])
            for f in files:
                txt = self._clean_text(f.get("text",""))
                if not txt: continue
                chs = self._split_chunks(txt, 1000, 120)
                for i, ch in enumerate(chs, 1):
                    self.chunks.append(ch)
                    self.meta.append({"title": f.get("name","Manual"), "doc_id": f.get("id"), "chunk": i})
                self.docs_meta.append({"title": f.get("name","Manual"), "doc_id": f.get("id"), "modifiedTime": f.get("modifiedTime","")})
            toks = [c.lower().split() for c in self.chunks]
            self.bm25 = BM25Okapi(toks) if toks else None
            self.emb = None
            if openai and OPENAI_API_KEY and self.chunks:
                try:
                    self.emb = self._embed_list(self.chunks)
                except Exception:
                    log.exception("Embeddings fallaron; continuo con BM25")
                    self.emb = None
            self.hash  = self._hash([{"id": f.get("id"), "modifiedTime": f.get("modifiedTime",""), "etag": f.get("etag","")} for f in files])
            self.built = time.time()

    def status(self) -> Dict[str,Any]:
        return {
            "docs": len(self.docs_meta),
            "chunks": len(self.chunks),
            "last_built_iso": datetime.utcfromtimestamp(self.built).isoformat() if self.built else None,
            "embeddings": bool(self.emb is not None),
            "ttl": VX_TTL
        }

    def search(self, q: str, top_k: int = 6) -> List[Tuple[str, Dict[str,Any]]]:
        if not q.strip() or not self.chunks: return []
        nb = {}
        if self.bm25:
            s = self.bm25.get_scores(q.lower().split())
            v = np.array(s, dtype=np.float32)
            z = (v - v.min()) if (v.max()-v.min()<1e-9) else (v - v.min())/(v.max()-v.min())
            nb = {i: float(z[i]) for i in range(len(s))}
        ne = {}
        if self.emb is not None and openai:
            try:
                qv = self._embed_list([q])[0:1,:]
                sims = self._cos(qv, self.emb)[0]
                w = np.array(sims, dtype=np.float32)
                z = (w - w.min()) if (w.max()-w.min()<1e-9) else (w - w.min())/(w.max()-w.min())
                ne = {i: float(z[i]) for i in range(len(sims))}
            except Exception:
                ne = {}
        keys = set(nb.keys()) | set(ne.keys())
        fused = [(i, nb.get(i,0.0)*0.6 + ne.get(i,0.0)*0.4) for i in keys]
        fused.sort(key=lambda x: x[1], reverse=True)
        return [(self.chunks[i], self.meta[i]) for i,_ in fused[:top_k]]

# ---- pull manuals from Drive ----
def _find_manuals_folder_id(drive) -> Optional[str]:
    if MANUALES_VICKY_FOLDER_ID:
        return MANUALES_VICKY_FOLDER_ID
    try:
        q = "name = 'Manuales Vicky' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        r = drive.files().list(q=q, fields="files(id,name)").execute()
        files = r.get("files", [])
        if files: 
            return files[0]["id"]
    except Exception:
        pass
    return DRIVE_FOLDER_ID  # fallback

def _export_text(drive, file_meta: Dict[str,Any]) -> str:
    fid = file_meta["id"]; mime = file_meta.get("mimeType","")
    try:
        if mime == "application/vnd.google-apps.document":
            req = drive.files().export_media(fileId=fid, mimeType="text/plain")
            buf = io.BytesIO(); MediaIoBaseDownload(buf, req).next_chunk()
            return buf.getvalue().decode("utf-8","ignore")
        elif mime == "application/pdf":
            req = drive.files().get_media(fileId=fid)
            buf = io.BytesIO(); done=False
            dl = MediaIoBaseDownload(buf, req)
            while not done:
                _, done = dl.next_chunk()
            data = buf.getvalue()
            # prefer pdfminer; fallback pypdf
            try:
                from pdfminer.high_level import extract_text
                return extract_text(io.BytesIO(data)) or ""
            except Exception:
                from pypdf import PdfReader
                txt = ""
                for p in PdfReader(io.BytesIO(data)).pages:
                    txt += (p.extract_text() or "") + "\\n"
                return txt
        else:
            # intento generico como texto
            try:
                req = drive.files().export_media(fileId=fid, mimeType="text/plain")
                buf = io.BytesIO(); MediaIoBaseDownload(buf, req).next_chunk()
                return buf.getvalue().decode("utf-8","ignore")
            except Exception:
                return ""
    except Exception:
        log.exception("Export error %s", file_meta.get("name"))
        return ""

def _pull_manuals_payload(drive) -> Dict[str,Any]:
    folder_id = _find_manuals_folder_id(drive)
    if not folder_id:
        return {"files": []}
    q = f"'{folder_id}' in parents and trashed = false"
    fields = "files(id,name,mimeType,modifiedTime,etag)"
    r = drive.files().list(q=q, fields=fields).execute()
    files = r.get("files", [])
    out = []
    for f in files:
        text = _export_text(drive, f)
        out.append({"id": f.get("id"), "name": f.get("name"), "mimeType": f.get("mimeType"),
                    "modifiedTime": f.get("modifiedTime",""), "etag": f.get("etag",""),
                    "text": text})
    return {"files": out}

RAG = RAGIndex()

def ensure_rag_index(force: bool=False) -> Dict[str,Any]:
    sheets, drive = _google_clients()
    if not drive:
        return {"ok": False, "reason": "google_not_ready"}
    payload = _pull_manuals_payload(drive)
    if force or RAG.needs_refresh(payload.get("files", [])):
        RAG.rebuild(payload)
    return {"ok": True, "status": RAG.status()}

def answer_with_context(user_query: str) -> str:
    st = ensure_rag_index(False)
    if not st.get("ok"):
        return "En este momento no puedo consultar los manuales. Intenta de nuevo más tarde."
    hits = RAG.search(user_query, top_k=6)
    if not hits:
        return "No encontré información en los manuales para responder con certeza. ¿Puedes reformular tu pregunta o ser más específico?"
    intro = "Aquí tienes la información según los manuales (cita de origen y sección aproximada):\\n\\n"
    parts = []
    for text, meta in hits:
        title = meta.get("title","Manual")
        pg    = meta.get("chunk",1)
        snip  = text.strip()
        if len(snip) > 700: snip = snip[:700].rstrip() + "…"
        parts.append(f"• *{title}* (sección {pg}):\\n{snip}\\n")
    return intro + "\\n".join(parts)

# ==============================
# Promo masivo en background con log a Sheets
# ==============================
def _sheets_append(spreadsheet_id: str, range_a1: str, rows: List[List[Any]]):
    sheets, _ = _google_clients()
    if not (sheets and spreadsheet_id):
        return False
    try:
        body = {"values": rows}
        sheets.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body
        ).execute()
        return True
    except Exception as e:
        log.exception("Sheets append error: %s", e)
        return False

def _send_with_retries(item: Dict[str,Any], max_retries=4) -> Tuple[bool,str]:
    to = (item.get("to") or "").strip()
    tpl = (item.get("template") or "").strip()
    txt = (item.get("text") or "").strip()
    params = item.get("params") or []
    if not to:
        return False, "missing_to"
    attempt = 0
    while attempt <= max_retries:
        attempt += 1
        try:
            if tpl:
                ok = send_template(to, tpl, params)
            else:
                ok = send_message(to, txt or ".")
            if ok:
                return True, ""
        except Exception as e:
            last = str(e)
        # backoff
        sleep_s = min(2**attempt, 20)
        time.sleep(sleep_s)
    return False, last if 'last' in locals() else "failed"

def _bulk_worker(items: List[Dict[str,Any]]):
    ok = fail = 0
    for it in items:
        success, err = _send_with_retries(it)
        status = "sent" if success else "failed"
        ok += 1 if success else 0
        fail += 0 if success else 1
        # log a Sheets
        _sheets_append(GSHEET_PROSPECTS_ID, "PromosLog!A:F", [[
            datetime.utcnow().isoformat(), it.get("to",""), it.get("template") or it.get("text",""),
            status, err, json.dumps({"params": it.get("params",[])}, ensure_ascii=False)
        ]])
        # pace
        time.sleep(0.4)
    if ADVISOR_NUMBER:
        send_message(ADVISOR_NUMBER, f"📊 Resumen envío masivo:\\n• Exitosos: {ok}\\n• Fallidos: {fail}\\n• Total: {len(items)}")

# ==============================
# Rutas
# ==============================
@app.get("/ext/health")
def ext_health():
    rag = RAG.status()
    return jsonify({
        "status": "ok",
        "whatsapp_configured": bool(META_TOKEN and WABA_PHONE_ID),
        "google_ready": bool(GOOGLE_CREDENTIALS_JSON),
        "openai_ready": bool(OPENAI_API_KEY),
        "rag": rag
    }), 200

@app.get("/ext/reindex")
def ext_reindex():
    st = ensure_rag_index(force=True)
    return jsonify({"ok": st.get("ok", False), "rag": RAG.status(), "reason": st.get("reason")}), 200

@app.post("/ext/test-send")
def ext_test_send():
    data = request.get_json(force=True) or {}
    to = str(data.get("to") or ADVISOR_NUMBER or "").strip()
    text = data.get("text","Hola, prueba Vicky SECOM")
    if not to:
        return jsonify({"ok": False, "error": "destino vacío"}), 400
    ok = send_message(to, text)
    return jsonify({"ok": bool(ok), "to": to, "text": text}), 200 if ok else 502

@app.post("/ext/send-promo")
def ext_send_promo():
    body = request.get_json(force=True) or {}
    items = body.get("items", [])
    if not isinstance(items, list) or not items:
        return jsonify({"queued": False, "error": "items vacío"}), 400
    threading.Thread(target=_bulk_worker, args=(items,), daemon=True, name="BulkSendWorker").start()
    return jsonify({"queued": True, "message": f"Procesando {len(items)} mensajes en background", "timestamp": datetime.utcnow().isoformat()}), 202

@app.get("/webhook")
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    chal = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return chal, 200
    return "forbidden", 403

@app.post("/webhook")
def webhook_receive():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        entries = payload.get("entry", [])
        for entry in entries:
            changes = entry.get("changes", [])
            for ch in changes:
                value = ch.get("value", {})
                msgs = value.get("messages", [])
                for m in msgs:
                    if m.get("type") == "text":
                        from_id = m.get("from")
                        text = m.get("text", {}).get("body","").strip()
                        low = text.lower()
                        TRIGGER = ("qué","que","cómo","como","cuándo","cuando","dónde","donde","cuál","cual","consulta:","manual:")
                        KEYS = ("imss","pensión","pension","préstamo","prestamo","seguro","vida","salud","auto","vrim","inbursa","requisitos","documentos","monto","tasa","plazos","ley 73")
                        if ("?" in low or low.startswith(TRIGGER)) and any(k in low for k in KEYS):
                            ans = answer_with_context(text)
                            send_message(from_id, ans)
                        # si no es consulta RAG, no interferimos con tu flujo externo
        return jsonify({"ok": True}), 200
    except Exception as e:
        log.exception("webhook error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 200

# ==============================
# Main
# ==============================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
