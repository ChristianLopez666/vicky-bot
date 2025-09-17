# ==========================
# Script de despliegue Vicky Bot
# ==========================

# Ir a la carpeta del proyecto
Set-Location "C:\Users\chris\Downloads\bot-vicky"

# Preparar cambios
git add .

# Crear commit con fecha y hora automática
$fecha = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
git commit -m "Auto-deploy Vicky Bot - $fecha"

# Subir cambios a GitHub (rama main)
git push origin main
