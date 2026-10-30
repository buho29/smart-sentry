@echo off
rem Instala el add-in WormGear (Sinfín y rueda) en Fusion 360 copiando la carpeta WormGear
rem a la carpeta de complementos del usuario. No hace falta ser administrador.
chcp 65001 >nul
setlocal
set "SRC=%~dp0WormGear"
set "ADDINS=%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns"
set "DST=%ADDINS%\WormGear"

if not exist "%SRC%\WormGear.py" (
    echo No encuentro la carpeta WormGear junto a este archivo. Descomprime el zip entero antes de instalar.
    goto :fin
)
if not exist "%ADDINS%" (
    echo No encuentro la carpeta de complementos de Fusion 360:
    echo   %ADDINS%
    echo Abre Fusion 360 al menos una vez y vuelve a probar.
    goto :fin
)
rem Si es un enlace al repo (instalación de desarrollo), no se toca
dir /AL /B "%ADDINS%" 2>nul | findstr /X /I "WormGear" >nul
if not errorlevel 1 (
    echo %DST% es un enlace a otra carpeta, no lo sobrescribo.
    goto :fin
)

rem Copia espejo: borra lo que ya no existe, pero conserva los últimos valores del diálogo
robocopy "%SRC%" "%DST%" /MIR /XF settings.json /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
    echo Ha fallado la copia a %DST%.
    echo Si Fusion está abierto, ciérralo y vuelve a probar.
    goto :fin
)

echo Instalado en:
echo   %DST%
echo.
echo En Fusion 360: Utilidades ^> Complementos (Mayús+S) ^> WormGear ^> Ejecutar,
echo y marca "Al iniciar". Si Fusion ya estaba abierto y tenías una versión anterior,
echo detén el complemento y vuelve a ejecutarlo.
echo El botón está en SÓLIDO ^> CREAR ^> Sinfín y rueda.

:fin
echo.
pause
