@echo off
cd /d "%~dp0"
where pyinstaller >nul 2>nul
if errorlevel 1 (
  echo Установите: pip install -r requirements-build.txt
  exit /b 1
)
pyinstaller --noconfirm audioCuter.spec
if errorlevel 1 exit /b 1
echo.
echo Готово: dist\AudioCuter.exe
echo На другом ПК нужны ffmpeg/ffprobe в PATH и (для перетаскивания) windnd уже вшит в exe при сборке.
