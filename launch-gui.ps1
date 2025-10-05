# Launch Disk Scanner GUI (fixed)
$Base    = "C:\Users\Administrator\VideoCatalog"
$VenvPy  = Join-Path $Base "venv\Scripts\python.exe"
$GuiPath = Join-Path $Base "DiskScannerGUI.py"
& "$VenvPy" "$GuiPath"
