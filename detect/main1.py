"""
Servicio YOLO multi-cámara pensado para ser consumido por Home Assistant.

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

Historial de depuración de la GTX 1080 (P-states) y de que el filtrado de
confianza/ByteTrack no era el problema: ver notas originales más abajo,
en _process_loop.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import requests
import torch
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from ultralytics import YOLO

# Necesario en la GTX 1080 (Pascal) para evitar "CUDA misaligned address" con cuDNN
torch.backends.cudnn.enabled = False

# Dónde se guarda la lista de cámaras registradas para que sobrevivan a un reinicio
CAMERAS_CONFIG_FILE = Path(__file__).with_name("cameras_config1.json")

# Estilo de las cajas de detección
BOX_COLOR = (0, 220, 60)  # BGR: verde, para diferenciarlo del texto/overlay
BOX_THICKNESS = 3
FONT_SCALE = 0.6
FONT_THICKNESS = 2
CENTER_DOT_RADIUS = 4


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_cameras_from_disk()
    yield
    print("Apagando: deteniendo todas las cámaras...")
    for session in list(CAMERAS.values()):
        session.shutdown()
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


def get_model(name: str, device: str) -> YOLO:
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

    # -- arranque / parada ---------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._reader_thread is not None and self._reader_thread.is_alive()

    @property
    def client_count(self) -> int:
        return self._raw_clients + self._infer_clients + self._follow_clients

    def start(self, explicit: bool = False):
        with self._lock:
            if explicit:
                self.explicit_start = True
            if self.is_running:
                return
            self._stop_event.clear()
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
        self.start(explicit=False)  # arranque perezoso si hacía falta

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
        while not self._stop_event.is_set():
            r = None
            try:
                r = requests.get(self.cfg.stream_url, stream=True, timeout=(5, 15))
                with self._lock:
                    self._current_response = r
                buffer = b""
                for chunk in r.iter_content(chunk_size=4096):
                    if self._stop_event.is_set():
                        break
                    buffer += chunk
                    start = buffer.find(b"\xff\xd8")
                    end = buffer.find(b"\xff\xd9")
                    if start != -1 and end != -1 and end > start:
                        jpg = buffer[start:end + 2]
                        buffer = buffer[end + 2:]
                        nparr = np.frombuffer(jpg, np.uint8)
                        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                        if frame is not None:
                            if self._raw_queue.full():
                                try:
                                    self._raw_queue.get_nowait()
                                except queue.Empty:
                                    pass
                            self._raw_queue.put(frame)
                    if len(buffer) > 1_000_000:
                        buffer = b""
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

    def _update_fps(self):
        """Fps calculado como media sobre la última ventana de ~1s, para que no baile frame a frame."""
        now = time.time()
        self._fps_window.append(now)
        while self._fps_window and now - self._fps_window[0] > 1.0:
            self._fps_window.popleft()
        if len(self._fps_window) >= 2:
            self.pipeline_fps = (len(self._fps_window) - 1) / (self._fps_window[-1] - self._fps_window[0])

    def _process_loop(self):
        model = get_model(self.cfg.model_name, self.cfg.device)
        is_cuda = self.cfg.device.startswith("cuda")

        while not self._stop_event.is_set():
            keepalive_on = (
                self.cfg.keepalive_enabled
                if self.cfg.keepalive_enabled is not None
                else GLOBAL_CONFIG.keepalive_enabled
            )
            wait_timeout = GLOBAL_CONFIG.keepalive_interval_sec if (is_cuda and keepalive_on) else 1.0

            try:
                frame = self._raw_queue.get(timeout=wait_timeout)
            except queue.Empty:
                if is_cuda and keepalive_on:
                    try:
                        model.predict(np.zeros((640, 640, 3), dtype=np.uint8), imgsz=640, verbose=False)
                    except Exception as e:
                        print(f"[{self.cfg.camera_id}] keep-alive de GPU falló:", repr(e))
                if self._stop_event.is_set():
                    break
                continue

            self._update_fps()

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
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n")
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