@echo off
REM 매일 아침 8시에 수집·판정이 자동으로 돌도록 등록한다.
REM 사람이 시작하지 않아도 결과가 준비되어 있게 만드는 것이 목적이다.
REM
REM 확인:  schtasks /query /tn "IssueRadar" /v /fo list
REM 즉시 실행:  schtasks /run /tn "IssueRadar"
REM 해제:  schtasks /delete /tn "IssueRadar" /f

setlocal
set TASK=IssueRadar
set RUNNER=%~dp0daily.bat

echo 작업 이름 : %TASK%
echo 실행 대상 : %RUNNER%
echo 실행 시각 : 매일 08:00
echo.

schtasks /create /tn "%TASK%" /tr "\"%RUNNER%\"" /sc daily /st 08:00 /f
if errorlevel 1 (
    echo.
    echo 등록에 실패했습니다. 이미 있는 작업이면 /f 로 덮어씁니다.
    exit /b 1
)

echo.
echo 등록되었습니다. 지금 한 번 돌려보려면:
echo     schtasks /run /tn "%TASK%"
echo 결과는 data\cron.log 에 쌓입니다.
endlocal
