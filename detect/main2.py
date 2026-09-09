"""
Servicio YOLO multi-cámara para Home Assistant / ESPHome.

INSTALACIÓN
------------
    python -m venv yolo-env
    yolo-env\\Scripts\\activate          (Windows)
    pip install fastapi uvicorn pydantic ultralytics opencv-python numpy requests torch aioesphomeapi

    Para GPU NVIDIA, instala la versión de torch con soporte CUDA en vez de
    la genérica (ajusta cu124 a tu versión de CUDA):
    pip install torch --index-url https://download.pytorch.org/whl/cu124

    Descarga el modelo YOLO que vayas a usar (p.ej. yolo11m.pt) y déjalo en
    el mismo directorio que este script, o dale la ruta completa en
    "model_name" al registrar la cámara.

ARRANQUE
--------
    uvicorn main2:app --host 0.0.0.0 --port 8080 --workers 1 --timeout-graceful-shutdown 5

    El flag --timeout-graceful-shutdown 5 es OBLIGATORIO (ver más abajo en
    "historial de depuración" por qué). Sin --workers 1 no uses más de un
    worker: el estado de las cámaras vive en memoria de un solo proceso.

    Documentación interactiva una vez arrancado: http://localhost:8080/docs

REGISTRAR UNA CÁMARA
---------------------
    curl -X POST http://localhost:8080/cameras -H "Content-Type: application/json" -d '{
      "camera_id": "huerta",
      "stream_url": "http://192.168.1.50:8080/",
      "model_name": "yolo26m",
      "device": "cuda",
      "confidence": 0.5,
      "classes": [0],
      "noise_psk": "<api.encryption.key del YAML de la placa>"
    }'

    también se puede registrar desde Swagger (http://localhost:8080/docs) con el mismo JSON.

    Se guarda automáticamente en cameras_config.json (mismo directorio) y
    se recarga sola en el siguiente arranque -- no hace falta re-registrarla
    cada vez.

USO
---
    Ver stream anotado:   http://localhost:8080/cameras/{camera_id}/stream
    Ver stream crudo:     http://localhost:8080/cameras/{camera_id}/stream?infer=false
    Snapshot suelto:      http://localhost:8080/cameras/{camera_id}/snapshot
    Estado/diagnóstico:   http://localhost:8080/cameras/{camera_id}/status
    Arrancar/parar a mano: POST /cameras/{camera_id}/start | /stop

    Con "noise_psk" configurado, la cámara arranca/para sola siguiendo el
    estado real del PIR de la placa (huerta_estado on/off) -- no hace falta
    llamar a /start manualmente en el uso normal.


Arquitectura por cámara (CameraSession):
- 1 hilo lector (_read_loop): habla con el ESP32-S3-CAM y solo se queda con
  el frame más reciente. El ESP32 (esp32_camera_web_server) solo admite UN
  consumidor de stream a la vez, así que este es el único proceso que abre
  conexión contra él.
- 1 hilo de proceso (_process_loop): saca el frame más reciente, corre YOLO
  SOLO si hay algún cliente pidiendo el stream anotado, y publica el
  resultado (crudo y/o anotado) para que lo consuman N clientes a la vez
  mediante una threading.Condition. Así varios clientes (HA, navegador...)
  pueden mirar el mismo stream sin abrir varias conexiones al ESP32 ni
  duplicar la inferencia.
- Arranque/parada: cada sesión tiene un flag `explicit_start`. Si se activa
  por API (/start), la sesión se mantiene viva aunque no haya clientes. Si
  no, arranca sola con el primer cliente y se para sola cuando se va el
  último (evita insistir en conectar a una cámara que el PIR ha dormido).

Aquí tienes cada una, en el orden en que las usa main2.py:

fastapi — el framework web en sí. Define los endpoints (/cameras, /stream, etc.), valida los datos de entrada/salida y genera automáticamente la documentación interactiva de /docs.
uvicorn — el servidor que realmente ejecuta tu app FastAPI y escucha en el puerto 8080. FastAPI define qué hacer con cada petición; uvicorn es el que abre el socket, acepta conexiones y se las pasa a FastAPI.
pydantic — valida y estructura los datos. Es lo que hace que CameraConfig/InferenceConfig/etc. sean clases con tipos comprobados en vez de diccionarios sueltos: si mandas un confidence que no es un número, Pydantic rechaza la petición antes de que llegue a tu código.
ultralytics — la librería del modelo YOLO en sí (YOLO(...), model.predict()). Es la que carga los pesos .pt y hace la detección de objetos.
opencv-python (se importa como cv2) — procesamiento de imagen: decodificar los JPEGs que llegan del ESP32 (cv2.imdecode), dibujar las cajas y texto sobre el frame (cv2.rectangle, cv2.putText), y volver a codificar a JPEG para servirlo (cv2.imencode).
numpy — la estructura de datos numérica de base sobre la que trabajan tanto OpenCV como PyTorch/YOLO. Un frame de vídeo es, por debajo, un array de numpy (np.frombuffer, np.zeros para el keep-alive).
requests — cliente HTTP para conectarse al stream MJPEG del ESP32 (requests.get(..., stream=True)) y leerlo trozo a trozo con iter_content().
torch (PyTorch) — el motor de deep learning sobre el que corre YOLO por debajo. Tú lo usas directamente para mover el modelo a la GPU (model.to("cuda")) y para el ajuste de torch.backends.cudnn.enabled = False que necesitas por el bug de la GTX 1080.
aioesphomeapi — cliente de la API nativa de ESPHome (puerto 6053, no HTTP). Es lo que usa tu EsphomeController para suscribirse a huerta_estado y, en el futuro, para mandar

"""

