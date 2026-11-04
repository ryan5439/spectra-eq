@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem ============================================================
rem  SPECTRA EQ — only needs this file + Eq.py
rem
rem  build_exe.bat          → install deps + build SpectraEQ.exe
rem  build_exe.bat run      → install deps + run Eq.py (no EXE)
rem ============================================================

cd /d "%~dp0"
if errorlevel 1 (
    echo ERROR: Cannot open build folder.
    pause
    exit /b 1
)

set "ROOT=%cd%"
set "APP=SpectraEQ"
set "SPEC=%ROOT%\SpectraEQ.spec"
set "DIST=%ROOT%\dist"
set "BUILD=%ROOT%\build"
set "OUT_EXE=%ROOT%\%APP%.exe"
set "MODE=build"
if /I "%~1"=="run" set "MODE=run"
if /I "%~1"=="test" set "MODE=run"

if not exist "%ROOT%\Eq.py" (
    echo ERROR: Eq.py not found next to build_exe.bat
    echo Put Eq.py and build_exe.bat in the same folder.
    pause
    exit /b 1
)

echo ============================================================
echo  SPECTRA EQ  ·  %MODE%
echo  Folder: %ROOT%
for /f "usebackq tokens=2 delims==" %%V in (`findstr /B /C:"APP_VERSION" "%ROOT%\Eq.py"`) do (
  set "VER=%%~V"
  set "VER=!VER:"=!"
  set "VER=!VER: =!"
)
echo  Source version: !VER!
echo  After build, title bar MUST show that version — close old SpectraEQ.exe first.
echo ============================================================
echo.

rem --- find Python ---
set "PYEXE="
where py >nul 2>&1
if not errorlevel 1 (
    for %%V in (3.11 3.12 3.10 3) do (
        if not defined PYEXE (
            for /f "delims=" %%P in ('py -%%V -c "import sys; print(sys.executable)" 2^>nul') do set "PYEXE=%%P"
        )
    )
)
if not defined PYEXE (
    where python >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "PYEXE=%%P"
    )
)
if not defined PYEXE (
    echo ERROR: Python 3 not found.
    echo Install Python 3.11 or 3.12 64-bit from python.org
    echo Check "Add python.exe to PATH", open a NEW Command Prompt, rerun.
    pause
    exit /b 1
)

echo Using Python: %PYEXE%
"%PYEXE%" --version
"%PYEXE%" -c "import sys,platform; print('  bits:', platform.architecture()[0]); print('  tag:', sys.version.split()[0])"
"%PYEXE%" -c "import struct; assert struct.calcsize('P')*8==64" 2>nul
if errorlevel 1 (
    echo ERROR: Need 64-bit Python.
    pause
    exit /b 1
)

"%PYEXE%" -c "import sys; raise SystemExit(0 if sys.version_info < (3,13) else 1)"
if errorlevel 1 (
    echo.
    echo ERROR: Python 3.13+ detected. Use Python 3.11 or 3.12 64-bit.
    echo   py -3.11 "%~f0" %*
    pause
    exit /b 1
)
echo.

echo [1/5] Upgrade pip tooling...
"%PYEXE%" -m pip install --upgrade "pip>=24" "setuptools>=69,<81" wheel
if errorlevel 1 goto :fail
if /I "%MODE%"=="build" (
    "%PYEXE%" -m pip install --upgrade "pyinstaller>=6.11.0"
    if errorlevel 1 goto :fail
)
echo.

