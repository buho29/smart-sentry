"""El pipeline de video: leer del ESP32, inferir con YOLO y repartir el stream.

Aqui vive todo lo que ocurre por frame. Los endpoints estan en main.py y la
conexion con la placa en esphome_api.py.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import socket
import threading
import time
from collections import deque
from pathlib import Path
from typing import Iterable, Iterator, Optional
from urllib.parse import urlparse

import cv2
import numpy as np
import requests
import torch
from pydantic import BaseModel
from ultralytics import YOLO

from shutdown import is_shutting_down
from clips import STORE
from detections import Detection, DetectionConsumer
from esphome_api import EsphomeController
from log import print
from recorder import ClipRecorder, RecordingConfig
from servo_tracker import ServoConfig, ServoTracker


def _is_pascal(index: int = 0) -> bool:
    """¿Es una GPU de la era Pascal (GTX 10xx, compute capability 6.x)?

    Es la familia con los dos fallos que este proyecto rodea: el "CUDA
    misaligned address" de cuDNN y el de P-state, que devuelve inferencias
    corrupt cuando la tarjeta baja de relojes. En cualquier otra arquitectura
    los dos workarounds solo cuestan rendimiento, así que conviene detectarla en
    vez de imponérselos a todo el mundo: este repositorio es público.
    """
    try:
        if not torch.cuda.is_available():
            return False
        return torch.cuda.get_device_capability(index)[0] == 6
    except Exception:
        return False


def _any_pascal() -> bool:
    try:
        return any(_is_pascal(i) for i in range(torch.cuda.device_count()))
    except Exception:
        return False


# cuDNN desactivado SOLO donde hace falta. En la GTX 1080 evita el "CUDA
# misaligned address"; en el resto cuesta rendimiento y nada más (medido aquí:
# 9,71 ms con cuDNN contra 11,49 sin él, y en arquitecturas más nuevas se espera
# peor, porque cuDNN aporta más). DETECT_CUDNN=on|off lo fuerza en los dos
# sentidos, por si alguien tiene una tarjeta con síntomas parecidos.
_cudnn_env = os.environ.get("DETECT_CUDNN", "").strip().lower()
if _cudnn_env in ("on", "off"):
    torch.backends.cudnn.enabled = (_cudnn_env == "on")
    print(f"cuDNN {'activado' if _cudnn_env == 'on' else 'desactivado'} "
          f"por DETECT_CUDNN")
elif _any_pascal():
    torch.backends.cudnn.enabled = False
    print("GPU Pascal detectada: cuDNN desactivado "
          "(workaround del 'CUDA misaligned address')")


# Estilo de las cajas de detección
BOX_COLOR = (0, 220, 60)  # BGR: verde, para diferenciarlo del texto/overlay
BOX_THICKNESS = 3
FONT_SCALE = 0.6
FONT_THICKNESS = 2
CENTER_DOT_RADIUS = 4


# Para parsear el framing multipart del stream del ESP32 usando el
# Content-Length real que declara cada parte, en vez de buscar a mano
# \xff\xd8/\xff\xd9 en los bytes (ese método se desincronizaba si esos bytes
# aparecían por casualidad DENTRO de los datos comprimidos de un JPEG real,
# causando cuelgues de hasta varios minutos -- confirmado con pruebas
# aisladas moviendo la cámara físicamente).
_CONTENT_LENGTH_RE = re.compile(rb"Content-Length:\s*(\d+)", re.IGNORECASE)


def _iter_jpegs(chunks: Iterable[bytes], max_buffer: int = 2_000_000) -> Iterator[bytes]:
    """Trocea el multipart del ESP32 y va soltando los JPEG, uno a uno.

    Se usa el `Content-Length` que la propia placa declara en cada parte
    (`esp32_camera_web_server` manda "Content-Type: image/jpeg\\r\\n
    Content-Length: N\\r\\n\\r\\n" antes de cada frame) en vez de buscar a mano
    los marcadores \\xff\\xd8 / \\xff\\xd9. Ese método se desincronizaba cuando
    esos bytes aparecían por casualidad DENTRO de los datos comprimidos de un
    JPEG real, y provocaba cuelgues de hasta varios minutos (confirmado
    moviendo la cámara físicamente). El Content-Length es el framing de
    verdad, el mismo que usa un navegador para pintar este stream sin
    despeinarse.

    Es una función pura sobre un iterable de bytes —ni sockets ni hilos— para
    poder probar sin red los casos que motivaron escribirla así.
    """
    buffer = b""
    # None = esperando la cabecera de la siguiente parte; int = bytes de JPEG
    # que todavía remaining por leer de la parte actual.
    remaining: Optional[int] = None

    for chunk in chunks:
        buffer += chunk
        advanced = True
        while advanced:
            advanced = False
            if remaining is None:
                idx = buffer.find(b"\r\n\r\n")
                if idx != -1:
                    m = _CONTENT_LENGTH_RE.search(buffer[:idx])
                    buffer = buffer[idx + 4:]
                    if m:
                        remaining = int(m.group(1))
                    # Si el bloque no traía Content-Length (p.ej. era solo la
                    # línea del boundary suelta) no se fija nada y se reintenta
                    # con el siguiente \r\n\r\n que aparezca.
                    advanced = True
            elif len(buffer) >= remaining:
                yield buffer[:remaining]
                buffer = buffer[remaining:]
                remaining = None
                advanced = True

        # Salvavidas: si el stream viene corrupto y nunca cuadra una parte, el
        # buffer crecería sin fin. Se tira y se resincroniza con la siguiente
        # cabecera que llegue.
        if len(buffer) > max_buffer:
            buffer = b""
            remaining = None


def _reason(e: BaseException, max_len: int = 90) -> str:
    """Resumen corto de una excepción para el log.

    Los errores de urllib3 anidan MaxRetryError/HTTPConnectionPool/... y su
    repr() ocupa unos 400 caracteres. Repetido una vez por segundo y por cámara
    mientras se reintenta, ahoga el log. El repr completo se sigue guardando en
    self.last_error, que se expone en /status.
    """
    txt = " ".join(str(e).split())
    if len(txt) > max_len:
        txt = txt[:max_len - 1] + "…"
    return f"{type(e).__name__}: {txt}" if txt else type(e).__name__


def _quiet_close(resp: "requests.Response"):
    try:
        resp.close()
    except Exception:
        pass


def _force_close_response(resp: "requests.Response", tag: str = ""):
    """Cierra la conexión HTTP con el ESP32 sin bloquear a quien llama.

    resp.close() por sí solo puede tardar SEGUNDOS (medidos 12s en pruebas)
    cuando el hilo lector está dentro de un recv() sobre ese mismo socket: en
    Windows, cerrar el objeto fichero no interrumpe la lectura en curso. Como
    stop() se llama desde el event loop de EsphomeController (cuando la placa
    publica `awake=off` justo antes de dormirse) y desde endpoints async,
    quedarse bloqueado ahí congelaba la API entera durante esos segundos.

    Lo que sí desbloquea al lector al instante (~0.1 ms medidos) es un
    shutdown() del socket subyacente, así que va primero; el close() ordenado
    de después ya es inmediato. El lector se despierta con un
    ChunkedEncodingError, que su propio except captura y trata como parada.

    Las tripas de urllib3 cambian entre versiones, así que probamos varias
    rutas hasta el socket. Si no lo encontramos, delegamos el close() a un
    hilo aparte: en el peor caso el que se bloquea es ese hilo desechable y
    no el que pidió la parada.
    """
    sock = None
    for get_sock in (
        lambda: resp.raw._fp.fp.raw._sock,             # urllib3 2.x / 1.26
        lambda: resp.raw._original_response.fp.raw._sock,
        lambda: resp.raw._connection.sock,
    ):
        try:
            sock = get_sock()
        except Exception:
            sock = None
        if sock is not None:
            break

    if sock is None:
        threading.Thread(target=_quiet_close, args=(resp,), daemon=True,
                         name=f"close-{tag}").start()
        return

    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # el otro extremo ya lo había cerrado (la placa se durmió)
    _quiet_close(resp)


# ---------------------------------------------------------------------------
# Config global, modificable en runtime vía API
# ---------------------------------------------------------------------------

class GlobalConfig:
    def __init__(self):
        # "auto" = activo solo en GPU Pascal, que es donde existe el bug de
        # P-state. En una tarjeta sana el keep-alive llena los huecos entre
        # frames con inferencias inútiles: medido aquí, la ocupación de GPU pasa
        # del 18 % al 64 % sin ganar nada. True/False lo fuerzan.
        self.keepalive_enabled: "bool | str" = "auto"
        # Cada cuánto se lanza una inferencia dummy mientras se espera el frame
        # siguiente. Estuvo en 0.05 y era DEMASIADO LARGO: con la cámara a 16,7
        # fps (60 ms por frame) y una inferencia de ~20 ms, el frame siguiente
        # llegaba siempre antes del timeout, así que `queue.Empty` no saltaba
        # casi nunca y el keep-alive no se ejecutaba (19 dummies en 11.280
        # frames). La GPU se pasaba dormida dos tercios de cada ciclo y caía a
        # P5 / 759 MHz de los 1961 de la GTX 1080.
        #
        # El efecto no era solo lentitud: a esos relojes la inferencia salía
        # CORRUPTA, con confianzas fuera de [0,1] (se midió un 1.812) y frames
        # enteros sin detectar a una persona que estaba delante. Es el bug de
        # P-state que reproduce test/gpu_yolo_stress_test.py.
        #
        # Medido al bajarlo a 0.005, en caliente y con la misma escena:
        #   759 MHz (P5) -> 1265 MHz (P2), 22,2 ms -> 9,8-16,6 ms,
        #   y las confianzas volvieron a 0,71-0,91.
        #
        # Ya no es el único freno: quien decide si se calienta es el reloj real
        # de la GPU (ver keepalive_clock_ratio y gpu_underclocked). Esto solo
        # marca cada cuánto se vuelve a mirar mientras se espera un frame, así
        # que 0.01 va sobrado y ahorra vueltas en vacío.
        self.keepalive_interval_sec: float = 0.01
        # Segundos sin recibir un frame real tras los cuales se deja de
        # calentar la GPU. El keep-alive solo tiene sentido ENTRE frames de un
        # stream vivo (huecos de decenas de ms); pasado este plazo la cámara no
        # está dando nada y seguir lanzando inferencias dummy a 20 Hz es quemar
        # la GPU para nada.
        self.keepalive_idle_limit_sec: float = 3.0
        # Fracción del reloj MÁXIMO OBSERVADO por debajo de la cual se
        # considera que la GPU se ha dormido. La lógica es "calienta salvo que
        # la tarjeta esté prácticamente en su tope sostenido", no "calienta solo
        # si ha caído un 30 %": por eso está tan alto.
        #
        # Estuvo en 0.7 y fue un error caro. Con el máximo observado en 1657 MHz
        # el umbral quedaba en 1160, y esta GTX 1080 se sostiene en 1177 — justo
        # por encima, así que no se calentaba NUNCA. Medido: 58 detecciones
        # corrupt en 7486 frames, contra 1 en 5986 cuando sí calentaba.
        #
        # Puntos de operación medidos en esta tarjeta:
        #   759 MHz  -> corrupción masiva, deja de detectar
        #   1177 MHz -> 58 corrupt en 7486 frames
        #   1290 MHz -> 1 corrupta en 5986 frames
        #
        # A 0.95 el umbral queda en ~1574 y se calienta siempre, que es lo
        # correcto aquí. En una GPU sana que sostiene sus relojes, el actual es
        # ~igual al observado y no se calienta nada: ahí está la portabilidad.
        self.keepalive_clock_ratio: float = 0.95
        self.reconnect_delay_sec: float = 1.0

    def resolve_keepalive(self) -> bool:
        """`keepalive_enabled` ya resuelto: traduce "auto" a True/False."""
        if isinstance(self.keepalive_enabled, bool):
            return self.keepalive_enabled
        return _any_pascal()

    def as_dict(self):
        return {
            "keepalive_enabled": self.keepalive_enabled,
            "keepalive_resolved": self.resolve_keepalive(),
            "keepalive_reason": (
                "forzado por configuración" if isinstance(self.keepalive_enabled, bool)
                else ("GPU Pascal detectada" if _any_pascal()
                      else "no hay GPU Pascal: el workaround no hace falta")),
            "keepalive_interval_sec": self.keepalive_interval_sec,
            "keepalive_clock_ratio": self.keepalive_clock_ratio,
            "keepalive_idle_limit_sec": self.keepalive_idle_limit_sec,
            "reconnect_delay_sec": self.reconnect_delay_sec,
        }

    # Solo estos se guardan y se releen. `keepalive_resolved` y
    # `keepalive_reason` son derivados: persistirlos significaría que una
    # máquina que cambia de GPU arrastraría el veredicto de la anterior.
    _PERSISTED = ("keepalive_enabled", "keepalive_interval_sec",
                  "keepalive_clock_ratio", "keepalive_idle_limit_sec",
                  "reconnect_delay_sec")

    def save(self) -> None:
        try:
            GLOBAL_CONFIG_FILE.write_text(
                json.dumps({k: getattr(self, k) for k in self._PERSISTED},
                           indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            print(f"No se pudo guardar {GLOBAL_CONFIG_FILE}: {e!r}")

    def load(self) -> None:
        """Relee los ajustes del disco. Un fichero roto no impide arrancar.

        Existe porque antes no: `GLOBAL_CONFIG` vivía solo en memoria y cada
        reinicio se llevaba por delante lo que se hubiera ajustado por API.
        Cuesta explicar que un `POST /config/keepalive` "no haya servido de
        nada" cuando lo que pasó es que se perdió al reiniciar.

        Aviso: lo guardado MANDA sobre los valores por defecto del código, así
        que un defecto que cambie en una versión nueva no llega a una máquina
        que ya tenga su fichero. Es el mismo trato que cameras_config.json.
        """
        if not GLOBAL_CONFIG_FILE.exists():
            return
        try:
            data = json.loads(GLOBAL_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"No se pudo leer {GLOBAL_CONFIG_FILE}: {e!r}; "
                  f"se usan los valores por defecto")
            return
        for k in self._PERSISTED:
            if k in data:
                setattr(self, k, data[k])
        print(f"Ajustes globales cargados desde {GLOBAL_CONFIG_FILE.name}")


GLOBAL_CONFIG_FILE = Path(__file__).with_name("global_config.json")
GLOBAL_CONFIG = GlobalConfig()


_nvml_handles: dict = {}
_nvml_broken = False


def device_index(device: str) -> int:
    """Índice de GPU de un device de PyTorch: "cuda:1" -> 1, "cuda" -> 0."""
    if ":" in device:
        try:
            return int(device.rsplit(":", 1)[1])
        except ValueError:
            pass
    return 0


def _gpu_state(index: int = 0) -> dict:
    """P-state y relojes de la GPU, para /status.

    Barato (unas lecturas de NVML) y solo se llama al pedir el estado. Si
    pynvml no está o falla, se devuelven None y nadie se entera: esto es
    diagnóstico, no puede romper el servicio.

    El handle se cachea POR ÍNDICE: el device de cada cámara es configurable
    ("cuda:1"), y antes se leía siempre la GPU 0, así que en una máquina con
    varias tarjetas el keep-alive decidía mirando los relojes de otra.
    """
    global _nvml_broken
    if _nvml_broken:
        return {"pstate": None, "sm_clock_mhz": None, "sm_clock_max_mhz": None}
    try:
        import pynvml
        if index not in _nvml_handles:
            pynvml.nvmlInit()
            _nvml_handles[index] = pynvml.nvmlDeviceGetHandleByIndex(index)
        _nvml_handle = _nvml_handles[index]
        clock = pynvml.nvmlDeviceGetClockInfo(_nvml_handle, pynvml.NVML_CLOCK_SM)
        # El máximo observado se actualiza AQUÍ y no solo en gpu_underclocked:
        # si no, cualquier lector (gpu_clock_pct, GET /gpu) veía un máximo a
        # cero mientras el bucle de vídeo no hubiera arrancado todavía.
        global _sm_clock_max_seen
        _sm_clock_max_seen = max(_sm_clock_max_seen, clock)
        return {
            "pstate": f"P{pynvml.nvmlDeviceGetPerformanceState(_nvml_handle)}",
            "sm_clock_mhz": pynvml.nvmlDeviceGetClockInfo(_nvml_handle, pynvml.NVML_CLOCK_SM),
            "sm_clock_max_mhz": pynvml.nvmlDeviceGetMaxClockInfo(_nvml_handle, pynvml.NVML_CLOCK_SM),
        }
    except Exception:
        _nvml_broken = True
        return {"pstate": None, "sm_clock_mhz": None, "sm_clock_max_mhz": None}


def _is_corrupt(d: Detection) -> bool:
    """¿Esta detección es físicamente imposible?

    La confianza sale de una sigmoide, así que un valor fuera de [0,1] no es
    "una detección rara": es la GPU devolviendo basura. En producción se vieron
    un 1.812 y un 3.58e15 (memoria sin inicializar), los dos con la tarjeta
    corriendo a los relojes del escritorio.

    Función pura y aparte para poder probarla sin GPU, y para que el filtrado y
    el recuento usen exactamente el mismo criterio.
    """
    return (not (0.0 <= d.conf <= 1.0)
            or not all(map(math.isfinite, (d.x1, d.y1, d.x2, d.y2))))


_gpu_slow_cache = (0.0, False)
# Reloj más alto visto desde que arrancó el proceso. Se usa como referencia en
# vez del máximo teórico de NVML: ese es el de P0, y bajo carga de cómputo una
# GTX 1080 se queda en P2 con un techo bastante más bajo (medido: ~1290 MHz
# frente a los 1961 que declara).
_sm_clock_max_seen = 0


def gpu_clock_pct(index: int = 0) -> Optional[int]:
    """Reloj actual como % del máximo observado. None si no hay lecturas.

    Un solo número que resume el estado de la GPU para `/status`: 100 es "a
    pleno rendimiento", y por debajo del `keepalive_clock_ratio` (95) es donde
    esta GTX 1080 empieza a devolver inferencias corruptas.
    """
    current = _gpu_state(index).get("sm_clock_mhz")
    if not current or not _sm_clock_max_seen:
        return None
    return round(100 * current / _sm_clock_max_seen)


def gpu_underclocked(ratio: float = 0.95, max_age_sec: float = 0.5) -> bool:
    """¿Está la GPU por debajo de su tope sostenido, o sea a punto de dar basura?

    Es la señal que gobierna el keep-alive. Antes se aproximaba por "segundos
    sin frame", que no es lo mismo y fallaba justo en el caso que importaba:
    con un modelo rápido llegaban frames sin parar y aun así la GPU se dormía
    entre uno y otro.

    Se cachea porque esto se consulta en el bucle de vídeo y NVML no es gratis.
    Medio segundo es de sobra: las transiciones de P-state van en esa escala.

    **Sin lecturas de reloj devuelve False.** Antes devolvía True ("mejor
    proteger"), y era el peor defecto posible fuera de NVIDIA: en una AMD con
    ROCm PyTorch también llama "cuda" al device, así que el keep-alive se
    quedaba disparando de forma permanente en una tarjeta que no lo necesita.
    Quien sí lo necesite sigue cubierto por el criterio de tiempo sin frames,
    que es independiente de esto.
    """
    global _gpu_slow_cache, _sm_clock_max_seen
    now = time.monotonic()
    seen, value = _gpu_slow_cache
    if now - seen < max_age_sec:
        return value
    current = _gpu_state().get("sm_clock_mhz")
    if not current:
        value = False
    else:
        # Referencia: el reloj más alto que se ha visto de verdad.
        _sm_clock_max_seen = max(_sm_clock_max_seen, current)
        value = current < ratio * _sm_clock_max_seen
    _gpu_slow_cache = (now, value)
    return value


def _should_keepalive(is_cuda: bool, enabled: bool, seconds_since_frame: float,
                      idle_limit: float, has_inference: bool,
                      gpu_slow: bool = True) -> bool:
    """¿Hay que lanzar una inferencia dummy para que la GPU no baje de P-state?

    Separado en una función pura para poder probar la lógica sin GPU. Ver el
    comentario largo sobre el bug de P-state de la GTX 1080 encima de
    _process_loop.

    `has_inference` es lo que evita calentar para nada: el workaround protege
    a las inferencias REALES de salir corrupt, así que si no va a haber
    ninguna (always_infer a false, nadie mirando y ningún consumidor), tener la
    GPU ocupada no protege nada.

    `gpu_slow` es lo que evita calentar de más. Con un modelo pesado la GPU se
    mantiene despierta ella sola (el medium ocupa la mitad del tiempo entre
    frames), así que lanzar dummies ahí solo sirve para robarle tiempo al
    pipeline. Por defecto True para que quien no pueda medir los relojes se
    comporte como antes.
    """
    return (is_cuda and enabled and has_inference
            and seconds_since_frame < idle_limit and gpu_slow)


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


# Dónde corre el endpoint de prueba puntual (/detect-file). El modelo lo elige
# quien llama, y se carga de forma perezosa vía get_model() la primera vez, para
# no penalizar el arranque del servicio si solo se usan las cámaras.
DEFAULT_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"


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
    # Overrides del keep-alive para ESTA cámara. None = hereda de GLOBAL_CONFIG.
    keepalive_enabled: Optional[bool] = None
    # El intervalo es por cámara porque el coste del dummy depende del modelo:
    # medido, 9,3 ms con yolo26n y 37,2 ms con yolo26m. Un único valor global no
    # puede ser bueno para las dos a la vez.
    keepalive_interval_sec: Optional[float] = None
    default_infer: bool = True  # usado por /stream y /snapshot cuando no se pasa ?infer=

    # Correr YOLO aunque no haya nadie mirando el stream ni ningún consumidor
    # pidiéndolo: la cámara sigue detectando con el navegador cerrado.
    #
    # OJO, no confundirlo con default_infer, que está justo encima: aquel
    # decide QUÉ devuelven /stream y /snapshot cuando no se pasa ?infer=
    # (anotado o crudo); este decide SI la detección llega a correr.
    #
    # A false, una variante que solo sirva vídeo no gasta GPU.
    always_infer: bool = True

    # Parada manual (POST /stop): la cámara se queda parada hasta el siguiente
    # POST /start, aunque la placa republique awake=on al reconectar o al
    # despertar, o llegue un cliente nuevo. Va en la config (y no como estado
    # de la sesión) para que sobreviva a un reinicio del servicio: si no, al
    # reiniciar el primer awake=on la arrancaba otra vez.
    manual_stop: bool = False

    # Si se rellena, la sesión abre también una conexión a la API nativa de
    # ESPHome (puerto 6053) de la misma placa (host sacado de stream_url), y
    # usa el binary_sensor indicado (object_id, ON/OFF) para arrancar/parar la
    # lectura en vez de depender solo de que haya clientes HTTP mirando el
    # stream.
    noise_psk: Optional[str] = None  # api.encryption.key del YAML de la placa
    esphome_state_object_id: Optional[str] = "awake"

    # Hardware opcional de esta placa. None = esta variante no lo lleva, y
    # entonces la sesión ni siquiera crea el consumidor correspondiente. Va como
    # sección anidada y no como campos sueltos para que las variantes futuras
    # (p.ej. la pistola) añadan su propia sección sin ensanchar esto.
    servo: Optional[ServoConfig] = None

    # Grabación de clips a disco. None = esta cámara no graba, y entonces no se
    # crea el consumidor ni se codifica un JPEG de más: el coste es exactamente
    # cero, igual que antes de que esto existiera.
    recording: Optional[RecordingConfig] = None


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

        # Salud de la inferencia. Existen porque hubo un fallo en el que el
        # servicio dibujaba cero cajas sobre frames donde el mismo modelo,
        # offline, encontraba a la persona: sin estos números no había forma de
        # saber si las perdía el detector o el tracker.
        self._raw_dets_last = 0      # cajas del detector, antes del tracker
        self._dets_after_tracker = 0     # las que sobreviven al tracker
        self._frames_filtered = 0       # veces que el tracker se las comió TODAS
        self._frames_inferred = 0
        self._frames_without_box = 0
        self._consecutive_without_box = 0
        self._last_conf_range: Optional[list] = None
        self._last_detection: Optional[float] = None
        # Detecciones imposibles (confianza fuera de [0,1] o coordenadas no
        # finitas). Son la señal inequívoca del bug de P-state.
        self._corrupt_dets = 0
        self._last_corruption: Optional[dict] = None

        # Estado del keep-alive de GPU. Vivían como variables locales de
        # _process_loop, así que no había manera de saber desde fuera si estaba
        # calentando o en pausa.
        self._ka_idle = False
        self._ka_warming = False
        self._ka_dummies = 0
        self._ka_dummy_ms = 0.0     # coste medido de una dummy
        self._ka_skipped = 0       # dummies que no cabían antes del frame siguiente

        # Último frame decodificado, para que el selftest pueda inferir sobre
        # EXACTAMENTE lo mismo que vio el pipeline. Es una referencia, no una
        # copia: no cuesta nada.
        self._latest_frame: Optional[np.ndarray] = None

        # Conexión opcional a la API nativa de ESPHome de la misma placa,
        # para arrancar/parar la sesión según el estado real del hardware
        # (binary_sensor `awake`: ON = despierto / OFF = dormido) en vez de
        # solo por clientes HTTP.
        self.esphome: Optional[EsphomeController] = None
        self._last_esphome_state: Optional[bool] = None
        if cfg.noise_psk:
            host = urlparse(cfg.stream_url).hostname
            if host is None:
                print(f"[{cfg.camera_id}] noise_psk configurado pero no pude sacar el host de stream_url={cfg.stream_url!r}")
            else:
                self.esphome = EsphomeController(
                    address=host,
                    noise_psk=cfg.noise_psk,
                    watch_entity_object_id=cfg.esphome_state_object_id,
                    on_state_value=self._on_esphome_state,
                )

        # Consumidores de detecciones (ver detections.py): lo que cada variante
        # de placa hace con lo que ve la cámara. Vacío para una cámara normal,
        # que así se comporta exactamente igual que antes de que esto existiera.
        self._consumers: list[DetectionConsumer] = []
        self.servo_tracker: Optional[ServoTracker] = None
        if cfg.servo is not None and not self.configure_servo(cfg.servo):
            # Los servos van por la API nativa de la placa, que solo se levanta
            # con noise_psk. Sin ella no hay por dónde enviar nada, y arrancar
            # el seguimiento igualmente solo serviría para forzar inferencia
            # continua sin efecto ninguno.
            print(f"[{cfg.camera_id}] servo configurado pero sin noise_psk: "
                  f"no hay API de ESPHome por la que mover nada, lo ignoro")

        self.clip_recorder: Optional[ClipRecorder] = None
        if cfg.recording is not None:
            self.configure_recording(cfg.recording)

    def configure_recording(self, cfg: RecordingConfig) -> ClipRecorder:
        """Monta o reconfigura la grabación de clips. Único sitio que la enchufa.

        A diferencia de `configure_servo` no puede fallar: grabar no necesita la
        API de ESPHome ni ningún hardware, solo disco. Por eso lo usa tanto el
        alta de la sesión como /config/recording, que es el que le pone
        grabación a una cámara que no la tenía.

        Casi todo se aplica en caliente porque la config se relee en cada frame.
        Las excepciones son `source`, `encoder` y `fourcc`: esos los lee el hilo
        escritor al abrir el fichero, así que un cambio a media grabación no
        surte efecto hasta el clip siguiente.
        """
        self.cfg.recording = cfg
        if self.clip_recorder is None:
            self.clip_recorder = ClipRecorder(
                cfg, self.cfg.camera_id, STORE,
                fps_getter=lambda: self.pipeline_fps,
            )
            self._consumers.append(self.clip_recorder)
        else:
            self.clip_recorder.cfg = cfg
        return self.clip_recorder

    def configure_servo(self, cfg: ServoConfig) -> bool:
        """Monta o reconfigura la torreta. Devuelve si se pudo.

        Único sitio que sabe enchufar un ServoTracker: lo usan tanto el alta de
        la sesión como el endpoint /config/servo, que es el que le pone servos a
        una cámara que no los tenía. `False` = esta cámara no tiene API de
        ESPHome, así que no hay por dónde mover nada.
        """
        if self.esphome is None:
            return False
        self.cfg.servo = cfg
        if self.servo_tracker is None:
            # ServoTracker no toca el hardware al construirse (solo se apunta la
            # posición de reposo), así que se puede montar con la sesión ya
            # corriendo.
            self.servo_tracker = ServoTracker(cfg, self.esphome, self.cfg.camera_id)
            self._consumers.append(self.servo_tracker)
        else:
            # Lee self.cfg en vivo en cada frame, así que basta con sustituirla;
            # lo único que conserva del anterior es la posición actual, que es
            # estado y no configuración.
            self.servo_tracker.cfg = cfg
        return True

    def _on_esphome_state(self, value: Optional[bool]):
        # `awake` es un binary_sensor en el YAML de la placa, así que el valor
        # llega como bool (True = despierto, False = dormido). None = todavía
        # sin publicar.
        if value is None:
            return
        value = bool(value)
        # Ignoramos repeticiones (la placa republica el estado al reconectar),
        # PERO solo si la sesión ya está como debería estar. Si el estado dice
        # "on" y la lectura está parada -- p.ej. el _read_loop se rindió porque
        # la placa se durmió sin llegar a publicar el "off" -- volvemos a
        # arrancar aunque el valor no haya cambiado. Sin esto, esa combinación
        # dejaba la cámara muerta hasta el siguiente ciclo de sueño completo.
        if value == self._last_esphome_state and value == self.is_running:
            return
        self._last_esphome_state = value
        if value and self.cfg.manual_stop:
            print(f"[{self.cfg.camera_id}] {self.cfg.esphome_state_object_id}=on, pero la cámara "
                  f"está parada a mano -> no arranco (POST /start para reactivarla)")
            return
        if value:
            print(f"[{self.cfg.camera_id}] {self.cfg.esphome_state_object_id}=on -> arrancando cámara")
            self.start(explicit=True)
        else:
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
            # Estado del tracker a cero en cada arranque. El modelo se cachea
            # entre sesiones, así que sin esto una generación nueva heredaba el
            # ByteTrack de la anterior —con sus tracks de hace media hora y de
            # otra escena— y no había forma de limpiarlo salvo reiniciar el
            # proceso.
            self.reset_tracker()
            # Con nombre, para poder identificarlos en los logs de cierre y en
            # un volcado de hilos si alguna generación se queda colgada.
            self._reader_thread = threading.Thread(
                target=self._read_loop, daemon=True, name=f"read-{self.cfg.camera_id}"
            )
            self._processing_thread = threading.Thread(
                target=self._process_loop, daemon=True, name=f"yolo-{self.cfg.camera_id}"
            )
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
        # Cerrar la conexión HTTP abierta fuera del lock, para no bloquearlo, y
        # a la fuerza (shutdown del socket): ver _force_close_response, un
        # resp.close() a secas podía tardar segundos y este stop() lo llama el
        # loop de ESPHome cuando la placa avisa de que se va a dormir.
        if resp is not None:
            _force_close_response(resp, tag=self.cfg.camera_id)
        with self._cond:
            self._cond.notify_all()  # despierta YA a los generadores esperando, sin esperar al polling de 5s
        print(f"[{self.cfg.camera_id}] lectura detenida")

    def manual_start(self):
        """Arranque manual (POST /start): quita la parada manual y arranca.
        Quien llama debe persistir la config (save_cameras_to_disk)."""
        with self._lock:
            self.cfg.manual_stop = False
        self.start(explicit=True)

    def manual_stop(self):
        """Parada manual (POST /stop): para y marca cfg.manual_stop para que
        ni el awake=on de la placa ni un cliente nuevo la rearranquen, ni
        ahora ni tras reiniciar el servicio. Quien llama debe persistir la
        config (save_cameras_to_disk)."""
        with self._lock:
            self.cfg.manual_stop = True
        self.stop(explicit=True)

    def restart(self) -> bool:
        """Relanza los hilos, sin tocar el controller de ESPHome ni los consumidores.

        Hace falta para los ajustes que `_process_loop` resuelve **una sola vez**
        al arrancar: el modelo y el device se fijan en su primera línea, así que
        cambiarlos en `cfg` no tiene ningún efecto hasta que hay hilos nuevos.
        El resto de la config se relee en cada frame y no necesita esto.

        Devuelve si llegó a relanzar algo: con la sesión parada no hay nada que
        hacer, la config nueva la cogerá el próximo `start()`.

        Es bloqueante (espera a que muera la generación vieja), así que va
        envuelta en `asyncio.to_thread` desde los endpoints.
        """
        if not self.is_running:
            return False
        explicit = self.explicit_start
        self.stop(explicit=True)
        # Hay que esperar de verdad a que mueran: start() comprueba is_running,
        # y si algún hilo de la generación vieja sigue vivo se cree que ya está
        # todo en marcha y no arranca nada.
        for t in (self._reader_thread, self._processing_thread):
            if t is not None:
                t.join(timeout=3)
        self.start(explicit=explicit)
        return True

    def shutdown(self):
        """Para la sesión y espera a que los hilos terminen. Para usar al apagar
        el servicio o al borrar la cámara con DELETE.

        El presupuesto total está acotado a ~4s a propósito: lifespan solo da
        5s por sesión y uvicorn arranca con --timeout-graceful-shutdown 5. Los
        tres hilos son daemon, así que si alguno se pasa del plazo no impide
        que el proceso muera; solo lo dejamos dicho en el log.
        """
        self.stop(explicit=True)
        # Los consumidores, ANTES del controller: un ServoTracker aprovecha su
        # shutdown() para volver a reposo, y para eso la conexión con la placa
        # todavía tiene que estar viva.
        for c in self._consumers:
            try:
                c.shutdown()
            except Exception as e:
                print(f"[{self.cfg.camera_id}] {type(c).__name__}.shutdown falló:", repr(e))
        # Después el controller: así deja de reintentar contra la placa y
        # cierra su socket de la API nativa (6053) mientras los hilos de vídeo
        # terminan de salir, en vez de en serie después de ellos.
        if self.esphome is not None:
            self.esphome.shutdown(timeout=1.5)
        deadline = time.monotonic() + 2.5
        for t in (self._reader_thread, self._processing_thread):
            if t is None:
                continue
            t.join(timeout=max(0.0, deadline - time.monotonic()))
            if t.is_alive():
                print(f"[{self.cfg.camera_id}] el hilo {t.name} no terminó a tiempo "
                      f"(es daemon, no bloquea el cierre del proceso)")
        with self._cond:
            self._cond.notify_all()  # despierta a los generadores que sigan esperando

    def _maybe_autostop(self):
        with self._lock:
            if not self.explicit_start and self.client_count == 0:
                # Si ya estaba marcado no hay nada que anunciar: es el caso del
                # apagado, donde stop() ya logueó "lectura detenida" y luego
                # cada generador pasa por aquí al soltar su cliente.
                already_stopped = self._stop_event.is_set()
                self._stop_event.set()
                if not already_stopped:
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
        if self.esphome is None and not self.cfg.manual_stop:
            # Arranque perezoso solo para cámaras SIN control ESPHome (y no
            # paradas a mano: un POST /stop tiene que seguir valiendo aunque
            # el navegador vuelva a abrir el stream). Con
            # ESPHome, arrancar aquí sin saber si la placa está despierta
            # provocaría un _read_loop reintentando en bucle contra una
            # cámara dormida -> stream congelado/en blanco para el cliente.
            # El propio awake="on" ya llama a start() cuando toca;
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
                # Connect timeout de 3s (no 5): en la LAN abrir la conexión son
                # milisegundos, y ese número es el tiempo máximo que este hilo
                # puede tardar en enterarse de un stop() si le pilla justo aquí,
                # con la placa ya dormida y sin nadie que acepte la conexión.
                r = requests.get(self.cfg.stream_url, stream=True, timeout=(3, 15))
                with self._lock:
                    self._current_response = r
                # Si nos pararon MIENTRAS se abría esta conexión, stop() leyó
                # _current_response cuando todavía era None y no la cerró; sin
                # este chequeo nos meteríamos en iter_content y, con la placa
                # ya dormida y sin enviar nada, el hilo se quedaría ahí hasta
                # agotar los 15s de read timeout. El finally cierra la
                # respuesta al salir.
                if stop_event.is_set():
                    break
                for jpg in _iter_jpegs(r.iter_content(chunk_size=4096)):
                    if stop_event.is_set():
                        break
                    frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                    if frame is None:
                        continue
                    # Cola de 1: si el proceso va por detrás, se tira el frame
                    # viejo y se deja el nuevo. Más vale saltarse frames que
                    # inferir sobre imagen atrasada.
                    if raw_queue.full():
                        try:
                            raw_queue.get_nowait()
                        except queue.Empty:
                            pass
                    raw_queue.put(frame)
            except Exception as e:
                # Except amplio a propósito: al parar la sesión cerramos la
                # respuesta desde OTRO hilo (stop() -> _current_response.close())
                # y eso hace saltar a iter_content lo que toque según por dónde
                # le pille a urllib3: a veces un RequestException, pero también
                # ValueError("I/O operation on closed file") o AttributeError
                # sobre un socket ya puesto a None. Cazando solo
                # RequestException esos casos mataban el hilo con un traceback
                # por consola en vez de salir por el camino limpio de abajo.
                if stop_event.is_set():
                    # Parada pedida: la excepción es la consecuencia, no la
                    # causa. Salimos sin log de error ni espera de reconexión.
                    break
                self.last_error = repr(e)
                # Si nadie quiere ya la cámara (p.ej. se durmió por el PIR y no hay
                # clientes ni arranque explícito), dejamos de insistir en reconectar.
                if not self.explicit_start and self.client_count == 0:
                    break
                # Si esta sesión la gobierna ESPHome y también hemos perdido la
                # API nativa, la placa está dormida (o fuera de cobertura): no
                # tiene sentido machacar el stream HTTP cada segundo contra una
                # IP muerta. Paramos; el siguiente `awake=on` nos rearranca
                # (ver la nota sobre repeticiones en _on_esphome_state).
                if self.esphome is not None and not self.esphome.is_connected:
                    print(f"[{self.cfg.camera_id}] stream caído ({_reason(e)}) y API de ESPHome "
                          f"desconectada -> la placa parece dormida, dejo de reintentar")
                    break
                print(f"[{self.cfg.camera_id}] stream interrumpido ({_reason(e)}), "
                      f"reconectando en {GLOBAL_CONFIG.reconnect_delay_sec}s...")
                time.sleep(GLOBAL_CONFIG.reconnect_delay_sec)
                continue
            finally:
                with self._lock:
                    if self._current_response is r:
                        self._current_response = None
                if r is not None:
                    r.close()

        # Hemos salido del bucle. Si fue por nuestra cuenta (nos rendimos con
        # la placa dormida, o ya no queda nadie que quiera la cámara) y no
        # porque alguien nos parara, hay que parar la SESIÓN entera. Dejando
        # morir solo a este hilo, el de proceso se queda vivo con la cola vacía
        # haciendo keep-alive de GPU cada 50 ms indefinidamente: medido, un 54%
        # de una GTX 1080 con las dos cámaras desenchufadas y cero frames
        # entrando.
        #
        # La comparación por identidad no es opcional: si mientras este hilo
        # agonizaba ya arrancó otra generación, self._stop_event es un objeto
        # distinto del nuestro y llamar a stop() mataría a la generación NUEVA.
        if self._stop_event is stop_event and not stop_event.is_set():
            self.stop(explicit=True)

    # -- hilo de proceso: YOLO + keep-alive de GPU -----------------------
    #
    # Diagnóstico confirmado (31/08/2026): la GTX 1080 produce inferencias
    # corrupt (confianzas fuera de 0-1) cuando el driver baja el estado de
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

    def _notify_consumers(self, method: str, *args):
        """Llama a un método de cada consumidor sin dejar que uno tumbe el resto.

        Esto corre en el hilo de proceso, en el camino crítico del vídeo: un
        fallo moviendo servos no puede llevarse por delante el stream, así que
        cada uno va en su propio try.
        """
        for c in self._consumers:
            # Los métodos del Protocol que no todos implementan (on_jpeg, que
            # solo le interesa al grabador) se saltan en silencio. Sin esto, el
            # ServoTracker soltaría un AttributeError logueado quince veces por
            # segundo.
            fn = getattr(c, method, None)
            if fn is None:
                continue
            try:
                fn(*args)
            except Exception as e:
                self.last_error = repr(e)
                print(f"[{self.cfg.camera_id}] {type(c).__name__}.{method} falló:", repr(e))

    def _work_needed(self) -> tuple[bool, bool, bool]:
        """Qué trabajo pide este frame: (dibujar, inferir, publicar crudo).

        Se recalcula en cada vuelta del bucle, y ahí está la gracia: cambiar
        `default_infer` o `always_infer` por API afecta a los streams ya
        abiertos sin que nadie tenga que reconectar.
        """
        # Alguien está mirando el vídeo anotado. Se separa de la inferencia
        # porque un consumidor (los servos) necesita las detecciones pero no el
        # dibujo: sin clientes, pintar cajas y recodificar el JPEG sería trabajo
        # tirado en cada frame.
        # Un consumidor puede necesitar los JPEG aunque no haya ni un cliente
        # HTTP: el grabador tiene que llenar su pre-roll continuamente, o el
        # clip empezaría justo cuando el bicho ya está en el centro del
        # encuadre.
        rec_raw, rec_draw = self._consumers_want_frames()
        want_draw = (self._infer_clients > 0
                     or (self._follow_clients > 0 and self.cfg.default_infer)
                     or rec_draw)
        # always_infer manda: la cámara sigue detectando con el navegador
        # cerrado. Los consumidores también piden inferencia, o el seguimiento
        # se apagaría al cerrar el navegador.
        want_infer = (self.cfg.always_infer or want_draw
                      or self._consumers_want_inference())
        want_raw = (self._raw_clients > 0
                    or (self._follow_clients > 0 and not self.cfg.default_infer)
                    or rec_raw)
        return want_draw, want_infer, want_raw

    def _consumers_want_inference(self) -> bool:
        for c in self._consumers:
            try:
                if c.wants_inference():
                    return True
            except Exception:
                pass
        return False

    def _consumers_want_frames(self) -> tuple[bool, bool]:
        """(quiere_crudo, quiere_anotado) pedido por los consumidores.

        El gemelo de `_consumers_want_inference`, pero para la CODIFICACIÓN.
        Deliberadamente NO se resuelve con `add_client()`: `client_count`
        gobierna el arranque y la parada de la sesión (ver `stop`, `add_client`)
        y la lógica de reconexión del lector, así que un "cliente" interno
        permanente dejaría la cámara reintentando en bucle contra una placa
        dormida — justo el fallo que ese código evita.
        """
        raw = draw = False
        for c in self._consumers:
            fn = getattr(c, "wants_frames", None)
            if fn is None:
                continue
            try:
                quiere = fn()
            except Exception:
                continue
            if quiere == "raw":
                raw = True
            elif quiere == "annotated":
                draw = True
        return raw, draw

    def _instrument_tracker(self, model) -> None:
        """Apunta cuántas cajas trae el detector ANTES de que el tracker filtre.

        Existe por un fallo muy concreto: el servicio dibujaba cero cajas en
        frames donde el mismo modelo, offline y con los mismos ajustes,
        encontraba a la persona al 0,81. Con `results.boxes` a secas no se
        puede distinguir "el detector no vio nada" de "el tracker se lo comió",
        porque `on_predict_postprocess_end` de ultralytics SUSTITUYE los
        resultados por los que tengan track.

        Así que se registra un callback propio para el mismo evento. Ultralytics
        los ejecuta en orden de registro y el suyo ya está puesto, así que este
        corre después; para ver el crudo se lee `len(predictor.results[i])`
        antes de que nadie lo toque, lo que obliga a engancharse al evento
        ANTERIOR (`on_predict_postprocess_end` se dispara una vez por lote, y el
        del tracker se registró primero). Por eso lo que se apunta aquí es el
        recuento YA filtrado, y el crudo se obtiene interceptando el propio
        `tracker.update`, que es el único punto donde ambos números conviven.
        """
        try:
            predictor = getattr(model, "predictor", None)
            trackers = getattr(predictor, "trackers", None) if predictor else None
            if not trackers:
                return  # todavía no hay tracker: se instrumenta en el próximo frame
            tracker = trackers[0]
            if getattr(tracker, "_probe_installed", False):
                return
            original = tracker.update
            sesion = self

            def instrumented_update(dets, img=None, **kwargs):
                sesion._raw_dets_last = len(dets)
                salida = original(dets, img, **kwargs)
                sesion._dets_after_tracker = len(salida)
                if len(dets) > 0 and len(salida) == 0:
                    sesion._frames_filtered += 1
                return salida

            tracker.update = instrumented_update
            tracker._probe_installed = True
        except Exception as e:
            # La instrumentación NO puede tumbar el pipeline de vídeo.
            print(f"[{self.cfg.camera_id}] no se pudo instrumentar el tracker: {e!r}")

    def keepalive_setting(self, name: str):
        """El ajuste del keep-alive de esta cámara, o el global si no lo tiene.

        `CameraConfig` guarda los overrides como `Optional`, con None = heredar.
        La resolución aparecía copiada en varios sitios y era justo el tipo de
        detalle que se olvida en uno de ellos.

        `keepalive_enabled` pasa además por `resolve_keepalive()`, que traduce
        el "auto" del global (activo solo en GPU Pascal) a un bool.
        """
        own = getattr(self.cfg, name, None)
        if own is not None:
            return own
        if name == "keepalive_enabled":
            return GLOBAL_CONFIG.resolve_keepalive()
        return getattr(GLOBAL_CONFIG, name)

    def latest_frame(self):
        """El último frame decodificado, tal cual lo vio la inferencia."""
        return self._latest_frame

    def cached_model(self):
        """El objeto YOLO que está usando esta sesión, o None si no hay.

        Devuelve el MISMO objeto, no uno nuevo: el selftest necesita examinar
        precisamente el que lleva horas vivo.
        """
        return _loaded_models.get(f"{self.cfg.model_name}_{self.cfg.device}")

    def reset_tracker(self) -> bool:
        """Tira el estado de ByteTrack para que se reconstruya en el próximo frame.

        Hace falta porque no hay otra forma: `on_predict_start` hace
        `if hasattr(predictor, "trackers") and persist: return`, y el predictor
        vive en el modelo cacheado de `_loaded_models`, que sobrevive a
        `stop()`/`start()`. Sin esto, un tracker degradado solo se arregla
        reiniciando el proceso entero.
        """
        try:
            model = _loaded_models.get(f"{self.cfg.model_name}_{self.cfg.device}")
            predictor = getattr(model, "predictor", None) if model else None
            if predictor is None or not hasattr(predictor, "trackers"):
                return False
            del predictor.trackers
            self._frames_filtered = 0
            print(f"[{self.cfg.camera_id}] tracker reseteado")
            return True
        except Exception as e:
            print(f"[{self.cfg.camera_id}] no se pudo resetear el tracker: {e!r}")
            return False

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
        self._instrument_tracker(model)

        # Tensor de la dummy, reservado UNA vez: no tiene sentido asignar
        # memoria de GPU veinte veces por segundo para mandar siempre ceros.
        dummy_tensor = None
        if is_cuda:
            try:
                dummy_tensor = torch.zeros(
                    (1, 3, self.cfg.imgsz, self.cfg.imgsz),
                    dtype=next(model.model.parameters()).dtype,
                    device=self.cfg.device,
                )
            except Exception as e:
                print(f"[{self.cfg.camera_id}] no se pudo preparar la dummy de "
                      f"keep-alive: {e!r}")

        def dummy_fits() -> bool:
            """¿Da tiempo a meter un dummy antes del frame siguiente?

            El dummy cuesta lo mismo que una inferencia real, así que con un
            modelo pesado NO cabe: medido con yolo26m a 16,7 fps, el hueco entre
            frames es de 30 ms y el dummy cuesta 37. Lanzarlo igual metía el
            ciclo en 72 ms contra los 60 de la cámara, o sea perder en torno al
            17 % de los frames.
            """
            if not self._ka_dummy_ms or self.pipeline_fps <= 0:
                return True                      # todavía sin datos: se prueba
            periodo_ms = 1000.0 / self.pipeline_fps
            hueco_ms = periodo_ms - (self.last_inference_ms or 0.0)
            return hueco_ms > self._ka_dummy_ms

        def warm_up():
            """Mantiene la GPU ocupada SIN pasar por el predictor de ultralytics.

            Antes esto era `model.predict(...)`, y tenía un efecto secundario
            que costó encontrar: `register_tracker` engancha los callbacks del
            tracker al MODELO, y `predict()` reutiliza el predictor que montó
            `track()`. O sea que cada dummy metía un frame negro y vacío en
            ByteTrack, le avanzaba el `frame_id` y mandaba todos los tracks a
            "perdidos" (medido: de `activos=2` a `perdidos=2` con una sola
            dummy). Y como el modelo se cachea en `_loaded_models` y
            `on_predict_start` no reconstruye los trackers con `persist=True`,
            ese estado degradado no se limpiaba ni parando la cámara.

            Un forward crudo sobre el módulo ejercita exactamente los mismos
            kernels —es la misma red— así que sigue sirviendo igual como
            workaround del P-state, pero no puede tocar el estado de nadie.
            """
            t_dummy = time.perf_counter()
            try:
                if dummy_tensor is not None:
                    with torch.no_grad():
                        model.model(dummy_tensor)
                else:
                    # Sin CUDA el keep-alive no llega a llamarse (lo corta
                    # _should_keepalive), pero si alguien lo fuerza, que al
                    # menos no reviente.
                    model.predict(np.zeros((640, 640, 3), dtype=np.uint8),
                                  imgsz=640, verbose=False)
                self._ka_dummies += 1
                # Media móvil suave del coste: es lo que decide si el dummy
                # siguiente cabe o no.
                ms = (time.perf_counter() - t_dummy) * 1000
                self._ka_dummy_ms = (ms if not self._ka_dummy_ms
                                     else 0.8 * self._ka_dummy_ms + 0.2 * ms)
            except Exception as e:
                print(f"[{self.cfg.camera_id}] keep-alive de GPU falló:", repr(e))

        last_frame = time.monotonic()
        idle = False

        while not stop_event.is_set():
            keepalive_on = self.keepalive_setting("keepalive_enabled")
            # Se recalculan en cada vuelta y ANTES del get(), porque el
            # keep-alive necesita saber si va a haber inferencia para decidir si
            # calienta y cuánto espera. Releerlos cada vez es además lo que
            # permite tocar default_infer o always_infer por API y que los
            # streams ya abiertos cambien sin reconectar.
            #
            want_draw, want_infer, want_raw = self._work_needed()

            idle_limit = GLOBAL_CONFIG.keepalive_idle_limit_sec
            since_frame = time.monotonic() - last_frame

            # DOS preguntas distintas, y confundirlas costó un bug: el bucle
            # entraba y salía de "pausa" en cada frame, llenando el log y
            # llamando a warm_up() una vez por frame.
            #
            # 1) ¿Está armed el keep-alive? O sea: ¿llegan frames y va a haber
            #    inferencia que proteger? Esto es lo que gobierna el reposo del
            #    hilo y los mensajes de pausa/reanudación.
            armed = _should_keepalive(is_cuda, keepalive_on, since_frame,
                                       idle_limit, want_infer)
            # 2) ¿Hace falta calentar AHORA? Solo si además la GPU se ha
            #    dormido de verdad. Con un modelo pesado los relojes se
            #    mantienen solos y aquí no se calienta nada, pero el keep-alive
            #    sigue armed: no está en pausa, está esperando a que haga falta.
            gpu_slow = gpu_underclocked(GLOBAL_CONFIG.keepalive_clock_ratio) \
                if armed else False
            warming = _should_keepalive(is_cuda, keepalive_on, since_frame,
                                        idle_limit, want_infer, gpu_slow)
            # Si ya no toca calentar, esperar 1s en vez de 50ms: así el hilo
            # queda de verdad en reposo en lugar de girar a 20 Hz sin hacer nada.
            wait_timeout = (self.keepalive_setting("keepalive_interval_sec")
                            if armed else 1.0)
            self._ka_warming = warming

            if is_cuda and keepalive_on and not armed and not idle:
                idle = True
                self._ka_idle = True
                reason = ("nadie pide inferencia" if not want_infer
                          else f"{idle_limit:.0f}s sin frames")
                print(f"[{self.cfg.camera_id}] {reason} -> keep-alive de GPU en pausa")

            try:
                frame = raw_queue.get(timeout=wait_timeout)
            except queue.Empty:
                # Sin frame: que los consumidores puedan caducar su objetivo en
                # vez de quedarse apuntando a algo que ya no se ve.
                self._notify_consumers("on_idle")
                # `warming` ya incluye que la GPU esté baja de relojes: si está
                # a pleno rendimiento no se calienta, pero tampoco se considera
                # que el keep-alive esté en pausa.
                if warming:
                    if dummy_fits():
                        warm_up()
                    else:
                        self._ka_skipped += 1
                if stop_event.is_set():
                    break
                continue

            last_frame = time.monotonic()
            self._latest_frame = frame

            self._update_fps(fps_window)

            annotated_bytes = None
            raw_bytes = None

            if want_infer:
                try:
                    if idle:
                        # Veníamos de dejar enfriar la GPU (sin frames, o sin
                        # nadie que pidiera inferencia). Una dummy justo antes
                        # de la primera de verdad conserva la garantía del
                        # workaround de P-state: la buena no sale corrupta.
                        # Aquí y no al recibir el frame, para no calentar por un
                        # frame que no se va a inferir.
                        idle = False
                        self._ka_idle = False
                        print(f"[{self.cfg.camera_id}] keep-alive de GPU reanudado")
                        warm_up()
                    t0 = time.time()
                    results = model.track(
                        frame, persist=True, conf=self.cfg.confidence,
                        imgsz=self.cfg.imgsz, verbose=False,
                        tracker="bytetrack.yaml", classes=self.cfg.classes,
                    )[0]
                    inference_ms = (time.time() - t0) * 1000

                    # El tracker no existe hasta que corre el primer track(),
                    # así que la sonda se coloca aquí. Es idempotente.
                    self._instrument_tracker(model)

                    # Las cajas se traducen UNA vez a objetos planos: los usan
                    # tanto el dibujo como los consumidores, y así estos últimos
                    # no dependen de ultralytics y se pueden probar sin GPU.
                    dets = [
                        Detection(
                            *[float(v) for v in box.xyxy[0]],
                            cls=int(box.cls),
                            label=model.names[int(box.cls)],
                            conf=float(box.conf[0]),
                            track_id=int(box.id) if box.id is not None else None,
                        )
                        for box in results.boxes
                    ]

                    # Canario del bug de P-state: la confianza sale de una
                    # sigmoide, así que un valor fuera de [0,1] no es "una
                    # detección rara", es la GPU devolviendo basura porque está
                    # a los relojes del escritorio. Se vio un 1.812 en
                    # producción y nadie se enteró, porque nada lo miraba.
                    corrupt = [d for d in dets if _is_corrupt(d)]
                    if corrupt:
                        self._corrupt_dets += len(corrupt)
                        peor = max(corrupt, key=lambda d: abs(d.conf))
                        self._last_corruption = {
                            "when": time.time(),
                            "conf": round(peor.conf, 3),
                            "label": peor.label,
                        }
                        if self._corrupt_dets - len(corrupt) == 0 or \
                                self._corrupt_dets % 50 < len(corrupt):
                            print(f"[{self.cfg.camera_id}] INFERENCIA CORRUPTA: "
                                  f"confianza {peor.conf:.3f} fuera de [0,1]. "
                                  f"La GPU está dando resultados basura "
                                  f"(bug de P-state); revisa el keep-alive.")
                        # No se propagan: una caja con confianza 1.8 movería los
                        # servos hacia un fantasma y dispararía una grabación.
                        # Se filtra por el mismo predicado y no por pertenencia:
                        # `d not in corrupt` compara por VALOR, así que dos
                        # detecciones idénticas se irían las dos.
                        dets = [d for d in dets if not _is_corrupt(d)]

                    self._frames_inferred += 1
                    if dets:
                        self._consecutive_without_box = 0
                        self._last_detection = time.time()
                        confs = [d.conf for d in dets]
                        self._last_conf_range = [round(min(confs), 3),
                                                   round(max(confs), 3)]
                    else:
                        self._frames_without_box += 1
                        self._consecutive_without_box += 1
                        # El aviso que faltaba: el detector vio algo y no ha
                        # llegado nada al dibujo. Se loguea una vez por racha
                        # para no inundar el log a 15 fps.
                        # `corrupt` en la condición: si el frame se quedó sin
                        # cajas porque el canario las descartó, el tracker no ha
                        # tenido nada que ver. Mandar a alguien a /tracker/reset
                        # por un problema de GPU es mandarlo al sitio
                        # equivocado, y encontrar el sitio correcto en este
                        # fallo costó una sesión entera.
                        if (self._raw_dets_last > 0 and not corrupt
                                and self._consecutive_without_box == 1):
                            print(f"[{self.cfg.camera_id}] el tracker se ha comido "
                                  f"las {self._raw_dets_last} cajas del detector "
                                  f"(POST /cameras/{self.cfg.camera_id}/tracker/reset)")

                    if want_draw:
                        annotated = frame.copy()
                        for d in dets:
                            tid = d.track_id if d.track_id is not None else -1
                            cv2.rectangle(annotated, (int(d.x1), int(d.y1)), (int(d.x2), int(d.y2)),
                                          BOX_COLOR, BOX_THICKNESS)
                            cv2.circle(annotated, (int(d.cx), int(d.cy)), CENTER_DOT_RADIUS, BOX_COLOR, -1)
                            cv2.putText(annotated, f"{d.label} {d.conf:.2f} #{tid}",
                                        (int(d.x1), int(d.y1) - 8),
                                        cv2.FONT_HERSHEY_SIMPLEX, FONT_SCALE, BOX_COLOR, FONT_THICKNESS)

                        cv2.putText(annotated, f"{inference_ms:.0f} ms ({self.cfg.device}) | "
                                                f"{self.pipeline_fps:.1f} fps", (10, 20),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)

                        ok, buf = cv2.imencode(".jpg", annotated)
                        if ok:
                            annotated_bytes = buf.tobytes()
                    self.last_inference_ms = inference_ms

                    h, w = frame.shape[:2]
                    self._notify_consumers("on_detections", dets, w, h)
                except Exception as e:
                    self.last_error = repr(e)
                    print(f"[{self.cfg.camera_id}] error en track/dibujo:", repr(e))

            # Solo se codifica el crudo si alguien lo va a leer. Antes la
            # condición era `want_raw or annotated_bytes is None`, y con
            # always_infer y nadie mirando (want_draw False -> annotated_bytes
            # None) se codificaba un JPEG por frame que no leía nadie: ~15
            # imencode por segundo y cámara a la basura. El segundo término se
            # conserva pero atado a want_draw, que es el caso que le da sentido:
            # alguien mira el anotado y la inferencia ha fallado, así que al
            # menos se le sirve la imagen cruda.
            if want_raw or (want_draw and annotated_bytes is None):
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

            # Al disco DESPUÉS de publicar al stream: quien está mirando en
            # directo no tiene por qué esperar a que el grabador apunte nada.
            # Y después de on_detections (más arriba) a propósito, para que la
            # decisión de "esto dispara un clip" se tome sobre el frame N antes
            # de que llegue el JPEG del frame N: así el frame que dispara acaba
            # DENTRO del clip en vez de ser el primero que se pierde.
            if raw_bytes is not None or annotated_bytes is not None:
                self._notify_consumers("on_jpeg", raw_bytes, annotated_bytes,
                                       self.last_frame_time)

    # -- salida hacia los clientes ----------------------------------------

    def mjpeg_generator(self, infer: Optional[bool]):
        mode = self._resolve_mode(infer)
        self.add_client(mode)
        last_seq_seen = -1
        try:
            # is_shutting_down() además de _stop_event: en un Ctrl+C el
            # lifespan (que es quien marca _stop_event) no corre hasta DESPUÉS
            # de que uvicorn se canse de esperar a este mismo generador. Ver el
            # comentario de _install_shutdown_signal_hook. Nos despierta el
            # notify_all() de cada frame publicado, así que salimos en torno a
            # un frame (~50-100 ms); en el peor caso, el timeout de 5s de abajo.
            while not self._stop_event.is_set() and not is_shutting_down():
                with self._cond:
                    # timeout de 1s (no 5): es cada cuánto revisamos las
                    # banderas de parada si la cámara ha dejado de publicar
                    # frames. Con 5s, un cliente pegado a una cámara parada
                    # podía tardar más que el --timeout-graceful-shutdown en
                    # enterarse del apagado. El coste es reenviar el último
                    # frame una vez por segundo en vez de cada cinco.
                    self._cond.wait_for(lambda: self._frame_seq != last_seq_seen
                                                or self._stop_event.is_set()
                                                or is_shutting_down(),
                                         timeout=1.0)
                    if self._stop_event.is_set() or is_shutting_down():
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
            "manual_stop": self.cfg.manual_stop,
            "raw_clients": self._raw_clients,
            "infer_clients": self._infer_clients,
            "follow_clients": self._follow_clients,
            "last_frame_time": self.last_frame_time,
            "last_inference_ms": self.last_inference_ms,
            "pipeline_fps": round(self.pipeline_fps, 1),
            "last_error": self.last_error,
            "esphome_connected": self.esphome.is_connected if self.esphome else None,
            "consumers": {type(c).__name__: c.status() for c in self._consumers},
            # Lo que contesta "¿por qué no veo cajas?" sin tener que adivinar:
            # si raw_dets > 0 y dets_after_tracker == 0, las pierde el tracker;
            # si raw_dets == 0, no las está viendo el detector.
            "inference_health": {
                "frames_inferred": self._frames_inferred,
                "frames_without_box": self._frames_without_box,
                "consecutive_without_box": self._consecutive_without_box,
                "raw_dets": self._raw_dets_last,
                "dets_after_tracker": self._dets_after_tracker,
                "frames_filtered_by_tracker": self._frames_filtered,
                "corrupt_detections": self._corrupt_dets,
                "last_corruption": self._last_corruption,
                "last_conf_range": self._last_conf_range,
                "last_detection_ago_sec": (
                    round(time.time() - self._last_detection, 1)
                    if self._last_detection else None),
            },
            "gpu_keepalive": {
                # Ya resueltos contra GLOBAL_CONFIG: antes había que deducirlos.
                "enabled": self.keepalive_setting("keepalive_enabled"),
                "interval_sec": self.keepalive_setting("keepalive_interval_sec"),
                "warming": self._ka_warming,
                "idle": self._ka_idle,
                "dummies": self._ka_dummies,
                "dummy_ms": round(self._ka_dummy_ms, 1) or None,
                # Dummies que no se lanzaron porque no cabían antes del frame
                # siguiente. Si esto sube sin parar, el modelo es demasiado
                # pesado para calentar entre frames (y probablemente no lo
                # necesita).
                "dummies_skipped": self._ka_skipped,
                # El único dato de hardware que se queda aquí, porque es el que
                # explica todo lo demás de un vistazo: por debajo de ~95 la GPU
                # se ha dormido y la inferencia empieza a salir corrupta. Los
                # relojes en crudo, el P-state y los ajustes globales están en
                # GET /gpu y GET /config; repetirlos en cada cámara era ruido.
                "gpu_clock_pct": gpu_clock_pct(),
            },
            "config": self.cfg.dict(),
        }
