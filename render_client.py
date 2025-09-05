import os
import requests
from dotenv import load_dotenv

load_dotenv()

RENDER_API_KEY = os.getenv("RENDER_API_KEY")

url = "https://api.render.com/v1/services"
headers = {"Authorization": f"Bearer {RENDER_API_KEY}"}

response = requests.get(url, headers=headers)

print("Código de respuesta:", response.status_code)
print(response.json())
