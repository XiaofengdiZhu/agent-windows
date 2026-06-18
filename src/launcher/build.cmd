@echo off
REM Build the launcher with the correct MSVC environment loaded.
REM Loads vcvars64 (MSVC linker + libs), then appends the Windows SDK lib/include
REM paths — needed because a standalone winget SDK install isn't always picked up
REM by vcvars' auto-detection.
REM Usage: build.cmd build --release  (or any cargo subcommand)

call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
if errorlevel 1 (
    echo [build] ERROR: vcvars64.bat failed ^(errorlevel %errorlevel%^)
    exit /b 1
)

REM Locate the installed Windows 10 SDK version and append its paths.
for /f "delims=" %%v in ('dir /b /ad "C:\Program Files (x86)\Windows Kits\10\Lib" 2^>nul') do (
    set "WINSDK_VER=%%v"
)
if defined WINSDK_VER (
    set "WINSDK_LIB=C:\Program Files (x86)\Windows Kits\10\Lib\%WINSDK_VER%"
    set "WINSDK_INC=C:\Program Files (x86)\Windows Kits\10\Include\%WINSDK_VER%"
    set "LIB=%LIB%;%WINSDK_LIB%\um\x64;%WINSDK_LIB%\ucrt\x64"
    set "INCLUDE=%INCLUDE%;%WINSDK_INC%\um;%WINSDK_INC%\shared;%WINSDK_INC%\ucrt"
    echo [build] appended SDK %WINSDK_VER% to LIB/INCLUDE
) else (
    echo [build] WARNING: no Windows Kits SDK found under "C:\Program Files (x86)\Windows Kits\10\Lib"
)

"C:\Users\52308\.cargo\bin\cargo.exe" %*
