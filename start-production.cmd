@echo off
REM Catena di avvio produzione LocalRouter (proxy puro verso llama-swap).
REM Ordine: llama-swap (:8093) → gateway Localrouter (:8088).
REM LM Studio resta installato come via di ritorno (task separato / Run key).
REM NON mettere in Startup come .lnk: alcuni antivirus li mettono in quarantena.
cd /d "C:\Localrouter"
if not exist "logs" mkdir "logs"
echo [%DATE% %TIME%] start-production >> "logs\server_autostart.log"

call "C:\llamaswap\start-llamaswap.cmd"
if errorlevel 1 (
  echo [%DATE% %TIME%] WARN: llama-swap non pronto, gateway parte comunque >> "logs\server_autostart.log"
)

call "C:\Localrouter\start-gateway.cmd"
