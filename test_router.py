# test_router.py

from core_router import route_message

# Número de prueba (solo para simular)
TEST_WA_ID = "wamid.TEST"
TEST_PHONE = "5216680000000"

# Casos de prueba
pruebas = {
    "menu": "Prueba 1: Menú inicial",
    "5": "Prueba 2: Opción préstamos",
    "8": "Prueba 3: Contactar con Christian",
    "¿Me puedes explicar cómo funciona la modalidad 40 del IMSS?": "Prueba 4: Consulta libre (GPT)",
    "Necesito un préstamo empresarial urgente": "Prueba 5: GPT caído",
    "######": "Prueba 6: Entrada inválida"
}

print("=== PRUEBAS VICKY BOT ===\n")
for entrada, descripcion in pruebas.items():
    print(f"--- {descripcion} ---")
    respuesta = route_message(TEST_WA_ID, TEST_PHONE, entrada)
    print(f"Usuario: {entrada}")
    print(f"Vicky: {respuesta}\n")
