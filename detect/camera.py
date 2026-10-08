"""El pipeline de video: leer del ESP32, inferir con YOLO y repartir el stream.

Aqui vive todo lo que ocurre por frame. Los endpoints estan en main.py y la
conexion con la placa en esphome_api.py.
"""

from __future__ import annotations

import json
import math
import queue
import re
import socket
import threading
import time
from collections import deque
from pathlib import Path
from typing import Iterable, Iterator, Literal, Optional
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


# cuDNN NO se configura aquí: la decisión vive en `CUDNN_ENABLED`, arriba del
# todo en main.py, que es el fichero que uno abre. Aquí estuvo antes, y también
# como variable de entorno; las dos veces quedaba escondida.


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
    mientras se reintenta, ahoga el log.
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

# Modelo por defecto de una cámara. Un medium y no un nano a propósito: en la
# GTX 1080 un modelo ligero deja dormirse a la GPU entre frames y las
# inferencias salen corruptas; yolo26m la mantiene despierta él solo (97 % de
# reloj, 0 corruptas en 4935 frames). yolo26n se puede elegir igual: las
# detecciones corruptas se descartan y se cuentan (`_is_corrupt`), pero en cada
# bajada a P5 se pierden unos segundos de detección. Ver docs/GPU.md.
DEFAULT_MODEL = "yolo26m"


