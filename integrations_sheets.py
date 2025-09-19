# integrations_sheets.py
import os
import json
import re
import gspread
from typing import Optional, Dict, Any
from google.oauth2.service_account import Credentials

# --- Config ---
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",      # lectura/escritura
    "https://www.googleapis.com/auth/drive.readonly"
]

SHEET_ID_SECOM = os.getenv("SHEET_ID_SECOM")  # ID del Google Sheet (env)
SHEET_TITLE_SECOM = os.getenv("SHEET_TITLE_SECOM", "Prospectos SECOM Auto")  # nombre de la pestaña


def _get_gspread_client() -> gspread.Client:
    """Autoriza gspread usando el JSON de service account en GOOGLE_CREDENTIALS_JSON."""
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not raw:
        raise RuntimeError("Falta la variable de entorno GOOGLE_CREDENTIALS_JSON")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON no es un JSON válido (revisar comillas/escape).") from e

    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _open_ws(sheet_id: str, title: str) -> gspread.Worksheet:
    if not sheet_id:
        raise RuntimeError("Falta SHEET_ID_SECOM en variables de entorno.")
    client = _get_gspread_client()
    return client.open_by_key(sheet_id).worksheet(title)


def _only_digits(s: str) -> str:
    return re.sub(r"\D+", "", s or "")


def _last10(phone: str) -> Optional[str]:
    d = _only_digits(phone)
    return d[-10:] if len(d) >= 10 else None


def buscar_cliente_por_whatsapp(wa_number: str) -> Optional[Dict[str, Any]]:
    """
    Busca por WhatsApp (match por últimos 10 dígitos) en la hoja SECOM.
    Retorna un dict con campos útiles o None si no existe.
    """
    ws = _open_ws(SHEET_ID_SECOM, SHEET_TITLE_SECOM)
    registros = ws.get_all_records(default_blank="")

    target = _last10(wa_number)
    if not target:
        return None

    for row in registros:
        tel = _last10(str(row.get("TELEFONO/WHATSAPP", "")))
        if tel and tel == target:
            return {
                "nombre": row.get("NOMBRE", "").strip(),
                "rfc": row.get("RFC", "").strip(),
                "telefono": row.get("TELEFONO/WHATSAPP", "").strip(),
                "estatus": row.get("ESTATUS", "").strip(),
                "producto": row.get("PRODUCTO", "").strip(),
                "ultimo_contacto": row.get("ULTIMO_CONTACTO", "").strip(),
                "beneficio": row.get("BENEFICIO_OFRECIDO", "").strip(),
                "notas": row.get("NOTAS", "").strip(),
            }
    return None
