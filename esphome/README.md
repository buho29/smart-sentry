# esphome — firmware de las placas ESP32

Configuraciones ESPHome del proyecto **cúpula de agua / huerta**: la cámara
exterior con PIR, la cámara-proxy siempre encendida y varios Bluetooth proxy.

---

## Requisitos

- Python 3.11+
- Windows (los comandos de abajo son para PowerShell)
- ESPHome (se instala en el `venv`, ver más abajo)
- Cable USB para el primer flasheo de cada placa; después se puede por OTA

---

## Instalación

```powershell
cd esphome
python -m venv venv
venv\Scripts\Activate.ps1
pip install esphome
```

Si PowerShell se queja por la política de ejecución de scripts al activar el
entorno, ejecútalo antes en esa misma terminal:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

VS Code normalmente detecta el `venv` y ofrece usarlo como intérprete por
defecto; di que sí. Comprueba que aparece `(venv)` al principio de la línea.

### `secrets.yaml`

Todas las configs esperan `secrets.yaml` (está en `.gitignore`) con:

```yaml
wifi_ssid: "TU_SSID"
wifi_password: "TU_PASSWORD"
```

---

## Comandos habituales

```powershell
esphome dashboard .                        # UI web para editar/compilar/flashear
esphome run huerta.yaml                    # compilar + subir (USB la 1ª vez, luego OTA)
esphome compile huerta.yaml               # solo compilar
esphome logs huerta.yaml                   # logs por red/API
esphome logs huerta.yaml --device COM10    # logs por USB (puerto serie)
```

---

## Configuraciones

| Fichero | Placa | Qué es |
| --- | --- | --- |
| `huerta.yaml` | ESP32-S3-DevKitC-1 + cámara + PIR | **Config principal.** Nodo exterior a baterías: deep sleep permanente, despierta por PIR. |
| `esp32-s3-cam.yaml` | ESP32-S3-DevKitC-1 + cámara | Cámara-proxy siempre encendida, sirve stream/snapshot de forma continua para Frigate/YOLO. |
| `esp32-s3-cam-servo.yaml` | ESP32-S3-DevKitC-1 + cámara + 2 servos | **Torreta.** Como la anterior, más un pan/tilt que el servicio Python mueve para seguir al objetivo detectado. |

Cada variante de hardware lleva su propio YAML en vez de acumular condicionales
en uno solo. La duplicación entre ellos es consciente por ahora; si crecen más,
el mecanismo `packages:` de ESPHome permite factorizar el bloque común de
cámara/wifi/OTA.

---

## `huerta.yaml` — nodo de la huerta

- **Placa:** `esp32-s3-devkitc-1`. IP fija `192.168.1.50` (`manual_ip` para
  acelerar el DHCP y no quedarse en el hotspot de fallback).