from __future__ import annotations

import asyncio
import io
import json
import queue
import re
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import cv2
import numpy as np
import requests
import torch
from aioesphomeapi import APIClient, ReconnectLogic
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from ultralytics import YOLO

# Necesario en la GTX 1080 (Pascal) para evitar "CUDA misaligned address" con cuDNN
torch.backends.cudnn.enabled = False

# Dónde se guarda la lista de cámaras registradas para que sobrevivan a un reinicio
CAMERAS_CONFIG_FILE = Path(__file__).with_name("cameras_config.json")

# Estilo de las cajas de detección
BOX_COLOR = (0, 220, 60)  # BGR: verde, para diferenciarlo del texto/overlay
BOX_THICKNESS = 3
FONT_SCALE = 0.6
FONT_THICKNESS = 2
CENTER_DOT_RADIUS = 4

# Para parsear el framing multipart del stream del ESP32 usando el
# Content-Length real que declara cada parte
_CONTENT_LENGTH_RE = re.compile(rb"Content-Length:\s*(\d+)", re.IGNORECASE)


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_cameras_from_disk()
    yield
    print("Apagando: deteniendo todas las cámaras...")
    sessions = list(CAMERAS.values())

    async def _shutdown_one(session: "CameraSession"):
        try:
            # session.shutdown() es síncrona/bloqueante (hace .join() sobre
            # hilos); la corremos en un hilo aparte para no congelar el
            # event loop principal, que es lo que le impedía a uvicorn
            # terminar de cerrar conexiones de clientes activos a tiempo.
            await asyncio.wait_for(asyncio.to_thread(session.shutdown), timeout=5.0)
        except asyncio.TimeoutError:
            print(f"[{session.cfg.camera_id}] shutdown tardó más de 5s, "
                  f"continuando de todas formas (hilo daemon, no bloquea el cierre)")

    await asyncio.gather(*(_shutdown_one(s) for s in sessions))
    print("Todas las cámaras detenidas, cerrando.")


