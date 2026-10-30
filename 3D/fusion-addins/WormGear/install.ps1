# Enlaza esta carpeta del repo en la carpeta de complementos de Fusion 360 (junction: no hace falta ser administrador).
# Uso:  powershell -ExecutionPolicy Bypass -File install.ps1
$addins = Join-Path $env:APPDATA 'Autodesk\Autodesk Fusion 360\API\AddIns'
$link = Join-Path $addins 'WormGear'
$target = $PSScriptRoot

if (-not (Test-Path $addins)) {
    Write-Error "No encuentro la carpeta de complementos de Fusion: $addins"
    exit 1
}
if (Test-Path $link) {
    $item = Get-Item $link -Force
    if ($item.LinkType -eq 'Junction' -and $item.Target -contains $target) {
        Write-Host "Ya estaba enlazado: $link -> $target"
        exit 0
    }
    Write-Error "Ya existe $link y no es un enlace a este repo; bórralo o renómbralo antes."
    exit 1
}
New-Item -ItemType Junction -Path $link -Target $target | Out-Null
Write-Host "Enlazado: $link -> $target"
Write-Host "En Fusion: Utilidades > Complementos (Mayús+S) > Sinfín y rueda (WormGear) > Ejecutar (y 'Al iniciar')."
