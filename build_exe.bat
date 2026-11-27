@echo off
rem ======================================================================
rem  LogLoss Watcher - build LogLoss_Watcher.exe with PyInstaller
rem
rem  Run on a Windows BUILD PC (not the HMI PC), in the folder that holds
rem  logloss_watcher.py, llw_core.py, llw_tray.py, llw.ico, version_info.txt
rem
rem  For Windows 7 + 10 + 11 HMI PCs build with Python 3.8.10 (the last
rem  Python for Windows 7). Set PY below to that python.exe.
rem ======================================================================
setlocal
set PY=py -3.8
rem set PY=C:\Python38\python.exe

cd /d "%~dp0"

echo [1/5] Creating clean build environment (.venv38)...
if not exist .venv38 %PY% -m venv .venv38 || goto :fail
call .venv38\Scripts\activate.bat || goto :fail

echo [2/5] Installing PyInstaller and pylogix (needs internet, or a local wheel folder)...
python -m pip install "pyinstaller==6.9.0" -r requirements.txt || goto :fail

echo [3/5] Building...
pyinstaller --noconfirm --clean --onedir --windowed ^
  --name LogLoss_Watcher ^
  --icon llw.ico ^
  --version-file version_info.txt ^
  --hidden-import pylogix ^
  logloss_watcher.py || goto :fail

echo [4/5] Copying README...
copy /y README.txt dist\LogLoss_Watcher\ >nul

echo [5/5] Writing third-party licences (pylogix, Python, Tcl/Tk)...
python tools\collect_licenses.py -r requirements.txt --tkinter ^
  --app "LogLoss Watcher" -o dist\LogLoss_Watcher\THIRD_PARTY_LICENSES.txt || goto :fail

echo.
echo DONE.  Copy the whole folder  dist\LogLoss_Watcher  to the HMI PC
echo        (keep THIRD_PARTY_LICENSES.txt in it). Start  LogLoss_Watcher.exe  inside it.
goto :eof

:fail
echo.
echo BUILD FAILED - see the messages above.
exit /b 1