- **Ciclo:** en deep sleep casi siempre; el PIR en **GPIO14** lo despierta.
  Tras despertar sigue activo un tiempo configurable (número *"Tiempo
  despierto (s)"* en Home Assistant) para capturar/servir imágenes y luego
  vuelve a dormir.
- **Servidor de cámara HTTP** (`esp32_camera_web_server`):
  - Stream MJPEG: `http://192.168.1.50:8080/`
  - Snapshot: `http://192.168.1.50:8081`
- **API de ESPHome:** expone un `binary_sensor` `estado` (ON = despierta,
  OFF = dormida). El servicio de inferencia se suscribe a él con
  `aioesphomeapi` y la `api.encryption.key` del propio YAML. Usa la API solo
  para acciones en el ESP (servos, relés); para vídeo, el stream HTTP.
- **Aviso al servidor:** al obtener IP hace una petición a
  `http://192.168.1.171:8080/cameras/huerta/esphome/awake` para que el
  servicio Python sepa que la cámara está disponible.
- **Ajustes en runtime desde Home Assistant:** `Cam Resolution`,
  `Cam JPEG Quality`, `Cam Max Framerate`, `Cam AE Level`,
  `Cam AGC Gain Ceiling`, `Cam XCLK (MHz)`.

### Primer arranque

1. Rellena `secrets.yaml`.
2. `esphome run huerta.yaml` por USB.
3. En el log serie busca `Connected` e `IP Address: 192.168.1.50`.
4. Prueba `http://192.168.1.50:8081` (snapshot) y `http://192.168.1.50:8080/`
   (stream, con VLC o Frigate).

### Debug

`esphome logs huerta.yaml`. Líneas útiles: `api: client connected`,
`WiFi connected`, `IP Address`, `Beginning sleep`.

---

## `esp32-s3-cam.yaml` — cámara-proxy permanente

Igual que `huerta.yaml` pero **sin deep sleep**: la placa está siempre
encendida y conectada, sirviendo stream (`:8080/`) y snapshot (`:8081`) sin
parar. La IP la asigna el DHCP. `logger` con `baud_rate: 0`, así que no hay
log por serie: usa `esphome logs esp32-s3-cam.yaml` (por red).

---

## `esp32-s3-cam-servo.yaml` — torreta de seguimiento

`esp32-s3-cam.yaml` más dos servos pan/tilt, sin PIR y sin deep sleep. El
servicio Python de inferencia (`detect/`) sigue a un objetivo detectado y va
corrigiendo la posición de la torreta para centrarlo.

- **Contrato con Python:** la acción de la API `set_servo_position`, con las
  variables `pan` y `tilt` en el rango **-1.0 a 1.0** (lo que espera
  `servo.write`; no se manejan grados en ninguna capa). El nombre se busca
  literal desde Python, así que renombrarlo aquí deja el seguimiento mudo sin
  dar ningún error.
- **Cuidado con el timer LEDC:** la cámara usa `LEDC_TIMER_0` para el XCLK, a
  decenas de MHz, y los servos van a 50 Hz. Como cada par de canales LEDC
  comparte timer (0-1 → timer 0, 2-3 → timer 1, 4-5 → timer 2, 6-7 → timer 3),
  los servos usan los canales **4 y 6**. Ponerlos en el 0 o el 1 reprogramaría
  el timer del XCLK y rompería la imagen.
- **Antes de flashear** hay que rellenar lo marcado como `PENDIENTE` en el YAML:
  los dos GPIO de los servos, una `api.encryption.key` **propia**
  (`openssl rand -base64 32`, la misma que se pone como `noise_psk` en
  `detect/cameras_config.json`) y una contraseña de OTA propia.
- **Probar el hardware** sin esperar a que haya detecciones, con
  `esphome logs esp32-s3-cam-servo.yaml` abierto:
  `POST http://<servidor>:8080/cameras/<camera_id>/servo` con
  `{"pan": 0, "tilt": 0}`. También hay un botón *"Servos a reposo"* en Home
  Assistant. `GET /cameras/<camera_id>/status` indica en `consumers` si la placa
  llegó a publicar el servicio.

El lado Python está en `detect/servo_tracker.py`; la lógica de seguimiento
(bloqueo de objetivo, zona muerta, rate limit) se documenta en
`detect/docs/ARQUITECTURA.md`.

---

## Componente `esp32_camera` parcheado

`components/esp32_camera/` es una copia local del componente oficial de
ESPHome (rama `dev`) con un único cambio en `esp32_camera.cpp`:

```cpp
this->config_.grab_mode = CAMERA_GRAB_LATEST;  // antes: CAMERA_GRAB_WHEN_EMPTY
```

Reduce la latencia / "arrastre" del frame en el stream MJPEG (no sube el FPS
máximo). Se activa desde el YAML con:

```yaml
external_components:
  - source:
      type: local
      path: components
    components: [esp32_camera]
```

Como está hecho sobre la rama `dev`, si tu ESPHome instalado es otra versión
estable puede fallar la compilación por desajuste de símbolos; en ese caso
mira `esphome version` y regenera la copia sobre esa versión. Detalles en
`components/esp32_camera/README-parche-grab_latest.md`.

---

## Carpeta `old/`

Material descartado (`old/esp_pm_local.h`, `old/test_light_sleep.yaml`), en
`.gitignore`. `huerta_light_sleep.yaml` depende de `old/esp_pm_local.h` vía
`includes:`.
