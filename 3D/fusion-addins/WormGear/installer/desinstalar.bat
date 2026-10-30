@echo off
rem Quita el add-in WormGear (Sinfín y rueda) de la carpeta de complementos de Fusion 360.
chcp 65001 >nul
setlocal
set "ADDINS=%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns"
set "DST=%ADDINS%\WormGear"

if not exist "%DST%" (
    echo WormGear no está instalado en %ADDINS%.
    goto :fin
)
rem Un enlace al repo (instalación de desarrollo) no se borra desde aquí
dir /AL /B "%ADDINS%" 2>nul | findstr /X /I "WormGear" >nul
if not errorlevel 1 (
    echo %DST% es un enlace a otra carpeta, no lo borro.
    goto :fin
)

echo Antes de seguir, detén el complemento en Fusion (Utilidades ^> Complementos ^> WormGear ^> Detener)
echo o cierra Fusion.
pause
rmdir /S /Q "%DST%"
if exist "%DST%" (
    echo No se ha podido borrar del todo %DST%. Cierra Fusion y vuelve a probar.
) else (
    echo WormGear desinstalado.
)

:fin
echo.
pause
