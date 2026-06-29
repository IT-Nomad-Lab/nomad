@echo off
REM Stop the standalone local n8n (keeps the data volume).
cd /d "%~dp0"
docker compose down
echo n8n stopped. Data volume preserved.
