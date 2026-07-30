@echo off
rem Автоперезапуск решателя капчи для безнадзорной работы (недели/месяц).
rem Закрывает разом два риска долгого прогона: возможный рост памяти со
rem временем (Python/Chromium не всегда сразу отдают память ОС — новый
rem процесс гарантированно чист) и падения (сеть, тоннель, необработанная
rem ошибка — сейчас без обёртки это требует, чтобы человек заметил и
rem перезапустил руками; с ней падение = просто ещё один цикл).
rem
rem Использование:
rem   win_autorestart.cmd <номер_прокси_в_private_local.txt> <задержка_сек> [размер_цикла]
rem   пример: win_autorestart.cmd 9 1 3000
rem
rem ВАЖНО: перед запуском задай пароль сервера один раз в ЭТОМ окне —
rem иначе на каждом перезапуске (каждые ~3000 атлетов) скрипт будет
rem стоять и ждать ввода пароля, а прогон "без участия человека" не выйдет:
rem   set PM_SSH_PASS=твой_пароль

setlocal
if "%~1"=="" (
  echo Использование: win_autorestart.cmd ^<номер_прокси^> ^<задержка_сек^> [размер_цикла]
  echo   пример: win_autorestart.cmd 9 1 3000
  echo.
  echo   Номер прокси - тот же, что спрашивает waf_solver при выборе из private_local.txt.
  echo   Не забудь заранее: set PM_SSH_PASS=пароль  (иначе перезапуск будет ждать ввода)
  exit /b 1
)
if "%PM_SSH_PASS%"=="" (
  echo ПРЕДУПРЕЖДЕНИЕ: PM_SSH_PASS не задан в этом окне.
  echo На каждом перезапуске придётся вводить пароль вручную - для
  echo безнадзорного прогона задай его сейчас: set PM_SSH_PASS=пароль
  echo.
)

set PROXY_IDX=%~1
set DELAY_S=%~2
set CYCLE=%~3
if "%CYCLE%"=="" set CYCLE=3000

:loop
echo.
echo ================================================================
echo %date% %time%  -- запуск (proxy-index=%PROXY_IDX% delay=%DELAY_S% limit=%CYCLE%)
echo ================================================================
py -m athlete_sweep.waf_solver --fast --proxy-file private_local.txt --proxy-index %PROXY_IDX% --limit %CYCLE% --delay %DELAY_S%
echo %date% %time%  -- процесс завершился (код %errorlevel%), пауза 10с и перезапуск...
timeout /t 10 /nobreak >nul
goto loop
