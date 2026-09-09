# detect — servicio YOLO multi-cámara

Servicio de detección de objetos con YOLO para varias cámaras ESP32-S3-CAM,
pensado para integrarse con Home Assistant / ESPHome.

Expone una API FastAPI que, por cada cámara registrada, mantiene una sola
conexión contra el ESP32, corre la inferencia una única vez y reparte el
stream (crudo y/o anotado) entre N clientes.

---

## Requisitos

- Python 3.11+
- Windows (las rutas y comandos de abajo son para PowerShell)
- GPU NVIDIA con CUDA para inferencia en tiempo real (opcional: se puede usar
  `device: cpu`, pero va lento)

---

## Instalación

```powershell
cd detect
python -m venv venv
venv\Scripts\activate
```

Si PowerShell se queja por la política de ejecución de scripts al activar el
entorno (habitual la primera vez), ejecútalo una vez como usuario:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### PyTorch con CUDA

`torch` / `torchvision` **no** están en `requirements.txt` a propósito: hay
que instalarlos desde el índice de PyTorch con la variante de CUDA correcta.
Comprueba primero qué CUDA soporta tu driver:

```powershell
nvidia-smi
```

Y ajusta `cu124` a esa versión:

```powershell
pip install torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cu124
```

Son varios GB, tarda un rato. Verifica que ve la GPU:

```powershell
python -c "import torch; print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0))"
```

Debería imprimir `True` y el nombre de la tarjeta (p. ej. `NVIDIA GeForce GTX 1080`).

> **Nota GTX 1080 (Pascal):** `main.py` fuerza `torch.backends.cudnn.enabled = False`
> para evitar el error `CUDA misaligned address` con cuDNN en esta GPU.

### Resto de dependencias

```powershell
pip install -r requirements.txt
```

### Modelos

Los pesos YOLO (`*.pt`) se descargan solos la primera vez que se usan, o
puedes dejarlos en este directorio. En el repo hay varios: `yolo11n.pt`,
`yolo11m.pt`, `yolo26m.pt`, `rtdetr-l.pt`. Se seleccionan por `model_name` al
registrar la cámara.

---

## Arranque

```powershell
uvicorn main:app --host 0.0.0.0 --port 8080 --workers 1 --timeout-graceful-shutdown 5
```

- `--workers 1` es **obligatorio**: el estado de las cámaras vive en memoria
  de un único proceso.
- `--timeout-graceful-shutdown 5` es **obligatorio** para que uvicorn cierre a
  tiempo (los hilos de cámara hacen `join()` al apagar).

Documentación interactiva (Swagger): <http://localhost:8080/docs>

---

## Registrar una cámara

```powershell
curl -X POST http://localhost:8080/cameras -H "Content-Type: application/json" -d '{
  "camera_id": "huerta",
  "stream_url": "http://192.168.1.50:8080/",
  "model_name": "yolo26m",
  "device": "cuda",
  "confidence": 0.5,
  "classes": [0],
  "noise_psk": "<api.encryption.key del YAML de la placa>"
}'
```

También se puede registrar desde Swagger (recomendado) con el mismo JSON.
Ver `cameras_config.example.json` para todos los campos disponibles.

La configuración se guarda automáticamente en `cameras_config.json` (mismo
directorio) y se recarga sola en el siguiente arranque; no hace falta
re-registrar la cámara cada vez. Ese fichero está en `.gitignore` porque
contiene `noise_psk` e IPs de la LAN.

---

## Uso

| Qué | Endpoint |
| --- | --- |
| Stream anotado | `GET /cameras/{camera_id}/stream` |
| Stream crudo | `GET /cameras/{camera_id}/stream?infer=false` |
| Snapshot suelto | `GET /cameras/{camera_id}/snapshot` |
| Estado / diagnóstico | `GET /cameras/{camera_id}/status` |
| Arrancar / parar a mano | `POST /cameras/{camera_id}/start` \| `/stop` |

Con `noise_psk` configurado, la cámara arranca y para sola siguiendo el estado
real del PIR de la placa (`huerta_estado` on/off); en el uso normal no hace
falta llamar a `/start` a mano.

### Otros endpoints

