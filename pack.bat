@echo off
setlocal enabledelayedexpansion

REM Check if a file was dragged onto this batch file
if "%~1"=="" (
    echo Usage: Drag a .blend file onto this batch file
    echo Or: pack_drag.bat "path\to\file.blend" "output\directory"
    pause
    exit /b 1
)

REM Get the dragged file path
set "INPUT_FILE=%~1"
set "OUTPUT_DIR=%~2"
set "WORKFLOW_ARGS=--workflow pack-and-save"
set "MIRROR_FLAG="

REM Check if input file exists
if not exist "%INPUT_FILE%" (
    echo Error: File does not exist: %INPUT_FILE%
    pause
    exit /b 1
)

REM Check if it's a .blend file
echo "%INPUT_FILE%" | findstr /i "\.blend" >nul
if errorlevel 1 (
    echo Error: File must be a .blend file: %INPUT_FILE%
    pause
    exit /b 1
)

REM If no output directory specified, default under Downloads, folder named after the blend
if "%OUTPUT_DIR%"=="" (
    set "OUTPUT_DIR=C:\Users\%USERNAME%\Downloads\%~n1"
)

REM Create output directory if it doesn't exist
if not exist "%OUTPUT_DIR%" (
    mkdir "%OUTPUT_DIR%"
)

echo.
echo Select workflow:
echo   1^) Copy-only (no packing, remap only)
echo   2^) Pack and Save (current workflow with Downloads export) [default]
set /p WORKFLOW_CHOICE=Enter choice [2]:
if /I "%WORKFLOW_CHOICE%"=="1" (
    set "WORKFLOW_ARGS=--workflow copy-only"
    echo.
    set /p MIRROR_CHOICE=Mirror render settings to linked blends? ^(Y/N^) [N]:
    if /I "%MIRROR_CHOICE%"=="Y" (
        set "MIRROR_FLAG=--mirror-render-settings"
    ) else (
        set "MIRROR_FLAG="
    )
) else (
    set "WORKFLOW_ARGS=--workflow pack-and-save"
    set "MIRROR_FLAG="
)

REM Get the directory where this batch file is located
set "BATCH_DIR=%~dp0"

REM Run the pack script
echo Packing: %INPUT_FILE%
echo Output: %OUTPUT_DIR%
echo Workflow: %WORKFLOW_ARGS% %MIRROR_FLAG%
echo.

blender --factory-startup -b "%INPUT_FILE%" -P "%BATCH_DIR%pack.py" -- --enable-nla %WORKFLOW_ARGS% %MIRROR_FLAG% "%OUTPUT_DIR%"

echo.
echo Packing complete!
pause
