import requests

# URL local donde está corriendo Flask
URL = "http://127.0.0.1:5000/webhook"

payload = {
  "object": "whatsapp_business_account",
  "entry": [
    {
      "id": "WABA_ID_TEST",
      "changes": [
        {
          "value": {
            "messaging_product": "whatsapp",
            "metadata": {
              "display_phone_number": "521234567890",
              "phone_number_id": "PHONE_NUMBER_ID_TEST"
            },
            "contacts": [
              {"profile": {"name": "Test User"}, "wa_id": "5216681855146"}
            ],
            "messages": [
              {
                "from": "5216681855146",
                "id": "wamid.TEST",
                "timestamp": "1694100000",
                "type": "text",
                "text": {"body": "hola"}
              }
            ]
          },
          "field": "messages"
        }
      ]
    }
  ]
}

r = requests.post(URL, json=payload, timeout=15)
print("Status:", r.status_code)
print("Body:", r.text)
