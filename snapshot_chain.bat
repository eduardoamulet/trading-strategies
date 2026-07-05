@echo off
REM SignalForge - captura del snapshot de la cadena de opciones (greeks/IV/OI propios).
REM Lo ejecutan las Tareas Programadas "SignalForge Snapshot Apertura" (09:35) y
REM "SignalForge Snapshot Cierre" (15:45). %1 = apertura | cierre.
REM RUTA-INDEPENDIENTE: %~dp0 = la carpeta de este .bat.
cd /d "%~dp0options_replay"
"C:\Users\ROG ZEPHYRUS\AppData\Local\Python\pythoncore-3.14-64\python.exe" chain_snapshots.py --momento %1 >> "..\_snapshots.log" 2>&1
