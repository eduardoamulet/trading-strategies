@echo off
title Batch backtesting (ucbatch) - NO cerrar hasta el OK
cd /d "C:\Users\ROG ZEPHYRUS\OneDrive\Documents\Claude AI\Traiding"

rem Busca el results file mas reciente en Downloads (evita problemas con el guion largo del nombre)
set "RES="
for /f "delims=" %%f in ('dir /b /o-d "%USERPROFILE%\Downloads\Backtesting_Results_*.xlsx" 2^>nul') do (
  if not defined RES set "RES=%USERPROFILE%\Downloads\%%f"
)
if not defined RES (
  echo No encontre ningun Backtesting_Results_*.xlsx en Downloads.
  pause
  exit /b 1
)

echo Template : Backtesting_use_cases_template.xlsx
echo Results  : %RES%
echo.
echo Corriendo el batch... (la barra se actualiza por lotes; dejalo terminar)
echo.

"%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe" "options_replay\run_ucbatch.py" --excel "Backtesting_use_cases_template.xlsx" --fill "%RES%" --out "resultados" --notify

echo.
echo ================================================================
echo  TERMINADO. El results file lleno esta en la carpeta "resultados".
echo  Podes cerrar esta ventana.
echo ================================================================
pause
