param(
  [string]$Slug = "",
  [switch]$List
)

$headers = @{ Authorization = "Bearer $env:RENDER_API_KEY" }
$base = "https://api.render.com/v1"

# Listar servicios
if ($List -or [string]::IsNullOrWhiteSpace($Slug)) {
  $svcs = Invoke-RestMethod -Uri "$base/services" -Headers $headers
  $svcs | ForEach-Object { "{0}`t{1}`t{2}" -f $_.id, $_.slug, $_.suspended }
  if ($List) { exit 0 }
  Write-Host "`nUsa: .\render_deploy.ps1 -Slug <slug-o-id>" -ForegroundColor Yellow
  exit 0
}

# Buscar por slug o id
$all = Invoke-RestMethod -Uri "$base/services" -Headers $headers
$svc = $all | Where-Object { $_.slug -eq $Slug -or $_.id -eq $Slug } | Select-Object -First 1
if (-not $svc) { Write-Error "No encontrado: $Slug"; exit 1 }

$id = $svc.id

# Reanudar (ignora error si ya está activo)
try { Invoke-RestMethod -Method Post -Uri "$base/services/$id/resume" -Headers $headers | Out-Null } catch {}

# Deploy
$deploy = Invoke-RestMethod -Method Post -Uri "$base/services/$id/deploys" -Headers $headers -ContentType "application/json"

"Servicio: $($svc.slug)  ID: $id"
"Código: $($deploy.statusCode)"
$deploy | ConvertTo-Json -Depth 6
