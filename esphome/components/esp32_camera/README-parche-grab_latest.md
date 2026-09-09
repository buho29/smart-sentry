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