app = FastAPI(title="YOLO Camera Service", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Config global, modificable en runtime vía API
# ---------------------------------------------------------------------------
class GlobalConfig:
    def __init__(self):
        self.keepalive_enabled: bool = True
        self.keepalive_interval_sec: float = 0.05
        self.reconnect_delay_sec: float = 1.0

    def as_dict(self):
        return {
            "keepalive_enabled": self.keepalive_enabled,
            "keepalive_interval_sec": self.keepalive_interval_sec,
            "reconnect_delay_sec": self.reconnect_delay_sec,
        }


GLOBAL_CONFIG = GlobalConfig()


# ---------------------------------------------------------------------------
# Modelos YOLO cacheados (compartidos entre cámaras que usen el mismo modelo)
# ---------------------------------------------------------------------------

_loaded_models: dict[str, YOLO] = {}
_models_lock = threading.Lock()


def get_model(name: str, device: str = "cuda") -> YOLO:
    key = f"{name}_{device}"
    with _models_lock:
        if key not in _loaded_models:
            m = YOLO(f"{name}.pt")
            m.to(device)
            dummy = np.zeros((640, 640, 3), dtype=np.uint8)
            m.predict(dummy, verbose=False)
            _loaded_models[key] = m
            print(f"Modelo {name} precalentado en {device} y listo")
        return _loaded_models[key]


# Modelo por defecto para el endpoint /detect (URL de imagen suelta).
# Se carga de forma perezosa en la primera petición para no bloquear el
# arranque si no se usa.
DEFAULT_MODEL_NAME = "yolo11n"
DEFAULT_MODEL_DEVICE = "cuda"


class ImageRequest(BaseModel):
    image_url: str
    confidence: float = 0.5


# ---------------------------------------------------------------------------
# EsphomeController: conexión persistente a la API nativa de ESPHome
# (puerto 6053) de una placa, en su propio hilo con loop de asyncio propio,
# para poder llamarla/leerla desde hilos síncronos (como CameraSession)
# sin bloquearlos.
#
# De momento cubre dos usos:
#   - move_servo(pan, tilt): llama al servicio "set_servo_position" del
#     YAML si existe (no falla si aún no está definido, solo lo ignora).
#   - Vigila un text_sensor concreto (por object_id, p.ej. "huerta_estado")
#     y llama a on_state_text(valor) cada vez que cambia. Así una
#     CameraSession puede arrancar/parar en función del estado real del
#     hardware (PIR + deep sleep) en vez de solo por clientes HTTP.
#
# Reconexión: usa ReconnectLogic de la propia aioesphomeapi (la misma que
# usa Home Assistant), con backoff creciente si se cae la conexión.
# ---------------------------------------------------------------------------

class EsphomeController:
    def __init__(
        self,
        address: str,
        noise_psk: Optional[str] = None,
        port: int = 6053,
        watch_text_sensor_object_id: Optional[str] = None,
        on_state_text: Optional[Callable[[Optional[str]], None]] = None,
    ):
        self.address = address
        self.port = port
        self.noise_psk = noise_psk
        self.watch_text_sensor_object_id = watch_text_sensor_object_id
        self.on_state_text = on_state_text

        self._client: Optional[APIClient] = None
        self._servo_service = None
        self._watch_key: Optional[int] = None
        self._connected = threading.Event()
        self._stopping = False

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name=f"esphome-{address}"
        )
        self._thread.start()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._setup())
        self._loop.run_forever()

    async def _setup(self):
        self._client = APIClient(self.address, self.port, password="", noise_psk=self.noise_psk)

        async def on_connect():
            t0 = time.perf_counter()
            entities, services = await self._client.list_entities_services()
            self._servo_service = next(
                (s for s in services if s.name == "set_servo_position"), None
            )

            self._watch_key = None
            if self.watch_text_sensor_object_id:
                for e in entities:
                    if getattr(e, "object_id", None) == self.watch_text_sensor_object_id:
                        self._watch_key = e.key
                        break
                if self._watch_key is None:
                    print(
                        f"EsphomeController[{self.address}]: aviso, no encontré "
                        f"text_sensor '{self.watch_text_sensor_object_id}'"
                    )

            self._client.subscribe_states(self._on_state)
            self._connected.set()
            print(
                f"EsphomeController[{self.address}]: conectado en "
                f"{(time.perf_counter() - t0) * 1000:.0f}ms "
                f"({len(entities)} entidades, servo_service={'sí' if self._servo_service else 'no'})"
            )

        async def on_disconnect(expected_disconnect: bool):
            self._connected.clear()
            if not self._stopping:
                print(
                    f"EsphomeController[{self.address}]: desconectado "
                    f"(esperado={expected_disconnect}), reconectando..."
                )

        reconnect_logic = ReconnectLogic(
            client=self._client,
            on_connect=on_connect,
            on_disconnect=on_disconnect,
            zeroconf_instance=None,
            name=self.address,
        )
        await reconnect_logic.start()

    def _on_state(self, state):
        # Llamado en el hilo/loop propio de este controller.
        if self._watch_key is not None and getattr(state, "key", None) == self._watch_key:
            value = getattr(state, "state", None)
            print(f"EsphomeController[{self.address}]: '{self.watch_text_sensor_object_id}' -> {value!r}")
            if self.on_state_text is not None:
                try:
                    self.on_state_text(value)
                except Exception as e:
                    print(f"EsphomeController[{self.address}]: error en on_state_text callback:", repr(e))

    # -- servos --------------------------------------------------------

    def move_servo(self, pan: float, tilt: float):
        if not self._connected.is_set() or self._servo_service is None:
            return
        asyncio.run_coroutine_threadsafe(self._send_servo(pan, tilt), self._loop)

    async def _send_servo(self, pan: float, tilt: float):
        try:
            self._client.execute_service(self._servo_service, {"pan": pan, "tilt": tilt})
        except Exception as e:
            print(f"EsphomeController[{self.address}]: error enviando servo:", repr(e))

    # -- apagado ---------------------------------------------------------

    def shutdown(self):
        self._stopping = True
        if self._client is not None:
            asyncio.run_coroutine_threadsafe(self._client.disconnect(), self._loop)
        self._loop.call_soon_threadsafe(self._loop.stop)


# ---------------------------------------------------------------------------
# Config de una cámara
# ---------------------------------------------------------------------------

class CameraConfig(BaseModel):
    camera_id: str
    stream_url: str
    model_name: str = "yolo11m"
    device: str = "cuda"
    confidence: float = 0.5
    imgsz: int = 640
    classes: Optional[list[int]] = None  # None = todas las clases
    keepalive_enabled: Optional[bool] = None  # None = usa GLOBAL_CONFIG
    default_infer: bool = True  # usado por /stream y /snapshot cuando no se pasa ?infer=

    # Si se rellena, la sesión abre también una conexión a la API nativa de
    # ESPHome (puerto 6053) de la misma placa (host sacado de stream_url), y
    # usa el text_sensor indicado para arrancar/parar la lectura en vez de
    # depender solo de que haya clientes HTTP mirando el stream.
    noise_psk: Optional[str] = None  # api.encryption.key del YAML de la placa
    esphome_state_object_id: Optional[str] = "huerta_estado"


# ---------------------------------------------------------------------------
# Sesión de cámara
# ---------------------------------------------------------------------------

