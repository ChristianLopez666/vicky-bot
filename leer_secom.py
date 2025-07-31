import gspread
import os
import json
from oauth2client.service_account import ServiceAccountCredentials

def buscar_cliente(numero_whatsapp):
    # Autenticación con Google Sheets
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    ruta_credenciales = os.path.join(os.getcwd(), 'credenciales-vicky.json')  # Asegúrate que el nombre coincida
    creds = ServiceAccountCredentials.from_json_keyfile_name(ruta_credenciales, scope)
    client = gspread.authorize(creds)

    # Abrimos la hoja
    hoja = client.open("Prospectos SECOM Auto").sheet1

    # Leemos todos los valores
    datos = hoja.get_all_records()

    # Buscamos coincidencia exacta
    for fila in datos:
        if str(fila['WhatsApp']).strip() == str(numero_whatsapp).strip():
            return {
                'nombre': fila.get('Nombre completo', 'Cliente'),
                'rfc': fila.get('RFC', ''),
                'telefono': fila.get('WhatsApp', '')
            }

    return None
