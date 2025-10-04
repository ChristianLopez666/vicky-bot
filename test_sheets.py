# test_sheets.py  — PRUEBA NO INTRUSIVA
from integrations_sheets import buscar_cliente_por_whatsapp
from config_env import GOOGLE_CREDENTIALS_JSON, GOOGLE_SHEET_ID

def log(msg): 
    print(f"[TEST] {msg}")

if __name__ == "__main__":
    log("Verificando variables de entorno…")
    print("GOOGLE_CREDENTIALS_JSON:", "OK (presente)" if GOOGLE_CREDENTIALS_JSON else "FALTA")
    print("GOOGLE_SHEET_ID:", GOOGLE_SHEET_ID if GOOGLE_SHEET_ID else "FALTA")

    # Usa uno de tus números de prueba ya cargados en la hoja (últimos 10 dígitos)
    test_phone = "6681631267"
    log(f"Buscando prospecto por últimos 10 dígitos: {test_phone}")
    result = buscar_cliente_por_whatsapp(test_phone)
    log(f"Resultado: {result if result else 'No encontrado / error en conexión'}")