class CameraSession:
    def __init__(self, cfg: CameraConfig):
        self.cfg = cfg
        self._lock = threading.Lock()

        self._reader_thread: Optional[threading.Thread] = None
        self._processing_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._raw_queue: "queue.Queue" = queue.Queue(maxsize=1)

        # último frame publicado en cada modo + condición para avisar a los generadores
        self._cond = threading.Condition()
        self._latest_raw_jpeg: Optional[bytes] = None
        self._latest_annotated_jpeg: Optional[bytes] = None
        self._frame_seq = 0

        # 3 contadores de clientes: fijos a true/false, o "follow" (siguen
        # cfg.default_infer en caliente, sin tener que reconectar el stream)
        self._infer_clients = 0
        self._raw_clients = 0
        self._follow_clients = 0
        self.explicit_start = False

        # referencia a la conexión HTTP abierta con el ESP32, para poder
        # cerrarla a la fuerza al parar (si no, iter_content puede quedarse
        # bloqueado hasta el timeout de lectura)
        self._current_response: Optional[requests.Response] = None

        # ventana deslizante (últimos ~1s) para calcular fps sin que baile
        self._fps_window: deque[float] = deque()

        # métricas para /status
        self.last_frame_time: Optional[float] = None
        self.last_inference_ms: Optional[float] = None
        self.pipeline_fps: float = 0.0
        self.last_error: Optional[str] = None

        # Conexión opcional a la API nativa de ESPHome de la misma placa,
        # para arrancar/parar la sesión según el estado real del hardware
        # (huerta_estado: "on"/"off") en vez de solo por clientes HTTP.
        self.esphome: Optional[EsphomeController] = None
        self._last_esphome_state: Optional[str] = None
        if cfg.noise_psk:
            host = urlparse(cfg.stream_url).hostname
            if host is None:
                print(f"[{cfg.camera_id}] noise_psk configurado pero no pude sacar el host de stream_url={cfg.stream_url!r}")
            else:
                self.esphome = EsphomeController(
                    address=host,
                    noise_psk=cfg.noise_psk,
                    watch_text_sensor_object_id=cfg.esphome_state_object_id,
                    on_state_text=self._on_esphome_state,
                )

    def _on_esphome_state(self, value: Optional[str]):
        if value is None:
            return
        v = value.strip().lower()
        if v == self._last_esphome_state:
            return  # ignora repeticiones (p.ej. "on" republicado al reconectar)
        self._last_esphome_state = v
        if v == "on":
            print(f"[{self.cfg.camera_id}] {self.cfg.esphome_state_object_id}=on -> arrancando cámara")
            self.start(explicit=True)
        elif v == "off":
            print(f"[{self.cfg.camera_id}] {self.cfg.esphome_state_object_id}=off -> deteniendo cámara")
            self.stop(explicit=True)

    # -- arranque / parada ---------------------------------------------

    @property
    def is_running(self) -> bool:
        reader_alive = self._reader_thread is not None and self._reader_thread.is_alive()
        proc_alive = self._processing_thread is not None and self._processing_thread.is_alive()
        return reader_alive or proc_alive

    @property
    def client_count(self) -> int:
        return self._raw_clients + self._infer_clients + self._follow_clients

    def start(self, explicit: bool = False):
        with self._lock:
            if explicit:
                self.explicit_start = True
            if self.is_running:
                return
            # Objetos NUEVOS por generación, no reciclados. Si reutilizásemos
            # el mismo _stop_event de siempre (solo con .clear()), un hilo
            # "viejo" que todavía no se hubiera enterado del stop() anterior
            # (bloqueado en su queue.get() con timeout) vería el evento ya
            # limpio al despertar y seguiría corriendo como si nada -> dos
            # generaciones de hilos activas a la vez, pisándose datos
            # compartidos (self._fps_window, etc.). Con un objeto nuevo cada
            # vez, el hilo viejo sigue viendo SU PROPIO evento (el de antes,
            # que sigue en set()) y se para solo, sin tocar la generación nueva.
            self._stop_event = threading.Event()
            self._raw_queue = queue.Queue(maxsize=1)
            self._fps_window = deque()
            self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
            self._processing_thread = threading.Thread(target=self._process_loop, daemon=True)
            self._reader_thread.start()
            self._processing_thread.start()
            print(f"[{self.cfg.camera_id}] lectura iniciada")

    def stop(self, explicit: bool = False):
        with self._lock:
            if explicit:
                self.explicit_start = False
            if self.client_count > 0 and not explicit:
                return  # aún hay clientes mirando, no paramos
            self._stop_event.set()
            resp = self._current_response
        # cerrar la conexión HTTP abierta fuera del lock, para no bloquearlo
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        with self._cond:
            self._cond.notify_all()  # despierta YA a los generadores esperando, sin esperar al polling de 5s
        print(f"[{self.cfg.camera_id}] lectura detenida")

    def shutdown(self):
        """Para la sesión y espera a que los hilos terminen. Para usar al apagar el servicio."""
        self.stop(explicit=True)
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=3)
        if self._processing_thread is not None:
            self._processing_thread.join(timeout=3)
        with self._cond:
            self._cond.notify_all()  # despierta a los generadores que sigan esperando
        if self.esphome is not None:
            self.esphome.shutdown()

    def _maybe_autostop(self):
        with self._lock:
            if not self.explicit_start and self.client_count == 0:
                self._stop_event.set()
                print(f"[{self.cfg.camera_id}] sin clientes, lectura detenida automáticamente")

    # -- suscripción de clientes -----------------------------------------

    @staticmethod
    def _resolve_mode(infer: Optional[bool]) -> str:
        """None = 'follow' (sigue cfg.default_infer en caliente, editable por API)."""
        if infer is True:
            return "true"
        if infer is False:
            return "false"
        return "follow"

    def add_client(self, mode: str):
        with self._lock:
            if mode == "true":
                self._infer_clients += 1
            elif mode == "false":
                self._raw_clients += 1
            else:
                self._follow_clients += 1
        if self.esphome is None:
            # Arranque perezoso solo para cámaras SIN control ESPHome. Con
            # ESPHome, arrancar aquí sin saber si la placa está despierta
            # provocaría un _read_loop reintentando en bucle contra una
            # cámara dormida -> stream congelado/en blanco para el cliente.
            # El propio huerta_estado="on" ya llama a start() cuando toca;
            # el generador solo se queda esperando frames hasta entonces.
            self.start(explicit=False)

    def remove_client(self, mode: str):
        with self._lock:
            if mode == "true":
                self._infer_clients = max(0, self._infer_clients - 1)
            elif mode == "false":
                self._raw_clients = max(0, self._raw_clients - 1)
            else:
                self._follow_clients = max(0, self._follow_clients - 1)
        self._maybe_autostop()

    # -- hilo lector: solo habla con el ESP32 ----------------------------

    def _read_loop(self):
        # Referencias LOCALES de esta generación de hilo. Aunque start() cree
        # objetos nuevos para self._stop_event/self._raw_queue más adelante
        # (otra generación), este hilo sigue mirando los suyos propios y se
        # para solo cuando SU stop_event se activa, sin pisarse con el nuevo.
        stop_event = self._stop_event
        raw_queue = self._raw_queue

        while not stop_event.is_set():
            r = None
            try:
                r = requests.get(self.cfg.stream_url, stream=True, timeout=(5, 15))
                with self._lock:
                    self._current_response = r
                buffer = b""
                # None = esperando la cabecera de la siguiente parte del
                # multipart; int = bytes de JPEG que todavía faltan por leer
                # de la parte actual. Usamos el Content-Length que el propio
                # ESP32 declara en cada parte (esp32_camera_web_server manda
                # "Content-Type: image/jpeg\r\nContent-Length: N\r\n\r\n"
                # antes de cada frame) en vez de buscar a mano los marcadores
                # \xff\xd8/\xff\xd9: ese método se desincronizaba si esos
                # bytes aparecían por casualidad DENTRO de los datos
                # comprimidos de un JPEG real, provocando cuelgues de hasta
                # varios minutos (confirmado con pruebas físicas moviendo la
                # cámara). El Content-Length es la fuente de verdad real del
                # framing, igual que usa un navegador para renderizar el
                # mismo stream sin problemas.
                expected_len: Optional[int] = None
                for chunk in r.iter_content(chunk_size=4096):
                    if stop_event.is_set():
                        break
                    buffer += chunk

                    progressed = True
                    while progressed:
                        progressed = False
                        if expected_len is None:
                            idx = buffer.find(b"\r\n\r\n")
                            if idx != -1:
                                header_block = buffer[:idx]
                                m = _CONTENT_LENGTH_RE.search(header_block)
                                buffer = buffer[idx + 4:]
                                if m:
                                    expected_len = int(m.group(1))
                                # si el bloque no traía Content-Length (p.ej.
                                # es solo la línea del boundary suelta), no
                                # fijamos expected_len y reintentamos con el
                                # siguiente \r\n\r\n que aparezca
                                progressed = True
                        else:
                            if len(buffer) >= expected_len:
                                jpg = buffer[:expected_len]
                                buffer = buffer[expected_len:]
                                expected_len = None
                                nparr = np.frombuffer(jpg, np.uint8)
                                frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                                if frame is not None:
                                    if raw_queue.full():
                                        try:
                                            raw_queue.get_nowait()
                                        except queue.Empty:
                                            pass
                                    raw_queue.put(frame)
                                progressed = True

                    if len(buffer) > 2_000_000:
                        buffer = b""
                        expected_len = None
            except requests.exceptions.RequestException as e:
                self.last_error = repr(e)
                # Si nadie quiere ya la cámara (p.ej. se durmió por el PIR y no hay
                # clientes ni arranque explícito), dejamos de insistir en reconectar.
                if not self.explicit_start and self.client_count == 0:
                    break
                print(f"[{self.cfg.camera_id}] stream interrumpido ({e!r}), "
                      f"reconectando en {GLOBAL_CONFIG.reconnect_delay_sec}s...")
                time.sleep(GLOBAL_CONFIG.reconnect_delay_sec)
                continue
            finally:
                with self._lock:
                    if self._current_response is r:
                        self._current_response = None
                if r is not None:
                    r.close()

    # -- hilo de proceso: YOLO + keep-alive de GPU -----------------------
    #
    # Diagnóstico confirmado (31/08/2026): la GTX 1080 produce inferencias
    # corruptas (confianzas fuera de 0-1) cuando el driver baja el estado de
    # energía (P5) entre frames. Se confirmó con nvidia-smi que las anomalías
    # coinciden al segundo exacto con transiciones de P-state. La solución es
    # mantener la GPU activamente ocupada con inferencias mínimas cuando no
    # ha llegado un frame real, en vez de bloquearse sin límite en la cola.

    def _update_fps(self, fps_window: deque):
        """Fps calculado como media sobre la última ventana de ~1s, para que no baile frame a frame."""
        now = time.time()
        fps_window.append(now)
        while fps_window and now - fps_window[0] > 1.0:
            fps_window.popleft()
        if len(fps_window) >= 2:
            span = fps_window[-1] - fps_window[0]
            if span > 0:
                self.pipeline_fps = (len(fps_window) - 1) / span

    def _process_loop(self):
        # Referencias LOCALES de esta generación (ver comentario en _read_loop
        # sobre por qué no se puede usar self._stop_event/self._raw_queue
        # directamente: una nueva generación podría reasignarlos mientras
        # este hilo sigue vivo).
        stop_event = self._stop_event
        raw_queue = self._raw_queue
        fps_window: deque = deque()

        model = get_model(self.cfg.model_name, self.cfg.device)
        is_cuda = self.cfg.device.startswith("cuda")

        while not stop_event.is_set():
            keepalive_on = (
                self.cfg.keepalive_enabled
                if self.cfg.keepalive_enabled is not None
                else GLOBAL_CONFIG.keepalive_enabled
            )
            wait_timeout = GLOBAL_CONFIG.keepalive_interval_sec if (is_cuda and keepalive_on) else 1.0

            try:
                frame = raw_queue.get(timeout=wait_timeout)
            except queue.Empty:
                if is_cuda and keepalive_on:
                    try:
                        model.predict(np.zeros((640, 640, 3), dtype=np.uint8), imgsz=640, verbose=False)
                    except Exception as e:
                        print(f"[{self.cfg.camera_id}] keep-alive de GPU falló:", repr(e))
                if stop_event.is_set():
                    break
                continue

            self._update_fps(fps_window)

            # "follow" cuenta como si pidiera inferencia solo si cfg.default_infer
            # está a true en este momento; como se relee en cada frame, cambiar
            # default_infer por API afecta a los clientes ya conectados sin que
            # tengan que reconectar.
            want_infer = self._infer_clients > 0 or (self._follow_clients > 0 and self.cfg.default_infer)
            want_raw = self._raw_clients > 0 or (self._follow_clients > 0 and not self.cfg.default_infer)
            annotated_bytes = None
            raw_bytes = None

            if want_infer:
                try:
                    t0 = time.time()
                    results = model.track(
                        frame, persist=True, conf=self.cfg.confidence,
                        imgsz=self.cfg.imgsz, verbose=False,
                        tracker="bytetrack.yaml", classes=self.cfg.classes,
                    )[0]
                    inference_ms = (time.time() - t0) * 1000
                    annotated = frame.copy()
                    for box in results.boxes:
                        x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
                        label = model.names[int(box.cls)]
                        conf_val = float(box.conf[0])
                        tid = int(box.id) if box.id is not None else -1
                        cx, cy = int((x1 + x2) / 2), int((y1 + y2) / 2)
                        cv2.rectangle(annotated, (int(x1), int(y1)), (int(x2), int(y2)), BOX_COLOR, BOX_THICKNESS)
                        cv2.circle(annotated, (cx, cy), CENTER_DOT_RADIUS, BOX_COLOR, -1)
                        cv2.putText(annotated, f"{label} {conf_val:.2f} #{tid}", (int(x1), int(y1) - 8),
                                    cv2.FONT_HERSHEY_SIMPLEX, FONT_SCALE, BOX_COLOR, FONT_THICKNESS)

                    cv2.putText(annotated, f"{inference_ms:.0f} ms ({self.cfg.device}) | "
                                            f"{self.pipeline_fps:.1f} fps", (10, 20),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)

                    ok, buf = cv2.imencode(".jpg", annotated)
                    if ok:
                        annotated_bytes = buf.tobytes()
                    self.last_inference_ms = inference_ms
                except Exception as e:
                    self.last_error = repr(e)
                    print(f"[{self.cfg.camera_id}] error en track/dibujo:", repr(e))

            if want_raw or annotated_bytes is None:
                ok, buf = cv2.imencode(".jpg", frame)
                if ok:
                    raw_bytes = buf.tobytes()

            with self._cond:
                if raw_bytes is not None:
                    self._latest_raw_jpeg = raw_bytes
                if annotated_bytes is not None:
                    self._latest_annotated_jpeg = annotated_bytes
                self._frame_seq += 1
                self.last_frame_time = time.time()
                self._cond.notify_all()

    # -- salida hacia los clientes ----------------------------------------

    def mjpeg_generator(self, infer: Optional[bool]):
        mode = self._resolve_mode(infer)
        self.add_client(mode)
        last_seq_seen = -1
        try:
            while not self._stop_event.is_set():
                with self._cond:
                    self._cond.wait_for(lambda: self._frame_seq != last_seq_seen or self._stop_event.is_set(),
                                         timeout=5.0)
                    if self._stop_event.is_set():
                        break
                    last_seq_seen = self._frame_seq
                    # en modo "follow" se relee cfg.default_infer en cada frame, así
                    # que basta con cambiarlo por API para que este stream ya abierto
                    # cambie de crudo a anotado (o viceversa) sin reconectar
                    use_infer = self.cfg.default_infer if mode == "follow" else (mode == "true")
                    jpg = self._latest_annotated_jpeg if use_infer else self._latest_raw_jpeg
                if jpg is None:
                    continue
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n"
                    + jpg + b"\r\n"
                )
        finally:
            self.remove_client(mode)

    def snapshot(self, infer: Optional[bool]) -> Optional[bytes]:
        use_infer = self.cfg.default_infer if infer is None else infer
        with self._cond:
            return self._latest_annotated_jpeg if use_infer else self._latest_raw_jpeg

    def status(self) -> dict:
        return {
            "camera_id": self.cfg.camera_id,
            "running": self.is_running,
            "explicit_start": self.explicit_start,
            "raw_clients": self._raw_clients,
            "infer_clients": self._infer_clients,
            "follow_clients": self._follow_clients,
            "last_frame_time": self.last_frame_time,
            "last_inference_ms": self.last_inference_ms,
            "pipeline_fps": round(self.pipeline_fps, 1),
            "last_error": self.last_error,
            "esphome_connected": self.esphome.is_connected if self.esphome else None,
            "config": self.cfg.dict(),
        }


