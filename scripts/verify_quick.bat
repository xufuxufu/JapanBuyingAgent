@echo off
setlocal
cd /d "%~dp0.."
set "PYTHON=.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
  echo [verify_quick] Missing %PYTHON%. 1>&2
  exit /b 1
)

echo [verify_quick] Compiling key Python files...
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

if not "%~1"=="" (
  echo [verify_quick] Running targeted tests: %*
  "%PYTHON%" -m pytest -q --basetemp=.pytest-tmp-verify-quick %* || exit /b 1
  exit /b 0
)

echo [verify_quick] Checking http://127.0.0.1:8020/health ...
"%PYTHON%" -c "import urllib.request; response=urllib.request.urlopen('http://127.0.0.1:8020/health', timeout=5); assert response.status == 200" || exit /b 1
exit /b 0
