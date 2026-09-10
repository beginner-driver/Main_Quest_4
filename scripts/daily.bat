@echo off
REM 매일 자동 실행되는 진입점. Windows 작업 스케줄러가 이 파일을 부른다.
REM
REM   등록:  scripts\register_task.bat        (관리자 권한 불필요)
REM   해제:  schtasks /delete /tn "IssueRadar" /f
REM
REM 실행 결과는 data\cron.log 에 누적된다. 스케줄러는 실패해도 조용하므로
REM 로그가 유일한 확인 수단이다.

cd /d "%~dp0.."
if not exist data mkdir data

echo. >> data\cron.log
echo ======== %DATE% %TIME% ======== >> data\cron.log

REM Ollama가 안 떠 있으면 판정 단계에서 실패한다. 먼저 깨운다.
ollama list >nul 2>&1

python main.py --hours 24 >> data\cron.log 2>&1

if errorlevel 1 (
    echo [실패] 종료 코드 %errorlevel% >> data\cron.log
) else (
    echo [완료] >> data\cron.log
)
