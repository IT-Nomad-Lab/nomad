@echo off
REM Start the standalone local n8n. Open http://localhost:5678 when it's up.
cd /d "%~dp0"
if not exist ".env" (
  echo No .env found. Copying .env.example -^> .env  ^(edit it to set a real key^).
  copy ".env.example" ".env"
)
docker compose up -d
echo.
echo n8n starting at http://localhost:5678
