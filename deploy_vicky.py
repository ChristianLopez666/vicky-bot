import os, requests
from dotenv import load_dotenv

load_dotenv()
API = "https://api.render.com/v1"
KEY = os.getenv("RENDER_API_KEY")
HDR = {"Authorization": f"Bearer {KEY}"}

r = requests.get(f"{API}/services", headers=HDR)

print("Código:", r.status_code)
if r.status_code == 200:
    for svc in r.json():
        print("ID:", svc.get("id"))
        print("Slug:", svc.get("slug"))
        print("Estado:", svc.get("suspended"))
        print("-" * 40)
else:
    print(r.text)
