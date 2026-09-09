# smart-sentry

Vigilancia con visión artificial para un nodo de cámara a baterías: una placa
**ESP32-S3-CAM** con PIR y deep sleep envía vídeo a un servicio **YOLO** que
hace la detección una sola vez y la reparte a Home Assistant, el navegador o
VLC.

El repositorio tiene **dos mitades independientes** que se comunican por red:

| Carpeta | Qué es | Lenguaje |
| --- | --- | --- |
| [`esphome/`](esphome/) | Firmware de las placas ESP32-S3-CAM: cámara exterior con PIR + deep sleep, cámara sin PIR | YAML de ESPHome + componente C++ parcheado |
| [`detect/`](detect/) | Servicio Python (FastAPI) que hace la inferencia YOLO sobre el vídeo de esas cámaras y lo multiplexa hacia N clientes. | Python |

---

## Cómo encajan

La placa `huerta` ofrece dos canales:

- **Stream MJPEG por HTTP** (`:8080`) y snapshot (`:8081`) — el vídeo.
- **API nativa de ESPHome** (`:6053`) — control y estado. Expone un
  `binary_sensor` `estado`: `ON` = despierta (PIR), `OFF` = a punto de dormir.

El servicio de `detect/` consume los dos: el vídeo por HTTP (con `requests` +
OpenCV) y el estado por la API nativa (con `aioesphomeapi` y la
`api.encryption.key` del YAML de la placa). Con eso arranca y para la
inferencia siguiendo el PIR real, sin polling.

Idea central: **una sola conexión al ESP32, una sola inferencia, N clientes**.
El firmware `esp32_camera_web_server` solo admite un consumidor de stream a la
vez, así que el servicio Python es el único que abre conexión contra la placa
y hace de multiplexor.

```
ESP32-S3-CAM (huerta)                 detect/ (FastAPI, 1 proceso)          Clientes
─────────────────────                 ───────────────────────────          ────────
stream MJPEG  :8080  ──HTTP stream──►  hilo lector → cola → hilo YOLO  ──►  Home Assistant
API nativa    :6053  ──estado on/off──►  EsphomeController → start/stop      Navegador / VLC
        ▲
        └──────────  POST /cameras/huerta/esphome/awake  (al obtener IP)
```

---

## Puesta en marcha

Cada mitad tiene su propio `venv` y sus instrucciones detalladas:

1. **Firmware** — ver [`esphome/README.md`](esphome/README.md). Flashea
   `huerta.yaml` (nodo a baterías) o `esp32-s3-cam.yaml` (cámara-proxy
   siempre encendida). Necesita un `secrets.yaml` con el WiFi.
2. **Servicio de detección** — ver [`detect/README.md`](detect/README.md).
   Instala PyTorch con la variante de CUDA correcta, arranca `uvicorn` y
   registra la cámara con un `POST /cameras`. La config se persiste en
   `cameras_config.json` y se recarga sola.

---

## Documentación

| Fichero | Qué cubre |
| --- | --- |
| [`esphome/README.md`](esphome/README.md) | Placas, configuraciones ESPHome, `huerta.yaml`, componente `esp32_camera` parcheado. |
| [`detect/README.md`](detect/README.md) | Instalación del servicio, endpoints, arquitectura por cámara. |
| [`detect/docs/ARQUITECTURA.md`](detect/docs/ARQUITECTURA.md) | Qué hace cada clase, método y endpoint de `main.py`, con diagramas. |
| [`detect/docs/CICLO-DE-VIDA.md`](detect/docs/CICLO-DE-VIDA.md) | Cuánto vive cada instancia y cómo se cierran los sockets cuando la placa se duerme. |

---

