@echo off
:: Autostart Localrouter — solo .bat/.cmd (NO PowerShell: ATD lo blocca come malware).
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0scripts\install-autostart-user.bat"
echo.
echo Se l'antivirus blocca ancora: aggiungi un'eccezione sulla cartella del progetto
pause
