import json
from typing import Optional, Dict
from config_env import GOOGLE_CREDENTIALS_JSON, GOOGLE_SHEET_ID
from utils_logger import get_logger

log = get_logger("sheets")

def find_prospect_by_phone_last10(last10: str) -> Optional[Dict[str, str]]:
    try:
        if not GOOGLE_CREDENTIALS_JSON or not GOOGLE_SHEET_ID:
            return None
        import gspread
        from google.oauth2.service_account import Credentials

        creds = json.loads(GOOGLE_CREDENTIALS_JSON)
        scopes = ['https://www.googleapis.com/auth/spreadsheets.readonly']
        credentials = Credentials.from_service_account_info(creds, scopes=scopes)
        gc = gspread.authorize(credentials)
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        ws = sh.sheet1
        for row in ws.get_all_records():
            phone = str(row.get("wa_id") or row.get("telefono") or "")
            if "".join(filter(str.isdigit, phone))[-10:] == last10:
                return {
                    "nombre": str(row.get("nombre") or row.get("name") or "").strip(),
                    "producto": str(row.get("producto") or "").strip(),
                    "nota": str(row.get("nota") or "").strip(),
                }
        return None
    except Exception as e:
        log.exception("Sheets error: %s", e)
        return None
