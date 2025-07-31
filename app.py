from flask import Flask, request, jsonify
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = Flask(__name__)

# Conexión a Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
credentials = ServiceAccountCredentials.from_json_keyfile_name("credenciales_vicky.json", scope)
client = gspread.authorize(credentials)
sheet = client.open("Prospectos SECOM Auto").sheet1
datos = sheet.get_all_records()

def estandarizar_numero(numero):
    return ''.join(filter(str.isdigit, numero))

@app.route('/webhook', methods=['POST'])
def webhook():
    incoming_msg = request.values.get('Body', '').strip()
    from_number = request.values.get('From', '')
    numero_limpio = estandarizar_numero(from_number)

    # Buscar coincidencia en la hoja
    coincidencia = None
    for fila in datos:
        numero_en_hoja = estandarizar_numero(str(fila.get("Número", "")))
        if numero_limpio.endswith(numero_en_hoja[-10:]):
            coincidencia = fila
            break

    if coincidencia:
        nombre_cliente = coincidencia.get("Nombre", "Cliente")
        mensaje = f"""Hola {nombre_cliente} 👋

Tienes un beneficio exclusivo en tu seguro de auto:
✔️ Hasta *60% de descuento*
✔️ Transferible a familiares que vivan en tu mismo domicilio

Aquí tienes nuestro menú de servicios disponibles 👇

1️⃣ Seguro de Auto  
2️⃣ Seguro de Vida y Salud  
3️⃣ Tarjeta Médica VRIM  
4️⃣ Préstamo para Pensionados  
5️⃣ Financiamiento Empresarial  
6️⃣ Nómina Empresarial  
7️⃣ Asesoría en Pensiones  
8️⃣ Contactar con Christian ☎️

Por favor responde con el número de la opción que te interesa 😊"""
    else:
        mensaje = """Hola 👋

Gracias por comunicarte con Vicky, asistente de Christian López.

Aquí tienes nuestro menú de servicios disponibles 👇

1️⃣ Seguro de Auto  
2️⃣ Seguro de Vida y Salud  
3️⃣ Tarjeta Médica VRIM  
4️⃣ Préstamo para Pensionados  
5️⃣ Financiamiento Empresarial  
6️⃣ Nómina Empresarial  
7️⃣ Asesoría en Pensiones  
8️⃣ Contactar con Christian ☎️

Por favor responde con el número de la opción que te interesa 😊"""

    return jsonify({"reply": mensaje})

if __name__ == '__main__':
    app.run(port=5000)
