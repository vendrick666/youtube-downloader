@echo off
chcp 65001 >nul
title Сборка YouTube Downloader.exe
color 0B
cd /d "%~dp0"

echo ==========================================
echo    СБОРКА YouTube Downloader.exe
echo ==========================================
echo.

:: Python нужен только для сборки. Готовому exe он уже не нужен.
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY goto :nopython

echo [1/3] Ставлю PyInstaller...
%PY% -m pip install -U -q --disable-pip-version-check pyinstaller
if errorlevel 1 goto :fail

echo [2/3] Собираю exe, это 1-2 минуты...
set "WORK=%TEMP%\ytdl_build"
%PY% -m PyInstaller --noconfirm --clean --log-level WARN --onefile --windowed --name "YouTube Downloader" --icon "%~dp0icon.ico" --add-data "%~dp0icon.ico;." --distpath "%~dp0." --workpath "%WORK%" --specpath "%WORK%" "%~dp0ytdl_gui.py"
if errorlevel 1 goto :fail
if not exist "%~dp0YouTube Downloader.exe" goto :fail
rmdir /s /q "%WORK%" 2>nul

echo [3/3] Делаю ярлык на рабочем столе...
powershell -NoProfile -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\YouTube Downloader.lnk'); $s.TargetPath = '%~dp0YouTube Downloader.exe'; $s.WorkingDirectory = '%~dp0'; $s.Save()"

echo.
echo ==========================================
echo [ГОТОВО] %~dp0YouTube Downloader.exe
echo Ярлык "YouTube Downloader" лежит на рабочем столе.
echo ==========================================
echo.
pause
exit /b 0

:nopython
echo [ОШИБКА] Python не найден. Для сборки нужен Python 3.10 или новее:
echo https://www.python.org/downloads/
echo.
pause
exit /b 1

:fail
echo.
echo [ОШИБКА] Сборка не удалась. Пришли скрин этого окна.
echo.
pause
exit /b 1
