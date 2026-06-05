@echo off
echo ============================================
echo   HireLab Screener - Manual Backup
echo ============================================
echo.

set BACKUP_NAME=hirelab_backup_%date:~-4%-%date:~3,2%-%date:~0,2%_%time:~0,2%%time:~3,2%.db
set BACKUP_NAME=%BACKUP_NAME: =0%
set DATA_DIR=%USERPROFILE%\HireLab
set BACKUP_DIR=%USERPROFILE%\HireLab\backups

if not exist "%DATA_DIR%\hirelab.db" (
    echo No database found at: %DATA_DIR%\hirelab.db
    echo Nothing to backup.
    pause
    exit
)

if not exist "%BACKUP_DIR%" mkdir "%BACKUP_DIR%"
copy "%DATA_DIR%\hirelab.db" "%BACKUP_DIR%\%BACKUP_NAME%"

echo.
echo BACKUP COMPLETE!
echo Saved at: %BACKUP_DIR%\%BACKUP_NAME%
echo.
echo Tip: Run this before downloading any new update.
echo.
pause
