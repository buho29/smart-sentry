"""
Servicio de detección de objetos en tiempo real con YOLO y ESPHome.


"""

from __future__ import annotations

import asyncio
import io
import json
import queue
import re
import signal
import socket
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
from aioesphomeapi import APIClient
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from ultralytics import YOLO

# Todos los mensajes de este servicio salen por print(). Lo envolvemos una
# sola vez para anteponer la hora local en el mismo formato que usa el log de
# ESPHome ([HH:MM:SS.mmm]), y así poder cotejar tiempos entre ambos logs.
_orig_print = print


def print(*args, **kwargs):  # noqa: A001
    t = time.time()
    ts = time.strftime("%H:%M:%S", time.localtime(t))
    _orig_print(f"[{ts}.{int(t % 1 * 1000):03d}]", *args, **kwargs)


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
# Content-Length real que declara cada parte, en vez de buscar a mano
# \xff\xd8/\xff\xd9 en los bytes (ese método se desincronizaba si esos bytes
# aparecían por casualidad DENTRO de los datos comprimidos de un JPEG real,
# causando cuelgues de hasta varios minutos -- confirmado con pruebas
# aisladas moviendo la cámara físicamente).
_CONTENT_LENGTH_RE = re.compile(rb"Content-Length:\s*(\d+)", re.IGNORECASE)


def _motivo(e: BaseException, max_len: int = 90) -> str:
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
    publica `estado=off` justo antes de dormirse) y desde endpoints async,
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
# Aviso temprano de apagado
# ---------------------------------------------------------------------------
#
# uvicorn apaga en tres pasos, en este orden (uvicorn/server.py, Server.shutdown):
#   1. deja de aceptar conexiones nuevas;
#   2. ESPERA a que terminen las respuestas en vuelo, como mucho
#      --timeout-graceful-shutdown segundos, y si expira las cancela a la
#      fuerza ("Cancel N running task(s), timeout graceful shutdown exceeded");
#   3. solo entonces ejecuta el shutdown del lifespan, que es donde nosotros
#      paramos las cámaras.
#
# Nuestros streams MJPEG son bucles infinitos que solo salían cuando el
# lifespan marcaba _stop_event, o sea en el paso 3 -- que no llega hasta que el
# paso 2 se rinde. Bloqueo circular: con clientes conectados, cada Ctrl+C
# costaba los 5s enteros y soltaba un CancelledError por cliente.
#
# Como uvicorn instala sus handlers de señal ANTES de arrancar el lifespan
# (Server.serve: `with self.capture_signals(): await self._serve(...)`), desde
# el arranque del lifespan podemos encadenarnos a ellos y enterarnos de la
# señal en el "paso 0". Los generadores miran esta bandera y salen solos, así
# que el paso 2 termina enseguida en vez de agotar el timeout.

_SHUTTING_DOWN = False

# Las mismas que captura uvicorn (server.py, HANDLED_SIGNALS): si nos
# encadenáramos a menos, un Ctrl+Break apagaría el servidor sin que los
# generadores se enteraran, que es justo el problema que esto arregla.
_SEÑALES_APAGADO = (signal.SIGINT, signal.SIGTERM)
if sys.platform == "win32":
    _SEÑALES_APAGADO += (signal.SIGBREAK,)  # Ctrl+Break


def is_shutting_down() -> bool:
    return _SHUTTING_DOWN


