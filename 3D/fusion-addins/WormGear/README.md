# Sinfín y rueda (add-in de Fusion 360)

Genera un tornillo sin fin y su rueda dentada, paramétricos y pensados para imprimir sin soportes.

Está en **SÓLIDO > CREAR > Sinfín y rueda** (diseños de pieza e híbridos) y en **ENSAMBLAJE > CREAR** (diseños de ensamblaje).

## Instalación

```powershell
powershell -ExecutionPolicy Bypass -File 3D\fusion-addins\WormGear\install.ps1
```

El script crea un enlace (junction) desde `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\WormGear` hasta esta carpeta. Fusion carga directamente lo que hay en el repo.

Después, en Fusion: **Utilidades > Complementos** (Mayús+S) > *WormGear* > **Ejecutar**. Marca *Al iniciar* para que arranque siempre.

Si cambias el código, para el complemento y vuelve a ejecutarlo.

## Parámetros

| Grupo | Parámetro | Por defecto |
|---|---|---|
| Engrane | Módulo | 1.5 mm |
| | Entradas del sinfín | 1 |
| | Dientes de la rueda | 30 |
| | Ø primitivo del sinfín | 16 mm |
| | Ángulo de flanco (axial) | 45° (sin voladizos de más de 45° con el sinfín de pie) |
| | Cabeza / pie | 0.5 / 0.6 × módulo (diente bajo: con 45° uno normal acaba en punta) |
| | Holgura radial | 0.15 mm |
| | Holgura entre flancos | 0.1 mm |
| Sinfín | Longitud, Ø taladro | 30 mm, 5.1 mm |
| Rueda | Ancho, Ø taladro | 10 mm, 8.1 mm |
| | Borde sobre la cabeza | 0.63 mm |
| | Perfiles de la solevación | 23 |

El cuadro *Resumen* del diálogo se actualiza mientras se teclea. Muestra:
- la relación de transmisión;
- la distancia entre ejes;
- el ángulo de hélice, y si el sinfín se autobloquea;
- los diámetros;
- los avisos: cresta del sinfín muy fina, garganta de más de 45°, voladizos, socavado, dientes en punta y poco cubo.

Con un error no deja generar.

Los últimos valores se guardan en `settings.json`, que no va en git.

## Qué crea

- **En un diseño de ensamblaje o híbrido:** tres componentes:
  - "Sinfín …" y "Rueda …";
  - "Ejes …", fijo, con las líneas de los dos ejes.

  Cada pieza tiene una junta de giro sobre su eje, y las dos van unidas por un *enlace de movimiento* con la relación: arrastras el sinfín y gira la rueda.
- **En un diseño de pieza:** dos cuerpos, sin juntas.

Todo queda en un grupo de la línea de tiempo y se deshace con un solo Ctrl+Z.

**Cómo imprimirlo:** el sinfín de pie y la rueda tumbada.

## Cómo se calcula la rueda

La rueda no se dibuja a ojo, ni se mecaniza con cientos de restas, que dejarían cientos de caras por diente. Se calcula así:

1. Para cada plano z = c paralelo al plano medio de la rueda, se corta el "cortador" (el sinfín con cada flanco engordado media holgura y la cabeza más alta) con ese plano.
2. Al girar, el filete del sinfín, que es un helicoide, solo se desplaza a lo largo de su eje sin salir del plano. La rueda gira alrededor de Z, así que el plano tampoco se mueve. Por eso la envolvente 2D de cada plano es la del engrane 3D real, también lejos del centro.
3. La envolvente sale de la ecuación de engrane en forma cerrada (`wormgear/geometry.py`): la normal del flanco pasa por el punto primitivo.
4. Cada plano da el perfil cerrado del hueco de un diente: dos splines, el arco de fondo y el arco exterior.
5. Una **solevación** une los perfiles. Después se hace un patrón circular y se corta el disco en bruto, que lleva la garganta toroidal.

Cerca de las caras, el sinfín llega cada vez menos y los dientes salen bajos y débiles. Por eso el disco lleva un **chaflán de 45° en las dos caras**:
- empieza donde el diente baja de la mitad de su altura del centro;
- no se pone si quitaría menos de 0.3 mm;
- con la rueda tumbada, se imprime sin soportes.

El resultado es de unas 6 caras por diente.

La casilla **Comprobar engrane** usa cuerpos temporales:
- mide el volumen común entre sinfín y rueda en 8 posiciones de un paso de diente;
- mide cuánto gira el sinfín, con la rueda quieta, hasta tocar por cada lado (el juego).

Con los valores por defecto da 0 mm³ y ±3.8°, que son 0.100 mm axiales. Es justo la holgura pedida.

## Archivos

- `WormGear.py`: el add-in (botón, diálogo y eventos).
- `wormgear/geometry.py`: la matemática. No depende de Fusion: `python -X utf8 wormgear/geometry.py` imprime un resumen de prueba.
- `wormgear/build.py`: crea la geometría en el diseño activo.
- `wormgear/check.py`: comprueba interferencias y juego.
- `resources/`: los iconos.
- `install.ps1`: crea el enlace en la carpeta AddIns.
