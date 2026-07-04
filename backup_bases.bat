@echo off
REM SignalForge - backup semanal comprimido de las bases a OneDrive.
REM Lo ejecuta la Tarea Programada "SignalForge Backup Bases" (domingos 12:00).
REM RUTA-INDEPENDIENTE: %~dp0 = la carpeta de este .bat.
cd /d "%~dp0"
"C:\Users\ROG ZEPHYRUS\AppData\Local\Python\pythoncore-3.14-64\python.exe" backup_bases.py >> "_backup_bases.log" 2>&1