def _install_shutdown_signal_hook():
    """Encadena un handler propio a los que ya instaló uvicorn (ver arriba)."""
    if threading.current_thread() is not threading.main_thread():
        return  # signal.signal solo funciona desde el hilo principal

    for sig in _SEÑALES_APAGADO:
        try:
            previo = signal.getsignal(sig)
        except (ValueError, OSError):
            continue

        def handler(signum, frame, _previo=previo):
            # Ojo: esto corre dentro de un handler de señal. Una asignación a
            # un bool de módulo es atómica y segura aquí; un threading.Event
            # no lo sería del todo, porque set() coge un lock.
            global _SHUTTING_DOWN
            _SHUTTING_DOWN = True
            # Delegamos en el handler de uvicorn (Server.handle_exit), que es
            # quien de verdad arranca el apagado ordenado.
            if callable(_previo):
                _previo(signum, frame)

        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass  # p.ej. SIGTERM no soportado en esta plataforma


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Antes del yield: uvicorn ya tiene puestos sus handlers de señal, así que
    # este es el momento de encadenarnos a ellos.
    _install_shutdown_signal_hook()
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
        # Segundos sin recibir un frame real tras los cuales se deja de
        # calentar la GPU. El keep-alive solo tiene sentido ENTRE frames de un
        # stream vivo (huecos de decenas de ms); pasado este plazo la cámara no
        # está dando nada y seguir lanzando inferencias dummy a 20 Hz es quemar
        # la GPU para nada.
        self.keepalive_idle_limit_sec: float = 3.0
        self.reconnect_delay_sec: float = 1.0

    def as_dict(self):
        return {
            "keepalive_enabled": self.keepalive_enabled,
            "keepalive_interval_sec": self.keepalive_interval_sec,
            "keepalive_idle_limit_sec": self.keepalive_idle_limit_sec,
            "reconnect_delay_sec": self.reconnect_delay_sec,
        }


GLOBAL_CONFIG = GlobalConfig()


def _toca_keepalive(is_cuda: bool, activo: bool, segundos_sin_frame: float,
                    limite: float) -> bool:
    """¿Hay que lanzar una inferencia dummy para que la GPU no baje de P-state?

    Separado en una función pura para poder probar la lógica sin GPU. Ver el
    comentario largo sobre el bug de P-state de la GTX 1080 encima de
    _process_loop.
    """
    return is_cuda and activo and segundos_sin_frame < limite


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


# Modelo por defecto para los endpoints de prueba puntual (/detect y compañía).
# Se carga de forma perezosa la primera vez que se llama a uno de esos endpoints,
# para no penalizar el arranque del servicio si solo se usan las cámaras.
DEFAULT_MODEL_NAME = "yolo11m"
DEFAULT_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"


def get_default_model() -> YOLO:
    return get_model(DEFAULT_MODEL_NAME, DEFAULT_DEVICE)


# ---------------------------------------------------------------------------
# EsphomeController: conexión persistente a la API nativa de ESPHome
# (puerto 6053) de una placa, en su propio hilo con loop de asyncio propio,
# para poder llamarla/leerla desde hilos síncronos (como CameraSession)
# sin bloquearlos.
#
# De momento cubre dos usos:
#   - move_servo(pan, tilt): llama al servicio "set_servo_position" del
#     YAML si existe (no falla si aún no está definido, solo lo ignora).
#   - Vigila una entidad concreta (por object_id, p.ej. "estado", que en
#     el YAML de la placa es un binary_sensor) y llama a on_state_value(bool)
#     cada vez que cambia. Así una CameraSession puede arrancar/parar en
#     función del estado real del hardware (PIR + deep sleep) en vez de solo
#     por clientes HTTP.
#
# Reconexión: usa ReconnectLogic de la propia aioesphomeapi (la misma que
# usa Home Assistant), con backoff creciente si se cae la conexión.
# ---------------------------------------------------------------------------

