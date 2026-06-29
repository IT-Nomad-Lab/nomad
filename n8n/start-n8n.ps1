# Start the standalone local n8n. Open http://localhost:5678 when it's up.
Set-Location -Path $PSScriptRoot
if (-not (Test-Path ".env")) {
    Write-Host "No .env found. Copying .env.example -> .env (edit it to set a real key)."
    Copy-Item ".env.example" ".env"
}
docker compose up -d
Write-Host ""
Write-Host "n8n starting at http://localhost:5678"
