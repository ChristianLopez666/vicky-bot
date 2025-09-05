import requests

url = "https://bot-vicky.onrender.com/webhook"

data = {
    "Body": "Hola",
    "From": "whatsapp:+5216681855146"
}

response = requests.post(url, data=data)

print("Status:", response.status_code)
print("Respuesta:", response.text)
