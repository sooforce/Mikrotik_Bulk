@echo off
setlocal EnableDelayedExpansion
title MikroProv - Build

echo.
echo ================================================================
echo   MikroProv - PyInstaller Build Script
echo ================================================================
echo.

REM ── Check Python ────────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not found in PATH.
    echo  Download from https://www.python.org/downloads/ and retry.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo  Python: %%v

REM ── Install / upgrade build dependencies ────────────────────────────────────
echo.
echo  Installing build dependencies (pyinstaller, pillow)...
pip install pyinstaller pillow --quiet --upgrade
if errorlevel 1 (
    echo  ERROR: pip install failed.
    pause
    exit /b 1
)

REM ── Convert logo PNG -> ICO ─────────────────────────────────────────────────
echo.
set ICON_ARG=
if exist "assets\logo.png" (
    echo  Converting assets\logo.png to assets\logo.ico ...
    python -c ^
        "from PIL import Image; ^
         img = Image.open('assets/logo.png').convert('RGBA'); ^
         img.save('assets/logo.ico', format='ICO', ^
                  sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)]); ^
         print('  Icon saved: assets/logo.ico')"
    if exist "assets\logo.ico" (
        set ICON_ARG=--icon=assets\logo.ico
    )
) else (
    echo  WARNING: assets\logo.png not found - building without custom icon.
    echo           Copy the logo PNG to assets\logo.png and re-run to add it.
)

REM ── Clean previous build ────────────────────────────────────────────────────
echo.
echo  Cleaning previous build artefacts...
if exist "build" rmdir /s /q build
if exist "dist\MikroProv.exe" del /q "dist\MikroProv.exe"

REM ── Run PyInstaller ─────────────────────────────────────────────────────────
echo.
echo  Running PyInstaller...
echo.

pyinstaller ^
    --onefile ^
    --windowed ^
    --name MikroProv ^
    --uac-admin ^
    --add-data "assets;assets" ^
    %ICON_ARG% ^
    mikrotik_provisioner.py

if errorlevel 1 (
    echo.
    echo  BUILD FAILED - check the output above for details.
    pause
    exit /b 1
)

REM ── Done ────────────────────────────────────────────────────────────────────
echo.
echo ================================================================
echo   Build complete!
echo.
echo   Executable : dist\MikroProv.exe
echo.
echo   The EXE is fully self-contained - copy it to any Windows 10/11
echo   machine and double-click to run.  No Python installation needed.
echo.
echo   NOTE: Windows will prompt for Administrator rights on launch
echo         (required for the built-in DHCP server on port 67).
echo ================================================================
echo.
pause