# ---------------------------------------------------------------------------
# Registro de cámaras, persistido en CAMERAS_CONFIG_FILE para sobrevivir a
# un reinicio del servicio (se recargan solas al arrancar)
# ---------------------------------------------------------------------------

CAMERAS: dict[str, CameraSession] = {}
_cameras_lock = threading.Lock()


def save_cameras_to_disk():
    with _cameras_lock:
        data = [s.cfg.dict() for s in CAMERAS.values()]
    try:
        CAMERAS_CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    except OSError as e:
        print(f"No se pudo guardar {CAMERAS_CONFIG_FILE}: {e!r}")


def load_cameras_from_disk():
    if not CAMERAS_CONFIG_FILE.exists():
        return
    try:
        data = json.loads(CAMERAS_CONFIG_FILE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"No se pudo leer {CAMERAS_CONFIG_FILE}: {e!r}")
        return
    for entry in data:
        try:
            register_camera(CameraConfig(**entry), persist=False)
        except Exception as e:
            print(f"Cámara inválida en {CAMERAS_CONFIG_FILE}: {entry!r} ({e!r})")
    if data:
        print(f"{len(data)} cámara(s) cargada(s) desde {CAMERAS_CONFIG_FILE}")


def register_camera(cfg: CameraConfig, persist: bool = True) -> CameraSession:
    with _cameras_lock:
        if cfg.camera_id in CAMERAS:
            raise HTTPException(400, f"La cámara '{cfg.camera_id}' ya existe")
        session = CameraSession(cfg)
        CAMERAS[cfg.camera_id] = session
    if persist:
        save_cameras_to_disk()
    return session