echo [2/5] Install BINARY wheels (lean — no tensorflow/scipy required)...
rem NOTE: never put ! in version specs — EnableDelayedExpansion breaks them
"%PYEXE%" -m pip install --upgrade --only-binary=:all: --prefer-binary "numpy>=1.24" "pygame>=2.5.2" "sounddevice>=0.4.6"
if errorlevel 1 (
    echo Binary install failed — trying pinned set...
    "%PYEXE%" -m pip install --upgrade --only-binary=:all: "numpy>=1.24" "pygame==2.6.1" "sounddevice==0.5.1"
)
if errorlevel 1 (
    echo ERROR: No pre-built wheels for this Python. Use 3.11/3.12 64-bit.
    pause
    exit /b 1
)
echo Installing soundcard + cffi (optional — Eq.py has ctypes WASAPI fallback)...
"%PYEXE%" -m pip install --upgrade "soundcard>=0.4.2" "cffi>=1.15"
if errorlevel 1 echo   warning: soundcard optional — continuing
echo.

echo [3/5] Verify Eq.py...
"%PYEXE%" -c "import numpy,pygame,sounddevice; print('  numpy', numpy.__version__); print('  pygame', pygame.version.ver); print('  sounddevice', sounddevice.__version__)"
if errorlevel 1 goto :fail
"%PYEXE%" -c "import soundcard; print('  soundcard OK')" 2>nul
if errorlevel 1 echo   soundcard optional — ctypes WASAPI OK
"%PYEXE%" -c "import Eq; print('  Eq.py', Eq.APP_VERSION, '| WASAPI', Eq.WASAPI_NATIVE)"
if errorlevel 1 goto :fail
echo.

if /I "%MODE%"=="run" goto :run_app

echo [4/5] Write LEAN PyInstaller spec (blocks tensorflow/keras)...
rem Prefer a normal (non-Admin) Command Prompt — PyInstaller warns/blocks elevated builds
net session >NUL 2>&1
if not errorlevel 1 (
    echo   NOTE: Running as Administrator. Prefer a normal CMD if the build fails.
)
call :write_spec
if errorlevel 1 goto :fail
taskkill /F /IM SpectraEQ.exe >NUL 2>&1
timeout /t 1 /nobreak >NUL
if exist "%DIST%" rmdir /s /q "%DIST%"
if exist "%BUILD%" rmdir /s /q "%BUILD%"
if exist "%OUT_EXE%" del /f /q "%OUT_EXE%"
echo.

echo [5/5] PyInstaller one-file standalone (1-5 min)...
rem Spec already has excludes=... — do NOT pass --exclude-module with a .spec
"%PYEXE%" -m PyInstaller --noconfirm --clean "%SPEC%"
if errorlevel 1 goto :fail

if not exist "%DIST%\%APP%.exe" (
    echo ERROR: Expected %DIST%\%APP%.exe
    dir /s "%DIST%" 2>nul
    pause
    exit /b 1
)
copy /Y "%DIST%\%APP%.exe" "%OUT_EXE%" >NUL
if errorlevel 1 goto :fail
rem Versioned copy so you never reopen an old SpectraEQ.exe by mistake
for /f "usebackq tokens=2 delims==" %%V in (`findstr /B /C:"APP_VERSION" "%ROOT%\Eq.py"`) do (
  set "VER=%%~V"
  set "VER=!VER:"=!"
  set "VER=!VER: =!"
)
set "VER_EXE=%ROOT%\%APP%-!VER!.exe"
copy /Y "%OUT_EXE%" "!VER_EXE!" >NUL
for %%A in ("%OUT_EXE%") do echo   OK  %OUT_EXE%  ^(%%~zA bytes^)
echo   OK  !VER_EXE!

echo.
echo ============================================================
echo  BUILD OK  —  version !VER!
echo  CLOSE any old SpectraEQ window first.
echo  Run THIS file only:
echo    !VER_EXE!
echo  Title bar MUST say: SPECTRA EQ !VER!
echo  If it still says 2.3.5 you launched the wrong EXE.
echo ============================================================
pause
exit /b 0

:run_app
echo [4/5] Launching Eq.py (no EXE build)...
echo Title bar should show current APP_VERSION from Eq.py
echo.
"%PYEXE%" "%ROOT%\Eq.py"
echo.
pause
exit /b 0

:fail
echo.
echo FAILED.
pause
exit /b 1