- `GET /health` — vivo / no vivo
- `GET /config`, `POST /config/keepalive` — configuración global
- `GET /cameras`, `POST /cameras`, `DELETE /cameras/{camera_id}` — alta/baja
- `POST /cameras/{camera_id}/esphome/awake` — forzar "despierto"
- `POST /cameras/{camera_id}/inference/config` — cambiar confidence, clases, imgsz…
- `POST /cameras/{camera_id}/config/keepalive`
- `POST /cameras/{camera_id}/stream/config`
- `POST /detect` — detección puntual sobre una imagen por URL
- `POST /detect-file` — íd. sobre un fichero subido
- `POST /detect-file/annotated` — íd. devolviendo la imagen con las cajas

---

## Arquitectura por cámara (`CameraSession`)

- **Hilo lector (`_read_loop`):** habla con el ESP32-S3-CAM y se queda solo
  con el frame más reciente. El firmware `esp32_camera_web_server` admite un
  único consumidor de stream a la vez, así que este es el único proceso que
  abre conexión contra la placa. El framing multipart se parsea usando el
  `Content-Length` real de cada parte (buscar `\xff\xd8` / `\xff\xd9` a mano se
  desincronizaba cuando esos bytes aparecían dentro de un JPEG comprimido).
- **Hilo de proceso (`_process_loop`):** toma el frame más reciente, corre
  YOLO **solo si hay algún cliente** pidiendo el stream anotado, y publica el
  resultado (crudo y/o anotado) mediante una `threading.Condition` para que lo
  consuman N clientes a la vez. Así HA, el navegador, etc. miran el mismo
  stream sin abrir varias conexiones al ESP32 ni duplicar la inferencia.
- **Arranque / parada:** cada sesión tiene un flag `explicit_start`. Si se
  activa por API (`/start`), la sesión sigue viva aunque no haya clientes. Si
  no, arranca con el primer cliente y se para al irse el último (evita
  insistir en conectar a una cámara que el PIR ha dormido).

---

## Librerías

| Librería | Para qué |
| --- | --- |
| **fastapi** | El framework web. Define los endpoints, valida entrada/salida y genera `/docs`. |
| **uvicorn** | El servidor ASGI que ejecuta la app y escucha en el puerto 8080. |
| **pydantic** | Valida y estructura los datos (`CameraConfig`, `InferenceConfig`…); rechaza una petición mal formada antes de que llegue al código. |
| **ultralytics** | La librería de YOLO (`YOLO(...)`, `model.predict()`): carga los pesos `.pt` y hace la detección. |
| **opencv-python** (`cv2`) | Decodifica los JPEG del ESP32 (`cv2.imdecode`), dibuja cajas y texto (`cv2.rectangle`, `cv2.putText`) y recodifica a JPEG (`cv2.imencode`). |
| **numpy** | La estructura numérica base sobre la que trabajan OpenCV y PyTorch/YOLO. Un frame es un array de numpy. |
| **requests** | Cliente HTTP para el stream MJPEG del ESP32 (`requests.get(..., stream=True)` + `iter_content()`). |
| **torch** (PyTorch) | El motor de deep learning bajo YOLO. Se usa directamente para mover el modelo a la GPU (`model.to("cuda")`) y para `torch.backends.cudnn.enabled = False` (bug de la GTX 1080). |
| **aioesphomeapi** | Cliente de la API nativa de ESPHome (puerto 6053, no HTTP). Lo usa `EsphomeController` para suscribirse a `huerta_estado`. |

---

## Documentación

| Fichero | Qué cubre |
| --- | --- |
| [`docs/ARQUITECTURA.md`](docs/ARQUITECTURA.md) | Qué hace cada clase, método y endpoint de `main.py`, con diagramas (UML, flujo, secuencia). |
| [`docs/CICLO-DE-VIDA.md`](docs/CICLO-DE-VIDA.md) | Cuánto vive cada instancia, quién la destruye y cómo se cierran los sockets cuando la placa se duerme. |

---

## Tests

```powershell
venv\Scripts\python.exe test\test_cierre.py
```

`test/test_cierre.py` comprueba, **sin necesidad del ESP32**, que al parar una
cámara se cierran de verdad la conexión del stream, el socket de la API nativa
y el event loop del `EsphomeController`, y que ningún hilo se queda vivo ni
muere con un traceback. Usa `192.0.2.1` (TEST-NET-1) para simular una placa
dormida y un servidor MJPEG local para el caso de conexión viva.

El resto de scripts de `test/` son pruebas manuales que sí necesitan la placa.

---

## Historial de depuración

Las notas sobre los P-states de la GTX 1080 y sobre por qué el filtrado de
confianza / ByteTrack **no** era la causa de los cuelgues están en los
comentarios de `_process_loop` dentro de `main.py`.