def get_camera(camera_id: str) -> CameraSession:
    session = CAMERAS.get(camera_id)
    if session is None:
        raise HTTPException(404, f"Cámara '{camera_id}' no registrada")
    return session


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "cameras": list(CAMERAS.keys())}


@app.get("/config")
async def get_config():
    return GLOBAL_CONFIG.as_dict()


class KeepaliveConfig(BaseModel):
    enabled: Optional[bool] = None
    interval_sec: Optional[float] = None


@app.post("/config/keepalive")
async def set_global_keepalive(cfg: KeepaliveConfig):
    if cfg.enabled is not None:
        GLOBAL_CONFIG.keepalive_enabled = cfg.enabled
    if cfg.interval_sec is not None:
        GLOBAL_CONFIG.keepalive_interval_sec = cfg.interval_sec
    return GLOBAL_CONFIG.as_dict()


@app.get("/cameras")
async def list_cameras():
    return [s.status() for s in CAMERAS.values()]


@app.post("/cameras")
async def add_camera(cfg: CameraConfig):
    session = register_camera(cfg)
    return session.status()


@app.delete("/cameras/{camera_id}")
async def remove_camera(camera_id: str):
    with _cameras_lock:
        session = CAMERAS.pop(camera_id, None)
    if session is None:
        raise HTTPException(404, f"Cámara '{camera_id}' no registrada")
    session.shutdown()
    save_cameras_to_disk()
    return {"removed": camera_id}


