@echo off
REM Build the launcher with the correct MSVC environment loaded.
REM Auto-detects the VS 2022 install (any edition, any drive) via vswhere,
REM calls its vcvars64 (MSVC linker + libs), then appends the Windows SDK
REM lib/include paths from the WindowsSdkDir vcvars just set - in case a
REM standalone SDK install wasn't picked up by vcvars' own auto-detection.
REM Usage: build.cmd build --release  (or any cargo subcommand)
REM
REM Why single-line "if ... goto" guards instead of multi-line "if ( ... )" blocks:
REM paths under "Program Files (x86)" contain a ")" that closes batch if/for
REM blocks early - a classic parse-poison footgun. Expanding such a var INSIDE
REM a "( ... )" block corrupts parsing; expanding it in a top-level statement or
REM a usebackq backtick command does not. So every check is a one-line goto, the
REM SDK append runs as top-level sets, and the for loops use usebackq backticks.
REM No setlocal/delayed-expansion is used because enabling it would break vcvars
REM (its scripts weren't written for delayed expansion and inherit the state).

REM -- Locate Visual Studio via vswhere (Community/BuildTools/Enterprise, any
REM    drive). vswhere ships with the VS installer. --
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" goto :no_vswhere
set "VSINSTALL="
for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -products * -property installationPath`) do set "VSINSTALL=%%i"
if not defined VSINSTALL goto :no_vs
set "VCVARS=%VSINSTALL%\VC\Auxiliary\Build\vcvars64.bat"
if not exist "%VCVARS%" goto :no_vcvars
echo [build] using VS: %VSINSTALL%

call "%VCVARS%"
if errorlevel 1 goto :vcvars_fail

REM -- Windows SDK: vcvars64 set WindowsSdkDir to the installed SDK root (any
REM    drive). Append its Lib/Include ourselves as a belt-and-braces fix for
REM    the standalone-SDK case vcvars sometimes misses. Harmless duplicate if
REM    vcvars already added them. --
if not defined WindowsSdkDir goto :no_sdk
set "WINSDK_VER="
for /f "usebackq delims=" %%v in (`dir /b /ad "%WindowsSdkDir%Lib" 2^>nul`) do set "WINSDK_VER=%%v"
if not defined WINSDK_VER goto :no_sdkver
set "WINSDK_LIB=%WindowsSdkDir%Lib\%WINSDK_VER%"
set "WINSDK_INC=%WindowsSdkDir%Include\%WINSDK_VER%"
set "LIB=%LIB%;%WINSDK_LIB%\um\x64;%WINSDK_LIB%\ucrt\x64"
set "INCLUDE=%INCLUDE%;%WINSDK_INC%\um;%WINSDK_INC%\shared;%WINSDK_INC%\ucrt"
echo [build] appended SDK %WINSDK_VER% from "%WindowsSdkDir%" to LIB/INCLUDE

:cargo
cargo %*
if errorlevel 1 goto :cargo_fail

REM Stage the release exe into prebuilt/ so uv build picks it up. prebuilt/ is
REM a sibling of this dir (src/launcher/prebuilt), so it's a local relative
REM path. Only the release artifact is staged (debug builds stay out of the
REM wheel). prebuilt/ contents are gitignored - this is a local staging step,
REM the exe is never committed. Skip silently for non-release invocations
REM (cargo test/check have no release artifact to stage). This block expands no
REM variables, so its "( ... )" is safe from the poison described above.
if exist "target\release\agent-windows.exe" (
    if not exist "prebuilt" mkdir "prebuilt"
    copy /Y "target\release\agent-windows.exe" "prebuilt\agent-windows.exe" >nul
    if errorlevel 1 goto :no_stage
    echo [build] staged release exe to prebuilt\agent-windows.exe ^(gitignored, not committed^)
)
exit /b 0

:no_vswhere
echo [build] ERROR: vswhere.exe not found at "%VSWHERE%"
echo [build]        Visual Studio ^(or its Build Tools^) does not appear to be installed.
goto :die
:no_vs
echo [build] ERROR: vswhere found no Visual Studio installation.
goto :die
:no_vcvars
echo [build] ERROR: vcvars64.bat not found at "%VCVARS%"
echo [build]        Is the "MSVC v143 - x64/x86 build tools" component installed?
goto :die
:vcvars_fail
echo [build] ERROR: vcvars64.bat failed ^(errorlevel %errorlevel%^)
goto :die
:no_sdk
echo [build] ERROR: vcvars did not set WindowsSdkDir - is the Windows SDK installed?
echo [build]        Add "Windows 11 SDK" via the Visual Studio Installer.
goto :die
:no_sdkver
echo [build] WARNING: no SDK version found under "%WindowsSdkDir%Lib" ^(continuing; vcvars may have set LIB/INCLUDE^)
goto :cargo
:no_stage
echo [build] ERROR: failed to stage release exe to prebuilt
goto :die
:cargo_fail
REM cargo already printed its error; surface its exit code.
exit /b %errorlevel%
:die
exit /b 1
