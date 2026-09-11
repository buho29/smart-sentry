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

> **Nota GTX 1080 (Pascal):** `camera.py` fuerza `torch.backends.cudnn.enabled = False`
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

### Dos mensajes del log que son normales

- Al hacer **Ctrl+C**, después de `Finished server process`, sale un
  `OSError: [WinError 10038] ... signal wakeup fd`. Es cosa de uvicorn +
  CPython en Windows, con todo ya cerrado; no se puede silenciar desde Python.
- Con **dos o más** cámaras con `noise_psk`, al arrancar sale
  `Timezone resolution failed ... attached to a different loop`. Es de
  `aioesphomeapi` y no afecta a la conexión.

Los dos están explicados en
[`docs/CICLO-DE-VIDA.md`](docs/CICLO-DE-VIDA.md#11-dos-avisos-del-log-que-son-inofensivos).

---

## Registrar una cámara

El alta va por **formulario**, así que desde Swagger (recomendado) sale cada
campo en su casilla, con su descripción y su valor por defecto, en vez de un
JSON que editar a mano:

```powershell
curl -X POST http://localhost:8080/cameras `
  -F "camera_id=huerta" `
  -F "stream_url=http://192.168.1.50:8080/" `
  -F "model_name=yolo26m" -F "device=cuda" `
  -F "confidence=0.5" -F "classes=0" `
  -F "noise_psk=<api.encryption.key del YAML de la placa>"
```

`classes` son los IDs de clase COCO separados por comas (`0` = personas,
`16` = pájaros); vacío significa todas. Ver
[`CLASES_YOLO.md`](CLASES_YOLO.md) para la lista.

**Los servos no se configuran aquí**: si la placa lleva torreta, se monta
después con `POST /cameras/{camera_id}/config/servo`, que necesita que la
cámara ya exista y tenga `noise_psk`.

Todo lo de arriba se puede cambiar luego sin dar de baja la cámara, desde los
endpoints `/config/…` (ver más abajo) — incluidos el modelo y el device, que
relanzan la sesión solos porque son lo único que el pipeline resuelve una sola
vez al arrancar.

> **`always_infer`** (por defecto `true`): la cámara corre YOLO aunque nadie
> esté mirando el stream, así que **sigue detectando con el navegador
> cerrado** — y por tanto ocupa GPU mientras esté despierta (medido, una cámara
> con `yolo26m` ≈ 29 % de una GTX 1080). Ponlo a `false` en una cámara que solo
> quieras usar como vídeo. No es lo mismo que `default_infer`, que solo decide
> si `/stream` y `/snapshot` devuelven anotado o crudo.

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
real del PIR de la placa (`huerta_awake` on/off); en el uso normal no hace
falta llamar a `/start` a mano.

### Otros endpoints

- `GET /health` — vivo / no vivo
- `GET /config`, `POST /config/keepalive` — configuración global
  (`enabled`, `interval_sec`, `idle_limit_sec`: segundos sin frames tras los
  cuales se deja de calentar la GPU, ver
  [`docs/CICLO-DE-VIDA.md`](docs/CICLO-DE-VIDA.md))
- `GET /cameras`, `POST /cameras`, `DELETE /cameras/{camera_id}` — alta/baja
- `POST /cameras/{camera_id}/esphome/awake` — forzar "despierto"

Los ajustes de una cámara cuelgan de `/config/`:

- `POST /cameras/{camera_id}/config/inference` — `confidence`, `imgsz`,
  `classes`, `always_infer`, y también el `model_name` y el `device`. Va en
  JSON: en Swagger el desplegable **Examples** del body lista cada cámara
  con sus valores actuales, así que eliges la tuya, tocas lo que quieras y
  envías (recarga `/docs` para ver cambios recientes). Lo que no se envía se
  conserva, `classes: null` = todas las clases, y cambiar `model_name` o
  `device` relanza los hilos solo. El resto van por formulario:
- `POST /cameras/{camera_id}/config/keepalive` — override del keep-alive
- `POST /cameras/{camera_id}/config/stream` — `default_infer`
- `POST /cameras/{camera_id}/config/servo` — la torreta pan/tilt
- `POST /detect-file` — prueba puntual sobre una imagen subida, eligiendo
  modelo/confianza/resolución. Devuelve JSON con las detecciones, o con
  `annotated=true` la imagen con las cajas pintadas y un overlay de
  modelo / ms / número de detecciones

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
| **aioesphomeapi** | Cliente de la API nativa de ESPHome (puerto 6053, no HTTP). Lo usa `EsphomeController` para suscribirse a `huerta_awake`. |

---

## Documentación

| Fichero | Qué cubre |
| --- | --- |
| [`docs/ARQUITECTURA.md`](docs/ARQUITECTURA.md) | El reparto por módulos y qué hace cada clase, método y endpoint, con diagramas (UML, flujo, secuencia). |
| [`docs/CICLO-DE-VIDA.md`](docs/CICLO-DE-VIDA.md) | Cuánto vive cada instancia, quién la destruye y cómo se cierran los sockets cuando la placa se duerme. |

---

## Tests

```powershell
venv\Scripts\python.exe test\test_shutdown.py
venv\Scripts\python.exe test\test_shutdown_e2e.py
```

Ninguno de los dos necesita el ESP32: simulan una placa dormida con
`192.0.2.1` (TEST-NET-1) y una despierta con un servidor MJPEG local.

- **`test/test_shutdown.py`** — al parar una cámara se cierran de verdad la
  conexión del stream, el socket de la API nativa y el event loop del
  `EsphomeController`, y ningún hilo se queda vivo ni muere con un traceback.
  También cubre que un generador MJPEG sale solo con la señal de apagado y que
  el hook de señal delega en el handler de uvicorn.
- **`test/test_shutdown_e2e.py`** — levanta un uvicorn **de verdad** en un
  directorio temporal (no toca tu `cameras_config.json`), abre dos streams y le
  manda un Ctrl+C. Comprueba que el apagado no se come el
  `--timeout-graceful-shutdown` ni suelta `CancelledError`. Tarda ~20 s.

El resto de scripts de `test/` son pruebas manuales que sí necesitan la placa.

---

## Historial de depuración

Las notas sobre los P-states de la GTX 1080 y sobre por qué el filtrado de
confianza / ByteTrack **no** era la causa de los cuelgues están en los
comentarios de `_process_loop` dentro de `camera.py`.