@app.get("/cameras/{camera_id}/status")
async def camera_status(camera_id: str):
    return get_camera(camera_id).status()


@app.post("/cameras/{camera_id}/start")
async def start_camera(camera_id: str):
    session = get_camera(camera_id)
    session.start(explicit=True)
    return session.status()


@app.post("/cameras/{camera_id}/stop")
async def stop_camera(camera_id: str):
    session = get_camera(camera_id)
    session.stop(explicit=True)
    return session.status()


class InferenceConfig(BaseModel):
    confidence: Optional[float] = None
    imgsz: Optional[int] = None
    classes: Optional[list[int]] = None


@app.post("/cameras/{camera_id}/inference/config")
async def set_inference_config(camera_id: str, cfg: InferenceConfig):
    session = get_camera(camera_id)
    if cfg.confidence is not None:
        session.cfg.confidence = cfg.confidence
    if cfg.imgsz is not None:
        session.cfg.imgsz = cfg.imgsz
    if cfg.classes is not None:
        session.cfg.classes = cfg.classes
    save_cameras_to_disk()
    return session.status()


@app.post("/cameras/{camera_id}/config/keepalive")
async def set_camera_keepalive(camera_id: str, cfg: KeepaliveConfig):
    session = get_camera(camera_id)
    if cfg.enabled is not None:
        session.cfg.keepalive_enabled = cfg.enabled
    save_cameras_to_disk()
    return session.status()


