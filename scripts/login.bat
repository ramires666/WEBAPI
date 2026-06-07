@echo off
chcp 65001 >nul

set "PROFILES_DIR=W:\_python\APIPROXY\profiles"

if "%1"=="1" set "PROFILE=Profile 2"   & goto :open
if "%1"=="2" set "PROFILE=Profile 4"   & goto :open
if "%1"=="3" set "PROFILE=Profile_Fixed" & goto :open

echo Usage: login.bat [1^|2^|3]
echo   1 = Profile 2
echo   2 = Profile 4
echo   3 = Profile_Fixed
exit /b 1

:open
start chrome --user-data-dir="%PROFILES_DIR%\%PROFILE%" --profile-directory=Default "https://chatgpt.com"