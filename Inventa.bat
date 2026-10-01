@echo off
REM ==========================================================================
REM  Inventa - spousteci soubor (desktopova aplikace, zadny server)
REM  Pri prvnim spusteni (nebo po zmene requirements.txt) pripravi virtualni
REM  prostredi .venv a nainstaluje zavislosti. Pote otevre okno aplikace.
REM ==========================================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"

if not exist "app_logs" mkdir "app_logs"

if not exist ".venv\Scripts\pythonw.exe" (
    echo [Inventa] Prvni spusteni - vytvarim virtualni prostredi...
    where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
    if errorlevel 1 goto :nopython
)

fc /b "requirements.txt" ".venv\requirements.installed" >nul 2>nul
if errorlevel 1 (
    echo [Inventa] Instaluji zavislosti - muze trvat par minut...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >> "app_logs\system_runner.log" 2>&1
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt >> "app_logs\system_runner.log" 2>&1
    if errorlevel 1 goto :pipfail
    copy /y "requirements.txt" ".venv\requirements.installed" >nul
)

".venv\Scripts\python.exe" -c "import tkinter" >nul 2>nul
if errorlevel 1 goto :notk

REM pythonw = okno aplikace bez cerne konzole; tento skript se hned ukonci
start "" ".venv\Scripts\pythonw.exe" "src\gui.py"
exit /b 0

:nopython
echo.
echo CHYBA: Python 3.10+ nebyl nalezen. Nainstalujte jej z https://www.python.org/
pause
exit /b 1

:pipfail
echo.
echo CHYBA: Instalace zavislosti selhala. Podrobnosti: app_logs\system_runner.log
pause
exit /b 1

:notk
echo.
echo CHYBA: Nainstalovany Python neobsahuje Tkinter (graficke rozhrani).
echo        Preinstalujte Python z python.org a ponechte zaskrtnute "tcl/tk and IDLE".
pause
exit /b 1