rem ---- lean SpectraEQ.spec: EQ only — never pull ML stacks ----
:write_spec
> "%SPEC%" (
echo # -*- mode: python ; coding: utf-8 -*-
echo # AUTO-GENERATED by build_exe.bat
echo import sys
echo from pathlib import Path
echo from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs, collect_submodules
echo block_cipher = None
echo here = Path^(SPECPATH^)
echo datas, binaries = [], []
echo hiddenimports = [
echo     "numpy", "numpy.core._methods", "numpy.lib.format",
echo     "pygame", "pygame._sdl2", "pygame.gfxdraw",
echo     "sounddevice", "soundcard", "soundcard.mediafoundation",
echo     "cffi", "_cffi_backend", "multiprocessing",
echo ]
echo # Only these packages — do NOT collect_all^("scipy"^) / ML libs
echo for pkg in ^("pygame", "sounddevice", "numpy", "soundcard", "cffi"^):
echo     try:
echo         d, b, h = collect_all^(pkg^)
echo         datas += d; binaries += b; hiddenimports += h
echo     except Exception as exc:
echo         print^("WARN collect_all(%%s): %%s" %% ^(pkg, exc^), file=sys.stderr^)
echo try: hiddenimports += collect_submodules^("soundcard"^)
echo except Exception: pass
echo try: hiddenimports += collect_submodules^("cffi"^)
echo except Exception: pass
echo try: binaries += collect_dynamic_libs^("sounddevice"^)
echo except Exception: pass
echo try: binaries += collect_dynamic_libs^("cffi"^)
echo except Exception: pass
echo _BLOCK = ^("tensorflow", "keras", "tensorboard", "torch", "pandas", "sklearn", "cv2", "matplotlib", "scipy", "h5py", "grpc", "absl", "astunparse", "flatbuffers", "google.protobuf", "opt_einsum", "wrapt"^)
echo _seen, _hi = set^(^), []
echo for h in hiddenimports:
echo     low = str^(h^).lower^(^)
echo     if any^(b in low for b in _BLOCK^):
echo         continue
echo     if h not in _seen:
echo         _seen.add^(h^); _hi.append^(h^)
echo hiddenimports = _hi
echo excludes = [
echo     "tensorflow", "tensorflow_core", "tensorflow_estimator", "tensorboard",
echo     "keras", "keras_core", "tf_keras", "torch", "torchvision", "torchaudio",
echo     "pandas", "sklearn", "cv2", "matplotlib", "scipy", "h5py",
echo     "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6",
echo     "IPython", "jupyter", "notebook", "pytest", "unittest",
echo ]
echo a = Analysis^(
echo     [str^(here / "Eq.py"^)],
echo     pathex=[str^(here^)],
echo     binaries=binaries,
echo     datas=datas,
echo     hiddenimports=hiddenimports,
echo     hookspath=[],
echo     hooksconfig={},
echo     runtime_hooks=[],
echo     excludes=excludes,
echo     win_no_prefer_redirects=False,
echo     win_private_assemblies=False,
echo     cipher=block_cipher,
echo     noarchive=False,
echo ^)
echo pyz = PYZ^(a.pure, a.zipped_data, cipher=block_cipher^)
echo exe = EXE^(
echo     pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
echo     name="SpectraEQ",
echo     debug=False,
echo     bootloader_ignore_signals=False,
echo     strip=False,
echo     upx=False,
echo     upx_exclude=[],
echo     runtime_tmpdir=None,
echo     console=False,
echo     disable_windowed_traceback=False,
echo     argv_emulation=False,
echo     target_arch=None,
echo     codesign_identity=None,
echo     entitlements_file=None,
echo     icon=str^(here / "spectraeq.ico"^) if ^(here / "spectraeq.ico"^).exists^(^) else None,
echo     uac_admin=False,
echo ^)
)
if not exist "%SPEC%" exit /b 1
echo   wrote lean %SPEC%
exit /b 0

endlocal
