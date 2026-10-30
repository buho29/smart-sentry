# Genera dist\WormGear-<versión>.zip para instalar el add-in sin el repo.
# El zip lleva instalar.bat, desinstalar.bat, LEEME.txt y la carpeta WormGear con solo lo que necesita Fusion.
# Uso:  powershell -ExecutionPolicy Bypass -File installer\make_release.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$version = [regex]::Match((Get-Content (Join-Path $root 'WormGear.manifest') -Raw), '"version"\s*:\s*"([^"]+)"').Groups[1].Value
$dist = Join-Path $root 'dist'
$stage = Join-Path $dist 'stage'
$zip = Join-Path $dist "WormGear-$version.zip"

if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force (Join-Path $stage 'WormGear\wormgear') | Out-Null
New-Item -ItemType Directory -Force (Join-Path $stage 'WormGear\resources') | Out-Null

# Lo que carga Fusion (sin settings.json, __pycache__, install.ps1 ni este instalador)
Copy-Item (Join-Path $root 'WormGear.manifest'), (Join-Path $root 'WormGear.py'), (Join-Path $root 'README.md') (Join-Path $stage 'WormGear')
Copy-Item (Join-Path $root 'wormgear\*.py') (Join-Path $stage 'WormGear\wormgear')
Copy-Item (Join-Path $root 'resources\*.png') (Join-Path $stage 'WormGear\resources')
Copy-Item (Join-Path $PSScriptRoot 'instalar.bat'), (Join-Path $PSScriptRoot 'desinstalar.bat'), (Join-Path $PSScriptRoot 'LEEME.txt') $stage

if (Test-Path $zip) { Remove-Item $zip -Force }
# Entradas a mano con '/': Compress-Archive y CreateFromDirectory de PowerShell 5 guardan '\' y en Mac no se descomprime bien
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::Open($zip, 'Create')
try {
    Get-ChildItem $stage -Recurse -File | ForEach-Object {
        $name = $_.FullName.Substring($stage.Length + 1).Replace('\', '/')
        [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $_.FullName, $name) | Out-Null
    }
} finally { $archive.Dispose() }
Remove-Item $stage -Recurse -Force
Write-Host "Creado: $zip"
