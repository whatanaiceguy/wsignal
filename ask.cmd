@echo off
rem Ask one direction and watch the run. Double-click, or `ask --top 3`.
rem
rem The QUERY is never an argument, on purpose. A Cyrillic query passed on the
rem cmd.exe command line is read in the OEM code page and arrives mangled at the
rem other end, which then looks like the API rejecting Russian. Typed at the
rem prompt inside the container it is plain UTF-8 stdin and nothing touches it.
rem
rem Flags are forwarded, because they are ASCII and the code page cannot hurt
rem them. `ask --top 3` runs for three entries instead of the configured
rem default and still asks for the direction at the prompt.
setlocal

rem A double-clicked console starts at the system OEM code page, measured 437
rem on this box, which has no Cyrillic at all. Typing Russian there produces
rem bytes the container cannot decode and the crash lands on Enter, which reads
rem as the app rejecting the question. 65001 is UTF-8.
chcp 65001 >nul

set "SCRIPTDIR=%~dp0"
if "%SCRIPTDIR:~-1%"=="\" set "SCRIPTDIR=%SCRIPTDIR:~0,-1%"

for /f "usebackq delims=" %%i in (`wsl.exe wslpath -a "%SCRIPTDIR%" 2^>nul`) do set "REPO=%%i"
if not defined REPO (
  echo could not resolve this folder inside WSL. is WSL installed and running?
  exit /b 1
)

wsl.exe -- bash "%REPO%/scripts/ask.sh" %*
set "RC=%errorlevel%"

rem Double-clicked, the window closes with the process and takes the error with
rem it, so a crash looks like nothing happening. Hold it open.
echo.
if not "%RC%"=="0" echo run exited with code %RC%
pause
exit /b %RC%