class StreamDefaultConfig(BaseModel):
    default_infer: bool


@app.post("/cameras/{camera_id}/stream/config")
async def set_stream_default(camera_id: str, cfg: StreamDefaultConfig):
    """Cambia si /stream y /snapshot devuelven anotado o crudo por defecto (cuando
    no se pasa ?infer= explícito). Los streams ya abiertos en modo "por defecto"
    cambian en caliente, sin tener que reconectar."""
    session = get_camera(camera_id)
    session.cfg.default_infer = cfg.default_infer
    save_cameras_to_disk()
    return session.status()


@app.get("/cameras/{camera_id}/stream")
async def stream_camera(
    camera_id: str,
    infer: Optional[bool] = Query(
        None, description="true=anotado, false=crudo, omitido=usa default_infer de la cámara (editable por API)"
    ),
):
    session = get_camera(camera_id)
    return StreamingResponse(
        session.mjpeg_generator(infer),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/cameras/{camera_id}/snapshot")
async def snapshot_camera(camera_id: str, infer: Optional[bool] = Query(None)):
    session = get_camera(camera_id)
    mode = session._resolve_mode(infer)
    session.add_client(mode)
    try:
        deadline = time.time() + 1.0
        jpg = session.snapshot(infer)
        while jpg is None and time.time() < deadline:
            time.sleep(0.05)
            jpg = session.snapshot(infer)
    finally:
        session.remove_client(mode)
    if jpg is None:
        raise HTTPException(503, "Sin frame disponible todavía")
    return Response(content=jpg, media_type="image/jpeg")


@app.post("/detect")
async def detect(req: ImageRequest):
    response = requests.get(req.image_url, timeout=5)
    nparr = np.frombuffer(response.content, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return {"error": "No se pudo decodificar la imagen"}

    model = get_model(DEFAULT_MODEL_NAME, DEFAULT_MODEL_DEVICE)
    start = time.time()
    results = model.predict(img, verbose=False)[0]
    inference_ms = round((time.time() - start) * 1000, 1)

    detections = []
    for box in results.boxes:
        if float(box.conf) >= req.confidence:
            x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
            detections.append({
                "label": model.names[int(box.cls)],
                "confidence": round(float(box.conf), 3),
                "box": {"x1": round(x1, 1), "y1": round(y1, 1), "x2": round(x2, 1), "y2": round(y2, 1)},
                "center": {"x": round((x1 + x2) / 2, 1), "y": round((y1 + y2) / 2, 1)},
            })

    return {"detections": detections, "inference_ms": inference_ms}


@app.post("/detect-file")
async def detect_file(
    image: UploadFile = File(...),
    model_name: str = Form("yolo11n"),
    confidence: float = Form(0.5),
    imgsz: int = Form(640),
):
    """Prueba con imagen local + elige modelo/confianza/resolución"""
    contents = await image.read()
    nparr = np.frombuffer(contents, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return {"error": "No se pudo decodificar la imagen"}

    model = get_model(model_name)
    start = time.time()
    results = model.predict(img, conf=confidence, imgsz=imgsz, verbose=False)[0]
    inference_ms = round((time.time() - start) * 1000, 1)

    detections = [
        {"label": model.names[int(box.cls)], "confidence": round(float(box.conf), 3)}
        for box in results.boxes
    ]
    return {"model": model_name, "detections": detections, "inference_ms": inference_ms}

@app.post("/detect-file/annotated")
async def detect_file_annotated(
    image: UploadFile = File(...),
    model_name: str = Form("yolo11n"),
    confidence: float = Form(0.5),
    imgsz: int = Form(640),
):
    """Igual que arriba pero devuelve la imagen con las cajas dibujadas, para verlo directamente"""
    contents = await image.read()
    nparr = np.frombuffer(contents, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return {"error": "No se pudo decodificar la imagen"}

    model = get_model(model_name)
    results = model.predict(img, conf=confidence, imgsz=imgsz, verbose=False)[0]
    annotated = results.plot()  # numpy array con las cajas ya pintadas

    ok, buf = cv2.imencode(".jpg", annotated)
    return StreamingResponse(io.BytesIO(buf.tobytes()), media_type="image/jpeg")