class GlobalConfig:
    def __init__(self):
        self.reconnect_delay_sec: float = 1.0
        # Ventana OpenGL oculta para que la GTX 1080 no baje a P5 con yolo26n
        # (gl_keeper.py). Apagada por defecto: solo sirve con el perfil del
        # driver en Prefer maximum performance.
        self.gl_keeper_enabled: bool = False

    def as_dict(self):
        return {
            "reconnect_delay_sec": self.reconnect_delay_sec,
            "gl_keeper_enabled": self.gl_keeper_enabled,
        }

    # Un global_config.json de una versión anterior traerá claves que ya no
    # existen (`keepalive_enabled`, por ejemplo). No pasa nada: se recorre
    # esta lista y lo que sobra se ignora.
    _PERSISTED = ("reconnect_delay_sec", "gl_keeper_enabled")

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
        Lo guardado MANDA sobre los valores por defecto del código, igual que
        cameras_config.json. Por eso **cada valor se valida contra el tipo del
        defecto** y el que no cuadra se ignora con un aviso: un fichero viejo no
        puede reintroducir una opción que ya no existe.
        """
        if not GLOBAL_CONFIG_FILE.exists():
            return
        try:
            data = json.loads(GLOBAL_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"No se pudo leer {GLOBAL_CONFIG_FILE}: {e!r}; "
                  f"se usan los valores por defecto")
            return
        if not isinstance(data, dict):
            print(f"{GLOBAL_CONFIG_FILE.name} no contiene un objeto JSON; "
                  f"se usan los valores por defecto")
            return

        defaults = GlobalConfig.__new__(GlobalConfig)
        GlobalConfig.__init__(defaults)     # los defectos del código, sin tocar self
        ignored = []
        for k in self._PERSISTED:
            if k not in data:
                continue
            value, expected = data[k], type(getattr(defaults, k))
            if expected is bool:
                ok = isinstance(value, bool)
            elif expected is float:
                # JSON escribe 3 donde el defecto es 3.0, así que un int vale;
                # un bool NO, aunque Python lo considere subclase de int.
                ok = isinstance(value, (int, float)) and not isinstance(value, bool)
                if ok:
                    value = float(value)
            else:
                ok = isinstance(value, expected)
            if ok:
                setattr(self, k, value)
            else:
                ignored.append(f"{k}={data[k]!r}")

        print(f"Ajustes globales cargados desde {GLOBAL_CONFIG_FILE.name}")
        if ignored:
            print(f"  valores ignorados por no ser válidos (se usa el defecto): "
                  f"{', '.join(ignored)}")


GLOBAL_CONFIG_FILE = Path(__file__).with_name("global_config.json")
GLOBAL_CONFIG = GlobalConfig()


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


# ---------------------------------------------------------------------------
# Modelos YOLO cacheados, UNO POR CÁMARA
# ---------------------------------------------------------------------------

_loaded_models: dict[str, YOLO] = {}
_models_lock = threading.Lock()


def model_key(name: str, device: str, owner: Optional[str] = None) -> str:
    return f"{name}_{device}_{owner or 'shared'}"


def get_model(name: str, device: str, owner: Optional[str] = None) -> YOLO:
    """El modelo de `owner`, cargándolo la primera vez.

    **`owner` es lo que impide que dos cámaras compartan el tracker.** La caché
    estuvo indexada solo por modelo+device, así que dos cámaras con el mismo
    `yolo26n` en `cuda` recibían el MISMO objeto `YOLO`, y con él el mismo
    predictor y el mismo ByteTrack. Con una sola cámara no se nota; con dos, el
    estado de seguimiento de ambas escenas se mezcla en un único tracker, los
    track_id saltan de una a otra, un `reset_tracker` en una afecta a la otra, y
    encima dos hilos `yolo-*` llaman a `model.track()` sobre el mismo predictor
    sin ningún lock.

    El precio es una copia de los pesos por cámara (5,5 MB en `yolo26n`, 44 MB
    en `yolo26m`), despreciable al lado de una detección que se equivoca de
    objetivo. Quien no necesite tracking —`/detect-file`, los scripts de
    `test/`— llama sin `owner` y sigue compartiendo instancia.
    """
    key = model_key(name, device, owner)
    with _models_lock:
        if key not in _loaded_models:
            m = YOLO(f"{name}.pt")
            m.to(device)
            dummy = np.zeros((640, 640, 3), dtype=np.uint8)
            m.predict(dummy, verbose=False)
            _loaded_models[key] = m
            print(f"Modelo {name} precalentado en {device} y listo"
                  f"{f' (para {owner})' if owner else ''}")
        return _loaded_models[key]


def release_model(name: str, device: str, owner: str) -> bool:
    """Suelta el modelo de una cámara al darla de baja.

    Antes la caché no se vaciaba nunca y la VRAM de un modelo no se recuperaba
    al borrar la última cámara que lo usaba. Con una instancia por cámara eso
    pasa de ser una nota al pie a una fuga de verdad: dar de alta y de baja
    cámaras iría llenando la tarjeta.
    """
    key = model_key(name, device, owner)
    with _models_lock:
        if _loaded_models.pop(key, None) is None:
            return False
    try:
        if device.startswith("cuda"):
            torch.cuda.empty_cache()
    except Exception:
        pass
    print(f"Modelo {name} de {owner} liberado")
    return True


# Dónde corre el endpoint de prueba puntual (/detect-file). El modelo lo elige
# quien llama, y se carga de forma perezosa vía get_model() la primera vez, para
# no penalizar el arranque del servicio si solo se usan las cámaras.
DEFAULT_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"


# ---------------------------------------------------------------------------
# Config de una cámara
# ---------------------------------------------------------------------------

# Grados en sentido horario -> constante de cv2.rotate (0 = no se toca).
_ROTATIONS = {
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}

class CameraConfig(BaseModel):
    camera_id: str
    stream_url: str
    model_name: str = DEFAULT_MODEL
    device: str = "cuda"
    confidence: float = 0.5
    imgsz: int = 640
    classes: Optional[list[int]] = None  # None = todas las clases
    default_infer: bool = True  # usado por /stream y /snapshot cuando no se pasa ?infer=

    # Giro de la imagen en grados, en sentido horario, para cuando el módulo de
    # cámara va montado de lado. El sensor solo sabe voltear (vflip/hmirror),
    # no girar 90°, así que se gira aquí nada más decodificar: YOLO, el stream,
    # los clips y los servos ven ya la imagen derecha. Los servos no necesitan
    # nada más mientras pan siga siendo horizontal y tilt vertical en el mundo.
    rotation: Literal[0, 90, 180, 270] = 0

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

        # Frames con alguna detección imposible (confianza fuera de [0,1] o
        # coordenadas no finitas). Son la señal inequívoca del bug de P-state.
        # Se cuentan frames y no cajas: un frame roto puede traer cientos de
        # cajas basura, y contarlas hacía parecer mil fallos lo que eran tres.
        self._corrupt_frames = 0

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
        La excepción es `source`: lo lee el hilo escritor al abrir el fichero,
        así que un cambio a media grabación no surte efecto hasta el clip
        siguiente.
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
            self.servo_tracker.set_config(cfg)
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
                    rotation = self.cfg.rotation
                    if rotation:
                        frame = cv2.rotate(frame, _ROTATIONS[rotation])
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
        # para siempre. (Cuando existía el keep-alive de GPU, eso era un 54 %
        # de una GTX 1080 con las dos cámaras desenchufadas y cero frames
        # entrando.)
        #
        # La comparación por identidad no es opcional: si mientras este hilo
        # agonizaba ya arrancó otra generación, self._stop_event es un objeto
        # distinto del nuestro y llamar a stop() mataría a la generación NUEVA.
        if self._stop_event is stop_event and not stop_event.is_set():
            self.stop(explicit=True)

    # -- hilo de proceso: YOLO -------------------------------------------
    #
    # Diagnóstico confirmado (31/08/2026): la GTX 1080 produce inferencias
    # corruptas (confianzas fuera de 0-1) cuando el driver baja el estado de
    # energía (P5) entre frames. Se confirmó con nvidia-smi que las anomalías
    # coinciden al segundo exacto con transiciones de P-state. Un modelo
    # pesado (yolo26m) mantiene la GPU despierta él solo; con uno ligero las
    # detecciones corruptas se descartan y se cuentan. Ver docs/GPU.md.

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

    def reset_tracker(self) -> bool:
        """Tira el estado de ByteTrack para que se reconstruya en el próximo frame.

        Hace falta porque no hay otra forma: `on_predict_start` hace
        `if hasattr(predictor, "trackers") and persist: return`, y el predictor
        vive en el modelo cacheado de `_loaded_models`, que sobrevive a
        `stop()`/`start()`. Se llama en cada arranque de la sesión.
        """
        try:
            model = _loaded_models.get(model_key(self.cfg.model_name, self.cfg.device, self.cfg.camera_id))
            predictor = getattr(model, "predictor", None) if model else None
            if predictor is None or not hasattr(predictor, "trackers"):
                return False
            del predictor.trackers
            print(f"[{self.cfg.camera_id}] tracker reseteado")
            return True
        except Exception as e:
            print(f"[{self.cfg.camera_id}] no se pudo resetear el tracker: {e!r}")
            return False

    def _drop_corrupt(self, dets: list[Detection]) -> list[Detection]:
        """Canario del bug de P-state: descarta el frame si trae basura.

        La confianza sale de una sigmoide, así que un valor fuera de [0,1] no
        es "una detección rara", es la GPU devolviendo basura (sale en los
        cambios de P-state, ver docs/GPU.md). Se vio un 1.812 en producción y
        nadie se enteró, porque nada lo miraba.

        Se descarta el frame **entero**, no solo las cajas imposibles: si la
        inferencia ha salido rota, las cajas con confianza normal de ese mismo
        frame tampoco son de fiar, y una caja fantasma movería los servos o
        dispararía una grabación. El frame cuenta como "no se ha visto nada".
        """
        corrupt = [d for d in dets if _is_corrupt(d)]
        if not corrupt:
            return dets
        self._corrupt_frames += 1
        peor = max(corrupt, key=lambda d: abs(d.conf))
        if self._corrupt_frames == 1 or self._corrupt_frames % 50 == 0:
            print(f"[{self.cfg.camera_id}] INFERENCIA CORRUPTA "
                  f"(frame n.º {self._corrupt_frames}): "
                  f"{len(corrupt)} caja(s) imposible(s) de {len(dets)}, la peor "
                  f"con confianza {peor.conf:.3f}. La GPU ha cambiado de "
                  f"P-state y da resultados basura; se descarta el frame. Con "
                  f"una sola cámara se recomienda yolo26m (ver docs/GPU.md).")
        return []

    def _process_loop(self):
        # Referencias LOCALES de esta generación (ver comentario en _read_loop
        # sobre por qué no se puede usar self._stop_event/self._raw_queue
        # directamente: una nueva generación podría reasignarlos mientras
        # este hilo sigue vivo).
        stop_event = self._stop_event
        raw_queue = self._raw_queue
        fps_window: deque = deque()

        model = get_model(self.cfg.model_name, self.cfg.device, self.cfg.camera_id)

        while not stop_event.is_set():
            # Se recalculan en cada vuelta: así cambiar default_infer o
            # always_infer por API afecta a los streams ya abiertos.
            want_draw, want_infer, want_raw = self._work_needed()

            try:
                # El timeout solo sirve para revisar stop_event y avisar a
                # los consumidores de que no llega nada.
                frame = raw_queue.get(timeout=1.0)
            except queue.Empty:
                # Sin frame: que los consumidores puedan caducar su objetivo en
                # vez de quedarse apuntando a algo que ya no se ve.
                self._notify_consumers("on_idle")
                if stop_event.is_set():
                    break
                continue

            self._update_fps(fps_window)

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
                    dets = self._drop_corrupt(dets)

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
                    # yield vacío y no `continue`: este generador es síncrono y
                    # Starlette corre cada next() en el threadpool de AnyIO (40
                    # hilos). Con `continue`, una cámara sin frames (dormida,
                    # caída) dejaba el next() girando aquí para siempre: el hilo
                    # no vuelve nunca, Starlette no puede cancelarlo aunque el
                    # cliente se vaya, y el finally no corre. HA abre el stream
                    # cada pocos segundos para sacar la imagen fija, así que en
                    # un rato se agotaban los 40 hilos y NINGÚN stream de
                    # ninguna cámara arrancaba. Un trozo vacío no escribe nada
                    # en el socket pero devuelve el hilo en ~1s (el timeout de
                    # arriba) y deja a Starlette cerrar si el cliente se fue.
                    yield b""
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
            # Los tres contadores de clientes se publican sumados: el reparto
            # por modo solo le importa a _work_needed().
            "clients": self.client_count,
            "last_frame_time": self.last_frame_time,
            "last_inference_ms": self.last_inference_ms,
            "pipeline_fps": round(self.pipeline_fps, 1),
            "esphome_connected": self.esphome.is_connected if self.esphome else None,
            "corrupt_frames": self._corrupt_frames,
            "consumers": {type(c).__name__: c.status() for c in self._consumers},
            "config": self.cfg.dict(),
        }
