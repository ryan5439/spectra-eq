@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if not exist "Eq.cpp" (
    echo ERROR: Eq.cpp not found next to build_cpp.bat
    pause
    exit /b 1
)

where cl >nul 2>&1
if %errorlevel%==0 (
    cl /nologo /EHsc /O2 /W3 /DUNICODE /D_UNICODE Eq.cpp /Fe:SpectraEQ-cpp.exe /link ole32.lib uuid.lib user32.lib gdi32.lib shell32.lib
    if errorlevel 1 (
        echo Build failed.
        pause
        exit /b 1
    )
    echo Built SpectraEQ-cpp.exe
    exit /b 0
)

where g++ >nul 2>&1
if %errorlevel%==0 (
    g++ -std=c++17 -O2 -DUNICODE -D_UNICODE -o SpectraEQ-cpp.exe Eq.cpp -lole32 -luuid -lgdi32 -luser32 -lshell32 -mwindows
    if errorlevel 1 (
        echo Build failed.
        pause
        exit /b 1
    )
    echo Built SpectraEQ-cpp.exe
    exit /b 0
)

echo Open a Visual Studio Developer Prompt, or install MinGW, then run build_cpp.bat again.
pause
exit /b 1
