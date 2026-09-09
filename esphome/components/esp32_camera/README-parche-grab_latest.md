# Parche CAMERA_GRAB_LATEST para esp32_camera (ESPHome)

## Qué contiene
Copia íntegra del componente `esp32_camera` de ESPHome (rama `dev`), con un único
cambio en `esp32_camera.cpp`:

    this->config_.grab_mode = CAMERA_GRAB_LATEST;  // antes: CAMERA_GRAB_WHEN_EMPTY

Todo lo demás (opciones de config, sensor, etc.) es idéntico al componente oficial.

## Cómo instalarlo
1. Copia la carpeta `components/esp32_camera/` completa a la raíz de tu proyecto
   ESPHome (donde está tu .yaml), de forma que quede:

       tu_proyecto/
       ├── esp32-s3-cam.yaml
       └── components/
           └── esp32_camera/
               ├── __init__.py
               ├── esp32_camera.cpp
               └── esp32_camera.h

2. Añade esto a tu YAML (una sola vez, en cualquier parte del fichero):

       external_components:
         - source:
             type: local
             path: components
           components: [esp32_camera]

3. Compila y flashea como siempre (`esphome run esp32-s3-cam.yaml`).
   ESPHome usará tu copia local en vez de la del core para ese componente.

## Importante: compatibilidad de versión
Este parche está hecho sobre la rama `dev` de ESPHome. Si tu instalación de
ESPHome es una versión estable distinta, puede que `esp32_camera.h`/`.cpp`
no case exactamente con el resto del framework (símbolos, includes) y falle
la compilación. Si te da error al compilar dime qué versión de ESPHome
tienes (`esphome version`) y te preparo la copia sobre esa versión exacta
en vez de `dev`.

## Cómo comprobar si mejora
Antes de nada, ojo con la variable que realmente quieres medir: esto ataca
la *latencia/frescura* del frame, no tanto el FPS máximo bruto. Para notarlo:
- Compara visualmente un movimiento rápido delante de la cámara en el stream
  MJPEG (puerto 8080) antes/después del parche — con WHEN_EMPTY deberías ver
  un ligero "arrastre" respecto al movimiento real.
- Si usas tu servidor de detección, mide el timestamp de captura vs. el
  timestamp de recepción en el servidor, antes y después.