class EsphomeController:
    """Conexión a la API nativa de ESPHome de una placa, manejando APIClient
    directamente (sin ReconnectLogic): un solo dispositivo, latencia
    controlada por nosotros, sin depender de mDNS ni de atributos privados
    de la librería.

    Reconexión: al desconectarse, reintenta una vez. Si falla, espera
    pasivamente hasta 'safety_retry_sec' (red de seguridad, por si el
    webhook falla) O hasta que notify_awake() la despierte antes -- eso es
    lo que llama el endpoint que golpea el 'on_connect' del propio ESP32.

    connect_attempt_timeout_sec: cada intento de connect() individual se
    acota a esto (en vez de fiarnos del timeout por defecto de la librería,
    que ronda los ~10s). Sin esto, si notify_awake() llega mientras ya hay
    un intento fallido "en vuelo" contra la placa todavía dormida, la señal
    se queda esperando a que ESE intento viejo agote su propio timeout
    antes de poder arrancar el intento bueno -- confirmado en logs reales
    (~10s de retraso entre WiFi conectado y el Accept en el ESP).
    """

    def __init__(
        self,
        address: str,
        noise_psk: Optional[str] = None,
        port: int = 6053,
        watch_entity_object_id: Optional[str] = None,
        on_state_value: Optional[Callable[[object], None]] = None,
        safety_retry_sec: float = 30.0,
        connect_attempt_timeout_sec: float = 1.0,
    ):
        self.address = address
        self.port = port
        self.noise_psk = noise_psk
        self.watch_entity_object_id = watch_entity_object_id
        self.on_state_value = on_state_value
        self.safety_retry_sec = safety_retry_sec
        self.connect_attempt_timeout_sec = connect_attempt_timeout_sec

        self._client: Optional[APIClient] = None
        self._servo_service = None
        self._watch_key: Optional[int] = None
        self._connected = threading.Event()
        self._stopping = False
        # Se marca cuando el hilo ya ha cerrado el socket y el event loop.
        # Sirve para que shutdown() sea idempotente y para que notify_awake()/
        # move_servo() no intenten programar nada en un loop ya cerrado.
        self._closed = threading.Event()

        # Creados aquí, se usan dentro del loop propio de este controller.
        self._disconnected_event = asyncio.Event()
        self._disconnected_event.set()  # empezamos "desconectados" -> primer intento inmediato
        self._wake_event = asyncio.Event()

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
        try:
            self._loop.run_until_complete(self._reconnect_loop())
        finally:
            # El cierre se hace DENTRO de este hilo, que es el único sitio
            # donde el loop sigue vivo y se puede esperar de verdad a que el
            # socket con la placa (puerto 6053) se cierre.
            #
            # Antes shutdown() hacía loop.stop() desde fuera justo después de
            # programar disconnect(): el loop paraba antes de que esa corrutina
            # llegara a ejecutarse, así que el socket quedaba abierto hasta que
            # moría el proceso (un descriptor filtrado por cada DELETE de
            # cámara), y run_until_complete de arriba reventaba con
            # "Event loop stopped before Future completed".
            self._close_loop()

    def _close_loop(self):
        """Cierre ordenado del cliente, las tareas pendientes y el loop.
        Solo se llama desde el propio hilo del controller."""
        try:
            if self._client is not None:
                self._loop.run_until_complete(
                    asyncio.wait_for(self._client.disconnect(), timeout=1.0)
                )
        except Exception:
            # Si la placa ya se ha dormido no hay nadie al otro lado y el
            # disconnect puede fallar o agotar el timeout: da igual, lo que
            # importa es que el socket local quede cerrado igualmente.
            pass
        try:
            pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
        except Exception:
            pass
        self._loop.close()
        self._closed.set()

    async def _reconnect_loop(self):
        self._client = APIClient(self.address, self.port, password="", noise_psk=self.noise_psk)
        while not self._stopping:
            await self._disconnected_event.wait()
            if self._stopping:
                break

            self._wake_event.clear()
            connect_task = asyncio.ensure_future(self._try_connect_once())
            wake_task = asyncio.ensure_future(self._wake_event.wait())
            done, _pending = await asyncio.wait(
                {connect_task, wake_task}, return_when=asyncio.FIRST_COMPLETED
            )

            if connect_task in done:
                wake_task.cancel()
                try:
                    ok = connect_task.result()
                except Exception:
                    ok = False
                if ok:
                    self._disconnected_event.clear()
                    # nos quedamos aquí, sin hacer nada, hasta que _on_stop
                    # vuelva a marcar _disconnected_event -- cero polling
                    # mientras está conectado.
                    continue
                # falló de verdad (agotó connect_attempt_timeout_sec u otro
                # error) -> esperamos hasta el siguiente aviso o hasta la
                # red de seguridad.
                self._wake_event.clear()
                # Ojo con este clear(): si shutdown() acaba de levantar
                # _wake_event, se lo lleva por delante y nos quedaríamos aquí
                # los safety_retry_sec (30s) enteros pese a estar parando. Como
                # shutdown() pone _stopping ANTES de programar el set, mirarlo
                # justo después del clear cierra la ventana por los dos lados:
                # si el clear ganó, esto lo ve; si no, el set despierta al wait.
                if self._stopping:
                    break
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=self.safety_retry_sec)
                except asyncio.TimeoutError:
                    pass
                # seguimos en el while: _disconnected_event sigue set -> reintenta
            else:
                # nos avisaron MIENTRAS el intento anterior seguía en curso
                # (p.ej. contra la placa todavía dormida): lo cancelamos en
                # vez de esperar a que agote su propio timeout, y dejamos el
                # cliente en un estado limpio antes de reintentar ya.
                connect_task.cancel()
                try:
                    await connect_task
                except (asyncio.CancelledError, Exception):
                    # CancelledError hereda de BaseException, NO de Exception:
                    # sin nombrarla aquí se escapaba de este except, subía por
                    # run_until_complete y mataba el hilo del controller con un
                    # traceback cada vez que shutdown() o notify_awake()
                    # cancelaban un intento de conexión en vuelo.
                    pass
                try:
                    # acotado: contra una placa dormida un disconnect sin
                    # límite podría pasarse del presupuesto de shutdown()
                    await asyncio.wait_for(self._client.disconnect(), timeout=1.0)
                except (asyncio.CancelledError, Exception):
                    pass
                # seguimos en el while: _disconnected_event sigue set -> reintenta ya, sin esperar

    async def _try_connect_once(self) -> bool:
        t0 = time.perf_counter()
        try:
            await asyncio.wait_for(
                self._client.connect(login=True, on_stop=self._on_stop),
                timeout=self.connect_attempt_timeout_sec,
            )
        except Exception as e:
            print(f"EsphomeController[{self.address}]: fallo al conectar: {e!r}")
            return False

        entities, services = await self._client.list_entities_services()
        self._servo_service = next((s for s in services if s.name == "set_servo_position"), None)

        self._watch_key = None
        if self.watch_entity_object_id:
            for e in entities:
                if getattr(e, "object_id", None) == self.watch_entity_object_id:
                    self._watch_key = e.key
                    break
            if self._watch_key is None:
                print(f"EsphomeController[{self.address}]: aviso, no encontré "
                      f"la entidad '{self.watch_entity_object_id}'")

        self._client.subscribe_states(self._on_state)
        self._connected.set()
        print(f"EsphomeController[{self.address}]: conectado en "
              f"{(time.perf_counter() - t0) * 1000:.0f}ms "
              f"({len(entities)} entidades, servo_service={'sí' if self._servo_service else 'no'})")
        return True

    async def _on_stop(self, expected_disconnect: bool = False):
        # Callback de APIClient.connect(on_stop=...) -- se llama al perderse
        # la conexión. La firma exacta (con/sin expected_disconnect) varía
        # entre versiones de aioesphomeapi; con valor por defecto aceptamos
        # ambas sin romper si algún día cambia otra vez.
        self._connected.clear()
        if not self._stopping:
            print(f"EsphomeController[{self.address}]: desconectado "
                  f"(esperado={expected_disconnect}), reintentando...")
        self._disconnected_event.set()

    def notify_awake(self):
        """Llamar cuando algo externo (el webhook del propio ESP32 al
        conectar WiFi) nos indica que la placa puede estar lista, para
        saltarnos la espera de safety_retry_sec y reintentar ya."""
        if self._closed.is_set():
            return
        try:
            self._loop.call_soon_threadsafe(self._wake_event.set)
        except RuntimeError:
            pass  # el loop se cerró entre el check y esta llamada

    def _on_state(self, state):
        # Llamado en el hilo/loop propio de este controller.
        if self._watch_key is not None and getattr(state, "key", None) == self._watch_key:
            # 'estado' es un binary_sensor: state.state es un bool. Antes de la
            # primera publicación, aioesphomeapi marca missing_state=True y
            # state.state no significa nada -> lo tratamos como "sin valor".
            if getattr(state, "missing_state", False):
                return
            value = getattr(state, "state", None)
            print(f"EsphomeController[{self.address}]: '{self.watch_entity_object_id}' -> {value!r}")
            if self.on_state_value is not None:
                try:
                    self.on_state_value(value)
                except Exception as e:
                    print(f"EsphomeController[{self.address}]: error en on_state_value callback:", repr(e))

    # -- servos --------------------------------------------------------

    def move_servo(self, pan: float, tilt: float):
        if not self._connected.is_set() or self._servo_service is None:
            return
        if self._closed.is_set():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._send_servo(pan, tilt), self._loop)
        except RuntimeError:
            pass  # el loop se cerró entre el check y esta llamada

    async def _send_servo(self, pan: float, tilt: float):
        try:
            self._client.execute_service(self._servo_service, {"pan": pan, "tilt": tilt})
        except Exception as e:
            print(f"EsphomeController[{self.address}]: error enviando servo:", repr(e))

    # -- apagado ---------------------------------------------------------

    def shutdown(self, timeout: float = 1.5):
        """Para el hilo y cierra la conexión con la placa. Idempotente.

        No para el loop a la fuerza: solo levanta los dos eventos que hacen
        salir a _reconnect_loop por su propio pie, y espera a que el hilo
        cierre el socket y el loop en _close_loop(). Así el disconnect()
        siempre llega a ejecutarse dentro del loop, que es donde asyncio
        puede esperarlo.
        """
        if self._closed.is_set():
            return
        self._stopping = True
        try:
            self._loop.call_soon_threadsafe(self._disconnected_event.set)
            self._loop.call_soon_threadsafe(self._wake_event.set)
        except RuntimeError:
            return  # el loop ya estaba cerrado
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            print(f"EsphomeController[{self.address}]: el hilo no terminó en "
                  f"{timeout}s (es daemon, no bloquea el cierre del proceso)")


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
    # usa el binary_sensor indicado (object_id, ON/OFF) para arrancar/parar la
    # lectura en vez de depender solo de que haya clientes HTTP mirando el
    # stream.
    noise_psk: Optional[str] = None  # api.encryption.key del YAML de la placa
    esphome_state_object_id: Optional[str] = "estado"


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
        # (binary_sensor `estado`: ON = despierto / OFF = dormido) en vez de
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

    def _on_esphome_state(self, value: Optional[bool]):
        # `estado` es un binary_sensor en el YAML de la placa, así que el valor
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

    def shutdown(self):
        """Para la sesión y espera a que los hilos terminen. Para usar al apagar
        el servicio o al borrar la cámara con DELETE.

        El presupuesto total está acotado a ~4s a propósito: lifespan solo da
        5s por sesión y uvicorn arranca con --timeout-graceful-shutdown 5. Los
        tres hilos son daemon, así que si alguno se pasa del plazo no impide
        que el proceso muera; solo lo dejamos dicho en el log.
        """
        self.stop(explicit=True)
        # Primero el controller: así deja de reintentar contra la placa y
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
                ya_parada = self._stop_event.is_set()
                self._stop_event.set()
                if not ya_parada:
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
            # El propio estado="on" ya llama a start() cuando toca;
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
                # IP muerta. Paramos; el siguiente `estado=on` nos rearranca
                # (ver la nota sobre repeticiones en _on_esphome_state).
                if self.esphome is not None and not self.esphome.is_connected:
                    print(f"[{self.cfg.camera_id}] stream caído ({_motivo(e)}) y API de ESPHome "
                          f"desconectada -> la placa parece dormida, dejo de reintentar")
                    break
                print(f"[{self.cfg.camera_id}] stream interrumpido ({_motivo(e)}), "
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

        def calentar():
            try:
                model.predict(np.zeros((640, 640, 3), dtype=np.uint8), imgsz=640, verbose=False)
            except Exception as e:
                print(f"[{self.cfg.camera_id}] keep-alive de GPU falló:", repr(e))

        ultimo_frame = time.monotonic()
        en_reposo = False

        while not stop_event.is_set():
            keepalive_on = (
                self.cfg.keepalive_enabled
                if self.cfg.keepalive_enabled is not None
                else GLOBAL_CONFIG.keepalive_enabled
            )
            limite = GLOBAL_CONFIG.keepalive_idle_limit_sec
            calentando = _toca_keepalive(is_cuda, keepalive_on,
                                         time.monotonic() - ultimo_frame, limite)
            # Si ya no toca calentar, esperar 1s en vez de 50ms: así el hilo
            # queda de verdad en reposo en lugar de girar a 20 Hz sin hacer nada.
            wait_timeout = GLOBAL_CONFIG.keepalive_interval_sec if calentando else 1.0

            try:
                frame = raw_queue.get(timeout=wait_timeout)
            except queue.Empty:
                if calentando:
                    calentar()
                elif is_cuda and keepalive_on and not en_reposo:
                    en_reposo = True
                    print(f"[{self.cfg.camera_id}] {limite:.0f}s sin frames -> "
                          f"keep-alive de GPU en pausa hasta que vuelva la cámara")
                if stop_event.is_set():
                    break
                continue

            if en_reposo:
                # Volvemos de un parón largo con la GPU ya bajada de P-state.
                # Una dummy antes de la inferencia real conserva la garantía del
                # workaround: la primera de verdad no sale corrupta.
                en_reposo = False
                print(f"[{self.cfg.camera_id}] vuelven los frames -> keep-alive de GPU reanudado")
                calentar()
            ultimo_frame = time.monotonic()

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
    idle_limit_sec: Optional[float] = None  # solo global; por cámara se ignora


@app.post("/config/keepalive")
async def set_global_keepalive(cfg: KeepaliveConfig):
    if cfg.enabled is not None:
        GLOBAL_CONFIG.keepalive_enabled = cfg.enabled
    if cfg.interval_sec is not None:
        GLOBAL_CONFIG.keepalive_interval_sec = cfg.interval_sec
    if cfg.idle_limit_sec is not None:
        GLOBAL_CONFIG.keepalive_idle_limit_sec = cfg.idle_limit_sec
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
    # session.shutdown() es bloqueante (hace join() sobre hilos, hasta ~4s).
    # Llamarla directamente desde este endpoint async congelaba el event loop
    # entero durante ese rato: todos los demás streams y peticiones se
    # quedaban parados mientras se borraba una cámara. Mismo motivo que en
    # lifespan().
    await asyncio.to_thread(session.shutdown)
    save_cameras_to_disk()
    return {"removed": camera_id}


@app.get("/cameras/{camera_id}/status")
async def camera_status(camera_id: str):
    return get_camera(camera_id).status()


@app.post("/cameras/{camera_id}/esphome/awake")
async def esphome_awake(camera_id: str):
    """La propia placa llama a esto (wifi.on_connect -> http_request.post)
    en cuanto tiene IP, para que nos saltemos la espera de reconexión de la
    API en vez de esperar pasivamente hasta el siguiente reintento."""
    session = get_camera(camera_id)
    if session.esphome is None:
        raise HTTPException(400, f"la cámara '{camera_id}' no tiene noise_psk configurado")
    session.esphome.notify_awake()
    return {"ok": True}


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
            # await, no time.sleep(): esto corre en el event loop y un sleep
            # síncrono congelaba todos los demás streams hasta 1s cada vez que
            # se pedía un snapshot de una cámara que aún no tiene frame.
            await asyncio.sleep(0.05)
            jpg = session.snapshot(infer)
    finally:
        session.remove_client(mode)
    if jpg is None:
        raise HTTPException(503, "Sin frame disponible todavía")
    return Response(content=jpg, media_type="image/jpeg")


# ---------------------------------------------------------------------------
# Endpoints de prueba puntual: una sola imagen (URL o fichero subido), sin
# sesión de cámara ni tracking. Útiles para depurar modelos/confianza a mano.
# ---------------------------------------------------------------------------

class ImageRequest(BaseModel):
    image_url: str
    confidence: float = 0.5


@app.post("/detect")
async def detect(req: ImageRequest):
    """Descarga una imagen por URL y devuelve las detecciones del modelo por defecto."""
    try:
        response = requests.get(req.image_url, timeout=5)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise HTTPException(400, f"No se pudo descargar la imagen: {e!r}")

    nparr = np.frombuffer(response.content, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return {"error": "No se pudo decodificar la imagen"}

    model = get_default_model()
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
    """Prueba con imagen local + elige modelo/confianza/resolución."""
    contents = await image.read()
    nparr = np.frombuffer(contents, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return {"error": "No se pudo decodificar la imagen"}

    model = get_model(model_name, DEFAULT_DEVICE)
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
    """Igual que /detect-file pero devuelve la imagen con las cajas dibujadas, para verlo directamente."""
    contents = await image.read()
    nparr = np.frombuffer(contents, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return {"error": "No se pudo decodificar la imagen"}

    model = get_model(model_name, DEFAULT_DEVICE)
    results = model.predict(img, conf=confidence, imgsz=imgsz, verbose=False)[0]
    annotated = results.plot()  # numpy array (BGR) con las cajas ya pintadas

    ok, buf = cv2.imencode(".jpg", annotated)
    if not ok:
        raise HTTPException(500, "No se pudo codificar la imagen anotada")
    return StreamingResponse(io.BytesIO(buf.tobytes()), media_type="image/jpeg")