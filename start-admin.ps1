$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
& .venv/Scripts/python.exe admin_launcher.py @args
