@echo off
if "%1" == "h" goto begin
mshta vbscript:createobject("wscript.shell").run("""%~0""h",0)(window.close)&&exit
:begin
timeout /t 10 /nobreak
for /f "delims=" %%i in ('where python') do set "PYTHON_EXECUTABLE=%%i"

"%PYTHON_EXECUTABLE%" C:\Users\Public\python_script\python_recorder.py
pause