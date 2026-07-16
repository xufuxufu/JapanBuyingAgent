@echo off
setlocal
cd /d "%~dp0.."
set "PYTHON=.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
  echo [verify_core] Missing %PYTHON%. 1>&2
  exit /b 1
)

echo [verify_core] Compiling key Python files...
"%PYTHON%" -m py_compile ^
  app\main.py app\models.py app\schemas.py app\services.py ^
  app\product_identity.py app\product_matching.py app\purchase_service.py ^
  app\location_service.py app\qinsi_import.py app\qinsi_export.py ^
  app\price_providers.py app\price_service.py app\product_enrichment.py app\store_service.py ^
  app\watch_service.py app\monitor_service.py app\monitor_scheduler.py app\qinsi_inventory.py migrations\env.py ^
  migrations\versions\20260716_0013_store_traceability.py ^
  migrations\versions\20260716_0014_product_enrichment.py ^
  migrations\versions\20260716_0015_product_watch_mvp.py ^
  migrations\versions\20260716_0016_price_monitor_notifications.py ^
  migrations\versions\20260716_0017_qinsi_inventory_snapshots.py || exit /b 1

echo [verify_core] Checking Alembic current revision and heads...
"%PYTHON%" -m alembic heads || exit /b 1
"%PYTHON%" -m alembic current --check-heads || exit /b 1

echo [verify_core] Running core regression tests...
"%PYTHON%" -m pytest -q --basetemp=.pytest-tmp-verify-core ^
  tests\test_receipts.py ^
  tests\test_products_and_health.py ^
  tests\test_product_import_matching_tracking.py ^
  tests\test_purchase_batches.py ^
  tests\test_qinsi_purchase_exports.py ^
  tests\test_qinsi_inventory_snapshots.py || exit /b 1

echo [verify_core] Restarting 8020 with the existing project script...
start "" /b cmd /c call "%CD%\restart_dev_8020_py314.bat"

for /L %%I in (1,1,30) do (
  "%PYTHON%" -c "import urllib.request; response=urllib.request.urlopen('http://127.0.0.1:8020/health', timeout=2); assert response.status == 200" >nul 2>nul
  if not errorlevel 1 goto health_ok
  timeout /t 1 /nobreak >nul
)
echo [verify_core] 8020 health check failed. 1>&2
exit /b 1

:health_ok
echo [verify_core] 8020 health check passed.
exit /b 0
