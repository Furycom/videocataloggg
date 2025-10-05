# Launch Disk Scanner GUI (clean)
$Base   = "C:\Users\Administrator\VideoCatalog"
$VenvPy = Join-Path $Base "venv\Scripts\python.exe"
$Gui    = Join-Path $Base "DiskScannerGUI.py"

Set-Location $Base
powershell -NoProfile -ExecutionPolicy Bypass -Command "& `"$VenvPy`" `"$Gui`""
