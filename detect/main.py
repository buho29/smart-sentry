"""Servicio de deteccion de objetos en tiempo real con YOLO y ESPHome.

Este modulo es solo la API HTTP. El pipeline de video esta en camera.py, la
conexion con las placas en esphome_api.py y el registro de camaras en
registry.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional

import cv2
import numpy as np
import requests
import torch
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from shutdown import _install_shutdown_signal_hook
from camera import (
    DEFAULT_DEVICE,
    DEFAULT_MODEL,
    GLOBAL_CONFIG,
    CameraConfig,
    CameraSession,  # solo para la anotación de _shutdown_one
    get_model,
    release_model,
)
from clips import (
    STORE,
    RetentionSweeper,
    load_recordings_config,
    save_recordings_config,
)
from encoders import encoder_capabilities
import gl_keeper  # gl_keeper
from log import print
from recorder import RecordingConfig
from registry import (
    CAMERAS,
    _cameras_lock,
    get_camera,
    load_cameras_from_disk,
    register_camera,
    save_cameras_to_disk,
)
from servo_tracker import ServoConfig, ServoTracker


# ---------------------------------------------------------------------------
# Log de accesos sin el sondeo de Home Assistant
# ---------------------------------------------------------------------------
# HA pide /status de cada cámara cada 10 s, la imagen fija (/snapshot, o
# /stream si la cámara MJPEG no tiene URL de imagen fija) cada ~10 s con un
# dashboard abierto, y /config y /recordings cada minuto. Eso son 6-8 líneas
# cada 10 s que tapan lo que importa. Se descartan solo los GET de esas rutas
# que han ido bien; los POST/DELETE, los errores y el resto de GET se siguen
# registrando. Va aquí y no como --no-access-log en la línea de comandos
# porque eso se llevaría también los POST.
_POLLING_PATH = re.compile(
    r"^/(cameras/[^/]+/(status|snapshot|stream)|config|recordings(/stats)?)$")


class _SkipPollingAccessLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn pasa (cliente, método, ruta con query, versión http, código)
        try:
            _, method, path, _, status = record.args
            if method == "GET" and int(status) < 400:
                return not _POLLING_PATH.match(path.split("?", 1)[0])
        except (TypeError, ValueError):
            pass
        return True


logging.getLogger("uvicorn.access").addFilter(_SkipPollingAccessLog())


# ---------------------------------------------------------------------------
# cuDNN
# ---------------------------------------------------------------------------
# PONLO A False SI TU GPU ES UNA GTX 10xx (Pascal): con cuDNN activo produce
# "CUDA misaligned address" de forma intermitente, y las detecciones salen
# erráticas sin que nada llegue a dar error.
#
# En cualquier otra tarjeta déjalo a True: desactivarlo solo cuesta rendimiento
# (medido aquí: 9,71 ms con cuDNN contra 11,49 sin él, y en arquitecturas más
# nuevas se espera peor, porque cuDNN aporta más).
#
# Vive aquí, y no en camera.py ni en una variable de entorno, porque es un
# ajuste que se toca una vez por máquina al instalar: a la vista en el fichero
# que uno abre. OJO si cambias esto: `test/barrido_modelos.py` no pasa por
# main.py y lleva su propia copia de esta línea.
CUDNN_ENABLED = False

torch.backends.cudnn.enabled = CUDNN_ENABLED
print(f"cuDNN {'activado' if CUDNN_ENABLED else 'desactivado'} "
      f"(CUDNN_ENABLED en main.py)"
      + (". Si ves 'CUDA misaligned address' o detecciones erráticas en una "
         "GTX 10xx (Pascal), ponlo a False" if CUDNN_ENABLED else ""))


# Hilo de retención de clips. Uno solo para todo el servicio: el tope en GB es
# del disco, no de cada cámara.
SWEEPER: Optional[RetentionSweeper] = None

# gl_keeper: ventana OpenGL oculta mientras haya cámaras en CUDA, para que la
# GTX 1080 no baje a P5 con yolo26n. Ver gl_keeper.py (y cómo quitarlo).
GL_KEEPER = gl_keeper.GlKeeper(  # gl_keeper
    lambda: any(s.is_running and s.cfg.device.startswith("cuda")  # gl_keeper
                for s in list(CAMERAS.values())))  # gl_keeper


@asynccontextmanager
async def lifespan(app: FastAPI):
    global SWEEPER
    # Antes del yield: uvicorn ya tiene puestos sus handlers de señal, así que
    # este es el momento de encadenarnos a ellos.
    _install_shutdown_signal_hook()
    # Antes que las cámaras: sus sesiones leen GLOBAL_CONFIG al arrancar.
    GLOBAL_CONFIG.load()
    STORE.cfg = load_recordings_config()
    # Rescatar los .part de un apagado sucio ANTES de barrer: si no, el barrido
    # los contaría como ficheros desconocidos y el clip del incidente que tumbó
    # el servicio sería justo el que se pierde.
    STORE.recover_orphan_parts()
    SWEEPER = RetentionSweeper(STORE)
    SWEEPER.start()
    load_cameras_from_disk()
    GL_KEEPER.start()  # gl_keeper
    yield
    print("Apagando: deteniendo todas las cámaras...")
    GL_KEEPER.stop()  # gl_keeper
    # Antes que las sesiones: el barrido no tiene que competir por el disco con
    # los clips que se están cerrando.
    if SWEEPER is not None:
        SWEEPER.stop(timeout=2.0)
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
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "cameras": list(CAMERAS.keys())}


# ---------------------------------------------------------------------------
# Servicio: parar / reiniciar uvicorn desde este mismo Swagger
# ---------------------------------------------------------------------------
#
# Este proceso no puede reiniciarse a sí mismo: lo hace supervisor.py, que
# vive aparte (:8081) y es quien lanza y mata a uvicorn. Estos endpoints solo
# reenvían allí, para no tener que cambiar de pestaña. Arrancar no tiene
# sentido aquí (si esto responde, ya está arrancado): eso va en el :8081 o
# desde Home Assistant.

SUPERVISOR_URL = f"http://127.0.0.1:{os.environ.get('DETECT_SUPERVISOR_PORT', '8081')}"


def _supervisor_status() -> dict:
    try:
        r = requests.get(f"{SUPERVISOR_URL}/service/status", timeout=2)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        raise HTTPException(503, f"supervisor no disponible en {SUPERVISOR_URL}: "
                                 f"arranca el servicio con `python supervisor.py` "
                                 f"para poder pararlo/reiniciarlo desde aquí")


def _supervisor_post_later(path: str):
    """Manda la orden al supervisor con un pequeño retraso, desde otro hilo,
    para que esta respuesta llegue al navegador antes de que uvicorn empiece a
    morir. Si lo llamásemos en línea, el supervisor bloquearía hasta que este
    proceso muriera, y este proceso no muere hasta responder: 5s de
    timeout-graceful-shutdown y un CancelledError por nada."""
    def fire():
        try:
            requests.post(f"{SUPERVISOR_URL}{path}", timeout=30)
        except requests.RequestException as e:
            print(f"supervisor: {path} falló: {e!r}")
    threading.Timer(0.3, fire).start()


@app.get("/service/status")
async def service_status():
    """Estado del proceso de uvicorn según el supervisor (:8081)."""
    return await asyncio.to_thread(_supervisor_status)


@app.post("/service/restart")
async def service_restart():
    """Reinicia uvicorn (parada ordenada + arranque). Las cámaras vuelven con
    lo que haya en cameras_config.json, manual_stop incluido. Tarda lo que
    tarde torch en cargar: mira /health hasta que responda."""
    await asyncio.to_thread(_supervisor_status)
    _supervisor_post_later("/service/restart")
    return {"ok": True, "note": "reiniciando; /health responderá cuando vuelva"}


@app.post("/service/shutdown")
async def service_shutdown():
    """Apaga uvicorn de forma ordenada y definitiva: el supervisor no lo relanza
    hasta un POST /service/start en el :8081 (o desde Home Assistant)."""
    await asyncio.to_thread(_supervisor_status)
    _supervisor_post_later("/service/stop")
    return {"ok": True, "note": f"apagando; para arrancar: POST {SUPERVISOR_URL}/service/start"}


@app.get("/config")
async def get_config():
    return {**GLOBAL_CONFIG.as_dict(),
            "gl_keeper": GL_KEEPER.status()}  # gl_keeper


# gl_keeper: todo este endpoint.
@app.post("/config/gl-keeper")
async def set_gl_keeper(
    enabled: bool = Form(..., description="Abrir una ventana OpenGL oculta mientras haya cámaras en CUDA, para que la GTX 1080 no baje a P5 con yolo26n (ahí deja de detectar). Solo funciona con Power Management = Prefer maximum performance en el perfil de python.exe (NVIDIA Profile Inspector). Con una sola cámara es más sencillo usar yolo26m y dejarlo apagado."),
):
    """Ventana OpenGL para mantener la GPU despierta. Global y **se persiste**.

    Viene apagado. Compruébalo con `nvidia-smi`: con cámaras activas la GPU
    tiene que quedarse en P0/P2 fijo y `gpu_idle` en Not Active. Ver
    docs/GPU.md.
    """
    GLOBAL_CONFIG.gl_keeper_enabled = enabled
    GLOBAL_CONFIG.save()
    if enabled:
        GL_KEEPER.retry()
    return {**GLOBAL_CONFIG.as_dict(), "gl_keeper": GL_KEEPER.status()}


@app.get("/cameras")
async def list_cameras():
    return [s.status() for s in CAMERAS.values()]


def _parse_classes(txt: Optional[str]) -> Optional[list[int]]:
    """`"0, 2"` -> `[0, 2]`. Vacío -> `None`, que significa "todas las clases".

    En un formulario una lista es incómoda, así que se escribe como texto
    separado por comas y se traduce aquí.
    """
    if txt is None or not txt.strip():
        return None
    try:
        return [int(p) for p in txt.replace(";", ",").split(",") if p.strip()]
    except ValueError:
        raise HTTPException(400, f"'classes' tiene que ser una lista de números "
                                 f"separados por comas (p.ej. '0,16'), no {txt!r}")


def _validate_device(device: str) -> str:
    device = device.strip().lower()
    if device.startswith("cuda") and not torch.cuda.is_available():
        raise HTTPException(400, "este equipo no tiene CUDA disponible; usa device='cpu'")
    if not (device == "cpu" or device.startswith("cuda")):
        raise HTTPException(400, f"device tiene que ser 'cuda' o 'cpu', no {device!r}")
    return device


@app.post("/cameras")
async def add_camera(
    camera_id: str = Form(..., description="Identificador único; es el que va en el resto de rutas."),
    stream_url: str = Form(..., description="URL del stream MJPEG del ESP32, p.ej. http://192.168.1.50:8080/"),
    model_name: str = Form(DEFAULT_MODEL, description="Pesos YOLO a usar, sin el .pt. Se descargan solos la primera vez. En la GTX 1080, con una sola cámara se recomienda yolo26m: con yolo26n la GPU baja a P5 y da detecciones corruptas."),
    device: str = Form("cuda", description="Dónde corre la inferencia: 'cuda' o 'cpu'."),
    confidence: float = Form(0.5, ge=0.0, le=1.0, description="Confianza mínima para dar una detección por buena. Por defecto 0.5."),
    imgsz: int = Form(640, description="Lado al que YOLO reescala el frame antes de inferir. Más grande ve objetos más pequeños, pero cuesta más. Por defecto 640."),
    classes: Optional[str] = Form(None, description="IDs de clase COCO separados por comas a los que limitar la detección (0 = personas, 16 = pájaros). Vacío = todas."),
    default_infer: bool = Form(True, description="Qué devuelven /stream y /snapshot cuando no se pasa ?infer=: true = anotado, false = crudo."),
    always_infer: bool = Form(True, description="Correr YOLO aunque nadie mire el stream, para que la cámara siga detectando con el navegador cerrado. A false ahorra GPU en una cámara que solo sirva vídeo."),
    noise_psk: Optional[str] = Form(None, description="api.encryption.key del YAML de la placa. Si se rellena, la sesión abre además la API nativa de ESPHome y arranca/para siguiendo el sensor de estado."),
    esphome_state_object_id: str = Form("awake", description="object_id del sensor de la placa que dice si está despierta."),
):
    """Da de alta una cámara.

    Los servos **no** se configuran aquí: si esta placa lleva torreta, se
    montan después con `POST /cameras/{camera_id}/config/servo`, que necesita
    que la cámara ya exista y tenga `noise_psk`.
    """
    cfg = CameraConfig(
        camera_id=camera_id, stream_url=stream_url,
        model_name=model_name, device=_validate_device(device),
        confidence=confidence, imgsz=imgsz, classes=_parse_classes(classes),
        default_infer=default_infer, always_infer=always_infer,
        noise_psk=noise_psk or None,
        esphome_state_object_id=esphome_state_object_id,
        servo=None,
    )
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
    # Suelta su modelo: ahora hay una instancia por cámara (para que no
    # compartan el tracker), así que no liberarla convertiría un alta/baja
    # repetida en una fuga de VRAM.
    release_model(session.cfg.model_name, session.cfg.device, camera_id)
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


@app.post("/cameras/start")
async def start_all_cameras():
    """Arranca a mano todas las cámaras registradas (como `/cameras/{id}/start`
    pero de golpe). Las que ya estaban en marcha no se tocan."""
    sessions = list(CAMERAS.values())
    for session in sessions:
        session.manual_start()
    save_cameras_to_disk()
    return [s.status() for s in sessions]


@app.post("/cameras/stop")
async def stop_all_cameras():
    """Para todas las cámaras registradas, aunque tengan clientes mirando, y
    las deja paradas hasta el siguiente start: ni el awake=on de la placa al
    despertar/reconectar las rearranca, ni un reinicio del servicio (se guarda
    en cameras_config.json). No bloquea: solo avisa a los hilos."""
    sessions = list(CAMERAS.values())
    for session in sessions:
        session.manual_stop()
    save_cameras_to_disk()
    return [s.status() for s in sessions]


@app.post("/cameras/{camera_id}/start")
async def start_camera(camera_id: str):
    """Arranca la cámara a mano y levanta la parada manual de POST /stop."""
    session = get_camera(camera_id)
    session.manual_start()
    save_cameras_to_disk()
    return session.status()


@app.post("/cameras/{camera_id}/stop")
async def stop_camera(camera_id: str):
    """Para la cámara y la deja parada hasta POST /start: ni el awake=on de la
    placa al despertar/reconectar ni un cliente nuevo la rearrancan, y la
    parada sobrevive a un reinicio del servicio (va a cameras_config.json)."""
    session = get_camera(camera_id)
    session.manual_stop()
    save_cameras_to_disk()
    return session.status()


class InferenceConfig(BaseModel):
    """Body de POST /cameras/{id}/config/inference. Todo opcional: lo que no
    venga se conserva. `classes: null` = todas las clases."""
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0, description="Confianza mínima para dar una detección por buena.")
    imgsz: Optional[int] = Field(None, gt=0, description="Lado al que YOLO reescala el frame antes de inferir. Más grande ve objetos más pequeños, pero cuesta más GPU.")
    always_infer: Optional[bool] = Field(None, description="Correr YOLO aunque nadie mire el stream, para seguir detectando con el navegador cerrado.")
    classes: Optional[list[int]] = Field(None, description="IDs de clase COCO (0 = personas, 16 = pájaros). null = todas las clases.")
    model_name: Optional[str] = Field(None, description="Pesos YOLO, sin el .pt. Cambiarlo relanza la sesión. En la GTX 1080, con una sola cámara se recomienda yolo26m: con yolo26n la GPU baja a P5 entre frames y da detecciones corruptas (se descartan y se cuentan en corrupt_frames).")
    device: Optional[str] = Field(None, description="'cuda' o 'cpu'. Cambiarlo relanza la sesión.")


@app.post("/cameras/{camera_id}/config/inference")
async def set_inference_config(camera_id: str, body: InferenceConfig):
    """Ajustes de la detección de una cámara.

    En Swagger, el desplegable **Examples** del body lista cada cámara
    registrada con sus valores actuales: elige la que vas a editar, toca lo
    que quieras y envía. Lo que no se envía se conserva, y reenviar valores
    iguales no cuesta nada.

    `confidence`, `imgsz`, `always_infer` y `classes` se releen en cada
    frame, así que el cambio se nota al instante. `model_name` y `device`, en cambio, solo se resuelven al
    arrancar los hilos: cuando cambian la sesión se relanza sola.
    """
    session = get_camera(camera_id)
    cfg = session.cfg

    changes = body.model_dump(exclude_unset=True)
    # Solo classes admite null; en el resto, null o vacío = no tocar.
    for k in ("confidence", "imgsz", "always_infer", "model_name", "device"):
        if k in changes:
            v = changes[k]
            if v is None or (isinstance(v, str) and not v.strip()):
                changes.pop(k)
    if "model_name" in changes:
        changes["model_name"] = changes["model_name"].strip()
    if "device" in changes:
        changes["device"] = _validate_device(changes["device"])
    # Quedarse solo con lo que de verdad cambia: el ejemplo de Swagger trae
    # la config entera y no hay que relanzar por reenviar el mismo modelo.
    changes = {k: v for k, v in changes.items() if getattr(cfg, k) != v}
    relaunch = bool(changes.keys() & {"model_name", "device"})

    if relaunch:
        # El modelo se carga aquí y no en el hilo de proceso para que un
        # nombre inventado salga como un 400 en vez de reventar la cámara por
        # lo bajo. Va en un hilo porque cargar y precalentar unos pesos tarda
        # segundos.
        try:
            await asyncio.to_thread(get_model,
                                    changes.get("model_name", cfg.model_name),
                                    changes.get("device", cfg.device))
        except Exception as e:
            raise HTTPException(400, f"no pude cargar el modelo: {e}")

    # Sustituir el objeto entero es seguro: los hilos leen self.cfg.<campo>
    # en cada frame, no guardan referencia al cfg viejo.
    session.cfg = cfg.model_copy(update=changes)
    relaunched = await asyncio.to_thread(session.restart) if relaunch else False
    if changes:
        save_cameras_to_disk()
    return {"relaunched": relaunched, **session.status()}


def _openapi_with_camera_examples():
    """Esquema OpenAPI regenerado en cada petición a /openapi.json, para que
    los bodies de /config/inference y /config/servo lleven como ejemplos los
    valores actuales de cada cámara registrada. Swagger los muestra en un
    desplegable, y así se edita partiendo de lo real en vez de los "string"/0
    que inventa por defecto. Basta recargar /docs tras cambiar algo.
    """
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    inference_fields = set(InferenceConfig.model_fields)
    # Ruta -> cómo sacar de una sesión el valor actual de su body.
    sources = {
        "/cameras/{camera_id}/config/inference":
            lambda s: s.cfg.model_dump(include=inference_fields),
        "/cameras/{camera_id}/config/servo":
            lambda s: (s.cfg.servo or ServoConfig()).model_dump(),
    }
    with _cameras_lock:
        sessions = list(CAMERAS.items())
    for path, current in sources.items():
        examples = {cid: {"summary": cid, "value": current(s)} for cid, s in sessions}
        if not examples:
            continue
        try:
            content = schema["paths"][path]["post"]["requestBody"]["content"]
            content["application/json"]["examples"] = examples
        except KeyError:
            pass
    return schema


app.openapi = _openapi_with_camera_examples



# ---------------------------------------------------------------------------
# Servos (variantes de placa con torreta pan/tilt)
# ---------------------------------------------------------------------------

class ServoPosition(BaseModel):
    # Mismo rango que espera `servo.write` en el YAML: -1 = un extremo,
    # 0 = reposo, 1 = el otro. Nada de grados en ninguna capa.
    pan: float = Field(..., ge=-1.0, le=1.0)
    tilt: float = Field(..., ge=-1.0, le=1.0)


def get_servo_tracker(camera_id: str) -> ServoTracker:
    """La torreta de una cámara, o el error que explique por qué no la hay."""
    session = get_camera(camera_id)
    if session.servo_tracker is None:
        raise HTTPException(400, f"la cámara '{camera_id}' no tiene servos configurados "
                                 f"(falta la sección 'servo' y/o noise_psk)")
    if session.esphome is not None and not session.esphome.is_connected:
        raise HTTPException(503, f"sin conexión con la placa de '{camera_id}'")
    service = session.servo_tracker.cfg.service
    if session.esphome is not None and not session.esphome.has_service(service):
        # El caso que más despista: todo conectado, pero el firmware no expone
        # el servicio, así que la llamada no haría absolutamente nada.
        raise HTTPException(503, f"la placa de '{camera_id}' no publica el servicio "
                                 f"'{service}' (¿firmware sin servos?). Publica: "
                                 f"{', '.join(session.esphome.services) or 'ninguno'}")
    return session.servo_tracker


@app.post("/cameras/{camera_id}/servo")
async def move_servo(camera_id: str, pos: ServoPosition):
    """Control manual directo, para verificar el hardware sin depender de que
    haya detecciones. Salta el rate limit del seguimiento a propósito, pero
    no los límites de recorrido: la orden se recorta a ±pan_limit/tilt_limit,
    y la respuesta trae la posición realmente enviada."""
    tracker = get_servo_tracker(camera_id)
    tracker.move_to(pos.pan, pos.tilt)
    return tracker.status()


class ServoConfigUpdate(BaseModel):
    """Body de POST /cameras/{id}/config/servo. Todo opcional: lo que no venga
    se conserva."""
    enabled: Optional[bool] = Field(None, description="Seguimiento automático. A false la torreta se queda quieta pero sigue aceptando el control manual de POST /cameras/{camera_id}/servo.")
    service: Optional[str] = Field(None, description="Cómo se llama en el YAML de la placa el servicio (api: services:) que mueve la torreta. Solo hay que tocarlo si tu firmware lo nombra distinto.")
    gain: Optional[float] = Field(None, gt=0.0, le=1.0, description="Fracción del error que se corrige en cada envío. Más alto = más rápido, pero con riesgo de pasarse del objetivo y oscilar. Por defecto 0.15.")
    deadzone: Optional[float] = Field(None, ge=0.0, le=1.0, description="Error por debajo del cual un eje se considera centrado y NO se mueve. Sin zona muerta el servo tiembla persiguiendo el ruido de la caja. Por defecto 0.08.")
    min_interval_sec: Optional[float] = Field(None, gt=0.0, description="Tiempo mínimo entre órdenes. En el modo por pasos (latency_sec=0) tiene que dar tiempo a que el servo llegue y la imagen lo refleje, o la torreta se pasa. Si tarda en reaccionar, bájalo un poco. Por defecto 0.6 s.")
    latency_sec: Optional[float] = Field(None, ge=0.0, le=2.0, description="Avanzado. 0 = modo por pasos, el estable: corrige desde la orden actual. Mayor que 0 = modo continuo: corrige desde donde estaba la torreta hace este tiempo y permite bajar mucho min_interval_sec, pero hay que acertar el retardo o oscila. Por defecto 0.")
    transition_sec: Optional[float] = Field(None, ge=0.0, le=5.0, description="Ajuste de la PLACA, no se guarda aquí: lo que tardan los servos en recorrer todo el rango (-1 a +1), o sea su suavidad. Se manda al number 'Servo transition' (lo mismo que cambiarlo en HA). 0 = salto directo.")
    auto_detach_sec: Optional[float] = Field(None, ge=0.0, le=30.0, description="Segundos quieto tras los que la placa corta el PWM del servo (menos zumbido y calor). Solo vive en la placa (number 'Servo auto detach'): no se guarda aquí. 0 = no soltar nunca.")
    invert_pan: Optional[bool] = Field(None, description="Invertir el sentido horizontal, según cómo haya quedado montado el servo.")
    invert_tilt: Optional[bool] = Field(None, description="Invertir el sentido vertical.")
    pan_gear_ratio: Optional[float] = Field(None, gt=0.0, description="Relación de engranajes horizontal: dientes del engranaje de la cámara / dientes del del servo. 1 = servo directo. Por defecto 1.")
    tilt_gear_ratio: Optional[float] = Field(None, gt=0.0, description="Relación de engranajes vertical: dientes del engranaje de la cámara / dientes del del servo. Con 21 en el servo y 63 en la cámara, 63/21 = 3. Dos engranajes engranados giran en sentidos opuestos, así que con reducción suele hacer falta cambiar también invert_tilt. Por defecto 1.")
    pan_limit: Optional[float] = Field(None, gt=0.0, le=1.0, description="Recorrido máximo horizontal, simétrico, en unidades de servo: ninguna orden (ni del seguimiento ni manual) pasa de ±pan_limit. Evita forzar el servo contra el tope mecánico. Por defecto 0.9.")
    tilt_limit: Optional[float] = Field(None, gt=0.0, le=1.0, description="Recorrido máximo vertical, simétrico, en unidades de servo. Por defecto 0.9.")
    lost_target_sec: Optional[float] = Field(None, gt=0.0, description="Tiempo sin ver el objetivo antes de soltarlo y poder enganchar otro. Da margen para oclusiones de un par de frames. Por defecto 1.5 s.")
    home_pan: Optional[float] = Field(None, ge=-1.0, le=1.0, description="Posición de reposo horizontal: -1 y 1 son los extremos, 0 el centro.")
    home_tilt: Optional[float] = Field(None, ge=-1.0, le=1.0, description="Posición de reposo vertical.")
    return_home_on_lost: Optional[bool] = Field(None, description="Al perder el objetivo, ¿volver a reposo? Por defecto no: suele interesar más quedarse mirando por donde se perdió, que es por donde reaparecerá.")
    lead_sec: Optional[float] = Field(None, ge=0.0, le=3.0, description="Anticipación horizontal en segundos: se apunta a donde estará el objetivo según su velocidad, así la torreta va por delante y queda más aire en la dirección en la que se mueve. Solo para objetivos que cruzan andando: con alguien cerca y quieto, su balanceo se toma por velocidad y oscila. 0 = sin anticipar. Por defecto 0.")


@app.post("/cameras/{camera_id}/config/servo")
async def set_servo_config(camera_id: str, body: ServoConfigUpdate):
    """Configura la torreta pan/tilt de una cámara.

    Aquí es donde se le ponen servos a una cámara: al dar de alta no se
    piden, así que la primera llamada a este endpoint es la que monta el
    seguimiento. Necesita que la cámara tenga `noise_psk`, porque las órdenes
    van por la API nativa de la placa.

    Lo que no se envía se conserva (la primera vez, se toma el valor por
    defecto). En Swagger, el desplegable **Examples** del body trae cada cámara
    con sus valores actuales: elige la tuya, toca lo que quieras y envía.
    """
    session = get_camera(camera_id)
    first_time = session.servo_tracker is None
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if isinstance(changes.get("service"), str):
        changes["service"] = changes["service"].strip() or ServoConfig().service
    # Ajustes que solo viven en la placa: no son parte de ServoConfig.
    transition = changes.pop("transition_sec", None)
    auto_detach = changes.pop("auto_detach_sec", None)
    base = session.cfg.servo or ServoConfig()
    cfg = base.model_copy(update=changes)
    if not session.configure_servo(cfg):
        raise HTTPException(400, f"la cámara '{camera_id}' no tiene noise_psk, así que no "
                                 f"hay API de ESPHome por la que mover unos servos")
    if first_time:
        print(f"[{camera_id}] torreta configurada, seguimiento "
              f"{'activado' if cfg.enabled else 'desactivado'}")
    save_cameras_to_disk()

    # Velocidad y auto-detach van también a la placa, que es la que los
    # aplica. `sent` dice si se pudieron mandar: False = placa desconectada o
    # firmware sin esos number (entonces hay que flashear el YAML nuevo).
    board = {}
    if transition is not None:
        board["servo_transition"] = session.esphome.set_number(
            "servo_transition", transition)
    if auto_detach is not None:
        board["servo_auto_detach"] = session.esphome.set_number(
            "servo_auto_detach", auto_detach)
    return {**session.status(), **({"board_sent": board} if board else {})}


# ---------------------------------------------------------------------------
# Grabación de clips
#
# Reparto de rutas: /config/recording es configuración (persiste), /record/* son
# acciones sobre la cámara, y /recordings/* son los ficheros ya grabados, que es
# lo que consume Home Assistant desde la otra máquina.
# ---------------------------------------------------------------------------

def get_recorder(camera_id: str):
    """El grabador de una cámara, o un 400 que explica cómo ponérselo."""
    session = get_camera(camera_id)
    if session.clip_recorder is None:
        raise HTTPException(400, f"la cámara '{camera_id}' no tiene grabación configurada: "
                                 f"llama antes a POST /cameras/{camera_id}/config/recording")
    return session.clip_recorder


@app.post("/cameras/{camera_id}/config/recording",
          summary="Configura la grabación de clips de una cámara")
async def set_recording_config(
    camera_id: str,
    enabled: bool = Form(True, description="Grabación activa. A false el coste vuelve a ser cero: no se guarda pre-roll ni se codifica ningún JPEG de más."),
    source: str = Form("annotated", description="'annotated' = vídeo con las cajas de YOLO pintadas; 'raw' = imagen limpia. OJO: 'annotated' ENCIENDE la inferencia en esta cámara aunque no haya nadie mirando el stream, con su coste de GPU. Si solo quieres vídeo, usa 'raw'."),
    trigger_on_detection: bool = Form(True, description="Abrir un clip solo cuando YOLO detecte algo. A false la cámara solo graba cuando se le manda a mano por /record/start."),
    min_hits: int = Form(2, ge=1, description="Frames CONSECUTIVOS con detección antes de abrir el clip. A 1, un falso positivo suelto (una hoja movida) ya genera un fichero. Por defecto 2."),
    pre_roll_sec: float = Form(5.0, ge=0.0, le=60.0, description="Segundos ANTERIORES al disparo que se incluyen en el clip. Es lo que hace que el vídeo empiece antes de que aparezca el bicho. Se guardan en memoria: 5 s a 15 fps son unos 3,4 MB a 640x480."),
    post_roll_sec: float = Form(8.0, gt=0.0, description="Segundos sin detecciones tras los cuales se cierra el clip. Si lo pones más corto que las pausas del bicho, un evento sale partido en diez ficheros. Por defecto 8 s."),
    max_clip_sec: float = Form(120.0, gt=0.0, description="Corte duro. Si el evento sigue, se cierra este clip y se abre otro: evita el fichero de 8 GB. Por defecto 120 s."),
    min_clip_sec: float = Form(2.0, ge=0.0, description="Los clips más cortos que esto se descartan al cerrarlos. Casi siempre son falsos positivos. Por defecto 2 s."),
    cooldown_sec: float = Form(3.0, ge=0.0, description="Tiempo muerto tras cerrar un clip antes de poder disparar otro. Una orden manual se lo salta."),
    fps: Optional[float] = Form(None, description="FPS del fichero MP4. Vacío = se mide del propio pipeline al abrir cada clip, que es lo recomendable."),
):
    """Pone o cambia la grabación de clips de una cámara.

    Aquí es donde se le pone grabación a una cámara: al darla de alta no se
    pide, así que la primera llamada a este endpoint es la que la monta. No
    necesita ningún hardware, solo disco.

    Dispara cualquier detección de la cámara: qué clases y con qué confianza
    se deciden en su `classes` y su `confidence`. La codificación (encoder,
    calidad, miniatura) es común a todas las cámaras y va en
    POST /recordings/config.

    Casi todo se aplica en caliente. La excepción es `source`, que lo lee el
    hilo escritor al abrir el fichero: si lo cambias a media grabación, surte
    efecto en el clip siguiente.

    Ejemplo típico para la huerta (vídeo limpio, eventos con pausas largas):
    `source=raw`, `pre_roll_sec=5`, `post_roll_sec=10`.
    """
    session = get_camera(camera_id)
    if source not in ("annotated", "raw"):
        raise HTTPException(422, "source debe ser 'annotated' o 'raw'")

    first_time = session.clip_recorder is None
    session.configure_recording(RecordingConfig(
        enabled=enabled, source=source,
        trigger_on_detection=trigger_on_detection, min_hits=min_hits,
        pre_roll_sec=pre_roll_sec, post_roll_sec=post_roll_sec,
        max_clip_sec=max_clip_sec, min_clip_sec=min_clip_sec,
        cooldown_sec=cooldown_sec, fps=fps,
    ))
    if first_time:
        print(f"[{camera_id}] grabación configurada ({source}, "
              f"{'por detección' if trigger_on_detection else 'solo manual'}, "
              f"{'activa' if enabled else 'desactivada'})")
    save_cameras_to_disk()
    return session.status()


@app.post("/cameras/{camera_id}/record/start",
          summary="Empieza a grabar un clip a mano")
async def record_start(
    camera_id: str,
    note: Optional[str] = Form(None, description="Texto libre que se guarda en los metadatos del clip, para acordarse de por qué se grabó."),
):
    """Arranca una grabación manual, con el pre-roll que hubiera acumulado.

    Es decir: si pulsas esto al oír algo, el clip ya empieza unos segundos
    ANTES de que pulsaras. Pensado para llamarlo desde Swagger o desde un
    `rest_command` de Home Assistant (por ejemplo, al dispararse el PIR).

    Espera a que llegue un frame antes de responder, así que si contesta, está
    grabando de verdad. Si había un clip abierto por detección, lo adopta en vez
    de partirlo en dos.
    """
    rec = get_recorder(camera_id)
    try:
        return await asyncio.to_thread(rec.start_manual, note)
    except TimeoutError as e:
        raise HTTPException(503, str(e))
    except OSError as e:
        raise HTTPException(507, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.post("/cameras/{camera_id}/record/stop",
          summary="Cierra el clip que se está grabando")
async def record_stop(camera_id: str):
    """Cierra el clip y devuelve sus metadatos ya escritos en disco.

    Si el clip resultó más corto que `min_clip_sec` se descarta, y entonces
    `clip` viene a null: no es un error, es que no valía la pena guardarlo.
    """
    rec = get_recorder(camera_id)
    try:
        res = await asyncio.to_thread(rec.stop_manual)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    # Las URLs se añaden aquí y no en el grabador, que no sabe nada de HTTP.
    # Sin esto, quien para una grabación desde una automatización de Home
    # Assistant tendría que volver a llamar a /recordings solo para componer el
    # enlace del clip que acaba de pedir.
    clip = res.get("clip")
    if clip:
        clip["url"] = f"/recordings/{clip['clip_id']}"
        clip["thumbnail_url"] = f"/recordings/{clip['clip_id']}/thumbnail"
    return res


@app.get("/cameras/{camera_id}/record/status",
         summary="Estado de la grabación de una cámara")
async def record_status(camera_id: str):
    """Atajo de lo que también sale en /cameras/{id}/status, bajo `consumers`.

    Lo que hay que mirar cuando algo no cuadra: `state`, `dropped_frames` (si
    sube, el disco no da abasto y el clip tendrá saltos) y `last_error` (por
    qué no se abrió o no se publicó un clip). El disco libre está en
    /recordings/stats.
    """
    return get_recorder(camera_id).status()


@app.post("/cameras/{camera_id}/config/stream")
async def set_stream_default(
    camera_id: str,
    default_infer: Optional[bool] = Form(None, description="Qué devuelven /stream y /snapshot cuando no se pasa ?infer= explícito: true = con las cajas dibujadas, false = vídeo crudo. Vacío = no tocar."),
    rotation: Optional[int] = Form(None, description="Giro de la imagen en grados, en sentido horario: 0, 90, 180 o 270. Para un módulo de cámara montado de lado. Se aplica antes de YOLO, así que las cajas, los clips y los servos trabajan ya sobre la imagen derecha. Vacío = no tocar."),
):
    """Ajustes de la imagen de una cámara. Lo que no se envía se conserva.

    `default_infer` cambia en caliente los streams ya abiertos en modo "por
    defecto", sin tener que reconectar. Cambiar `rotation` relanza la sesión
    para que el tracker empiece de cero: sus tracks estaban en las coordenadas
    del giro anterior.
    """
    session = get_camera(camera_id)
    if rotation is not None and rotation not in (0, 90, 180, 270):
        raise HTTPException(400, "rotation tiene que ser 0, 90, 180 o 270")
    if default_infer is not None:
        session.cfg.default_infer = default_infer
    relaunched = False
    if rotation is not None and rotation != session.cfg.rotation:
        session.cfg.rotation = rotation
        relaunched = await asyncio.to_thread(session.restart)
    save_cameras_to_disk()
    return {"relaunched": relaunched, **session.status()}


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
    # Lo que hubiera guardado ANTES de registrarnos. El pipeline solo codifica
    # para quien esté mirando, así que sin clientes esto puede llevar rancio
    # desde que se fue el último: hay que esperar a uno recién hecho. Cada
    # imencode devuelve un bytes nuevo, así que comparar por identidad es
    # exactamente "lo han vuelto a codificar desde que miré".
    old = session.snapshot(infer)
    session.add_client(mode)
    try:
        deadline = time.time() + 2.0
        jpg = None
        while time.time() < deadline:
            current = session.snapshot(infer)
            if current is not None and current is not old:
                jpg = current
                break
            # await, no time.sleep(): esto corre en el event loop y un sleep
            # síncrono congelaba todos los demás streams cada vez que se pedía
            # un snapshot de una cámara que aún no tiene frame.
            await asyncio.sleep(0.05)
    finally:
        session.remove_client(mode)
    if jpg is None:
        # No llegó ninguno nuevo a tiempo (cámara dormida o parada). Mejor
        # servir el último que se vio que un error, que es lo que hacía antes.
        jpg = old
    if jpg is None:
        raise HTTPException(503, "Sin frame disponible todavía")
    return Response(content=jpg, media_type="image/jpeg")


# ---------------------------------------------------------------------------
# Endpoints de prueba puntual: una sola imagen (URL o fichero subido), sin
# sesión de cámara ni tracking. Útiles para depurar modelos/confianza a mano.
# ---------------------------------------------------------------------------

@app.post(
    "/detect-file",
    responses={200: {"content": {"application/json": {}, "image/jpeg": {}}}},
)
async def detect_file(
    image: UploadFile = File(...),
    model_name: str = Form("yolo11n"),
    confidence: float = Form(0.5),
    imgsz: int = Form(640),
    annotated: bool = Form(False),
):
    """Prueba con una imagen local, eligiendo modelo/confianza/resolución.

    `annotated=false` devuelve JSON con las detecciones; `annotated=true`
    devuelve la imagen con las cajas pintadas, para mirarla directamente desde
    Swagger. Las dos salidas salen de la misma inferencia: lo único que cambia
    es cómo se empaqueta el resultado.
    """
    contents = await image.read()
    nparr = np.frombuffer(contents, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return {"error": "No se pudo decodificar la imagen"}

    model = get_model(model_name, DEFAULT_DEVICE)
    start = time.time()
    results = model.predict(img, conf=confidence, imgsz=imgsz, verbose=False)[0]
    inference_ms = round((time.time() - start) * 1000, 1)

    if not annotated:
        detections = []
        for box in results.boxes:
            x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
            detections.append({
                "label": model.names[int(box.cls)],
                "confidence": round(float(box.conf), 3),
                "box": {"x1": round(x1, 1), "y1": round(y1, 1), "x2": round(x2, 1), "y2": round(y2, 1)},
                "center": {"x": round((x1 + x2) / 2, 1), "y": round((y1 + y2) / 2, 1)},
            })
        return {"model": model_name, "detections": detections, "inference_ms": inference_ms}

    output = results.plot()  # numpy array (BGR) con las cajas ya pintadas

    # Mismo overlay que el stream de las cámaras. Aquí es además la única forma
    # de ver los ms y cuántas detecciones hubo, porque la respuesta es una
    # imagen y no hay JSON donde mirarlo. El modelo va delante porque este
    # endpoint existe justo para comparar modelos.
    height, width = output.shape[:2]
    # A diferencia del stream, cuyo tamaño fija la placa, aquí la imagen la sube
    # quien llama: puede ser una miniatura o una foto de 4000 px. Se usa la
    # misma escala que ultralytics para sus etiquetas (Annotator saca un grosor
    # del tamaño de la imagen y usa fontScale = grosor/3), o el overlay sale
    # ilegible al lado de las cajas que results.plot() acaba de pintar.
    lw = max(round((height + width) / 2 * 0.003), 2)
    scale, thickness = lw / 3, max(lw - 1, 1)
    text = (f"{model_name} | {inference_ms:.0f} ms ({DEFAULT_DEVICE}) | "
            f"{len(results.boxes)} detecciones")
    (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
    # Fondo sólido: el color fijo del stream se pierde sobre una imagen clara.
    cv2.rectangle(output, (0, 0), (tw + 2 * lw, th + base + 2 * lw), (0, 0, 0), -1)
    cv2.putText(output, text, (lw, th + lw), cv2.FONT_HERSHEY_SIMPLEX,
                scale, (255, 255, 0), thickness, cv2.LINE_AA)

    ok, buf = cv2.imencode(".jpg", output)
    if not ok:
        raise HTTPException(500, "No se pudo codificar la imagen anotada")
    return Response(content=buf.tobytes(), media_type="image/jpeg")


# ---------------------------------------------------------------------------
# Los clips ya grabados
#
# ORDEN IMPORTANTE: las rutas fijas (/stats, /config, /sweep, /capabilities) van
# ANTES que /recordings/{clip_id:path}, o el comodín se las traga y
# GET /recordings/stats se interpreta como "descárgame el clip llamado stats".
# El test test_recording_api.py lo comprueba, porque es un fallo silencioso.
# ---------------------------------------------------------------------------

def _parse_when(value: Optional[str], field: str) -> Optional[float]:
    """Acepta epoch ('1758300000') o ISO-8601 ('2026-09-20T18:00')."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        raise HTTPException(422, f"{field} no se entiende: usa epoch o ISO-8601 "
                                 f"(p.ej. 2026-09-20T18:00)")


@app.get("/recordings", summary="Lista los clips grabados")
async def list_recordings(
    camera_id: Optional[str] = Query(None, description="Solo los clips de esta cámara."),
    trigger: Optional[str] = Query(None, description="'detection' o 'manual'."),
    since: Optional[str] = Query(None, description="Desde cuándo. Epoch o ISO-8601 (2026-09-20T18:00)."),
    until: Optional[str] = Query(None, description="Hasta cuándo, mismo formato."),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    order: str = Query("desc", description="'desc' = más recientes primero (por defecto)."),
):
    """Los clips terminados, con sus metadatos y la URL para descargarlos.

    Esto es lo que consume Home Assistant desde la otra máquina. Un sensor REST
    sobre `/recordings?camera_id=huerta&limit=1` te da el último clip, y de ahí
    sale la `url` que puedes reproducir o mandar en una notificación.

    Los clips que se están grabando ahora mismo NO salen aquí: mientras se
    escriben son un fichero `.part` y nadie debe descargarse un MP4 a medias.
    """
    t_since = _parse_when(since, "since")
    t_until = _parse_when(until, "until")

    entries = STORE.list_clips(camera_id)
    if order == "asc":
        entries = list(reversed(entries))

    out = []
    for e in entries:
        meta = dict(e.meta)
        started = meta.get("started_at", e.mtime)
        if t_since is not None and started < t_since:
            continue
        if t_until is not None and started > t_until:
            continue
        if trigger and meta.get("trigger") != trigger:
            continue
        meta.setdefault("clip_id", e.clip_id)
        meta["bytes"] = e.size
        meta["url"] = f"/recordings/{e.clip_id}"
        meta["thumbnail_url"] = (f"/recordings/{e.clip_id}/thumbnail"
                                 if e.thumbnail.is_file() else None)
        out.append(meta)

    total = len(out)
    return {
        "total": total,
        "total_bytes": sum(m["bytes"] for m in out),
        "limit": limit,
        "offset": offset,
        "clips": out[offset:offset + limit],
    }


@app.get("/recordings/stats", summary="Cuánto ocupan los clips y cuánto disco queda")
async def recordings_stats():
    """Lo que hay que mirar antes de preguntarse por qué se han borrado clips."""
    st = STORE.stats()
    st["config"] = STORE.cfg.dict()
    st["last_sweep"] = SWEEPER.last_sweep if SWEEPER else None
    return st


@app.get("/recordings/capabilities",
         summary="Qué se puede codificar en esta máquina")
async def recordings_capabilities():
    """Si `h264` viene a false, los clips NO se verán en Home Assistant.

    Es el fallo más caro de diagnosticar de toda la grabación: el fichero pesa,
    VLC lo abre, y en el navegador se ve un reproductor en negro. La solución es
    `pip install imageio-ffmpeg` en el venv del servicio.
    """
    return encoder_capabilities()


@app.get("/recordings/config", summary="Ajustes de almacenamiento y retención")
async def get_recordings_config():
    return {**STORE.cfg.dict(), "root_dir_resolved": str(STORE.root)}


@app.post("/recordings/config", summary="Cambia el almacenamiento y la retención")
async def set_recordings_config(
    root_dir: Optional[str] = Form(None, description="Carpeta donde se guardan los clips. Relativa al servicio si no es absoluta. Cambiarla NO mueve lo ya grabado."),
    max_age_days: Optional[float] = Form(None, ge=0, description="Los clips más viejos que esto se borran. 0 = sin límite por edad."),
    max_total_gb: Optional[float] = Form(None, ge=0, description="Tope de ocupación de la carpeta entera; al pasarse se borran los más antiguos. 0 = sin límite por tamaño."),
    sweep_interval_sec: Optional[float] = Form(None, ge=30, description="Cada cuánto corre el barrido. Además se barre al arrancar y tras cerrar cada clip."),
    min_free_gb: Optional[float] = Form(None, ge=0, description="Por debajo de este hueco libre NO se abren clips nuevos, para no tumbar el disco donde también corre YOLO."),
    encoder: Optional[str] = Form(None, description="'auto' (ffmpeg si lo hay, si no OpenCV), 'ffmpeg' o 'opencv'. Mira GET /recordings/capabilities para ver qué hay disponible."),
    fourcc: Optional[str] = Form(None, description="Solo para el encoder 'opencv'. 'mp4v' produce ficheros que VLC abre pero que NO se reproducen en el navegador ni en Home Assistant."),
    ffmpeg_path: Optional[str] = Form(None, description="Ruta a un ffmpeg concreto. Vacío = se busca en el PATH y luego el de imageio-ffmpeg."),
    crf: Optional[int] = Form(None, ge=0, le=51, description="Calidad de x264: más bajo = mejor imagen y fichero más gordo. 18 es casi sin pérdidas, 28 es pequeño y basto. Por defecto 23."),
    preset: Optional[str] = Form(None, description="Preset de x264: cuánta CPU se gasta en comprimir mejor. 'ultrafast' a 'veryslow'. Por defecto 'veryfast', que deja la CPU para YOLO."),
    fps_min: Optional[float] = Form(None, gt=0.0, description="FPS mínimo con el que se abre un clip cuando la cámara no fija `fps` y se usa el medido. Por defecto 4."),
    fps_max: Optional[float] = Form(None, gt=0.0, description="FPS máximo con el que se abre un clip cuando se usa el medido. Por defecto 30."),
    save_thumbnail: Optional[bool] = Form(None, description="Guardar junto al clip el frame que lo disparó, como JPEG. Es gratis (ya está codificado) y le da a Home Assistant una imagen sin abrir el vídeo."),
    queue_maxsize: Optional[int] = Form(None, ge=8, description="Frames en vuelo hacia el disco, por cámara. Si se llena se descartan frames NUEVOS en vez de frenar el pipeline de vídeo. 120 son unos 8 s a 15 fps. Se aplica a los grabadores que se creen después (reinicio)."),
    preroll_max_mb: Optional[float] = Form(None, gt=0.0, description="Tope de memoria del pre-roll de cada cámara, por si la resolución sube y los mismos segundos ocupan diez veces más."),
):
    """Los campos que no mandes se quedan como estaban.

    Almacenamiento, retención y codificación son de la máquina, no de cada
    cámara: valen para todas. `encoder` y `fourcc` los lee el hilo escritor al
    abrir el fichero, así que un cambio a media grabación surte efecto en el
    clip siguiente.

    Aviso: cambiar `root_dir` no mueve los clips que ya hay, y la retención deja
    de vigilar la carpeta antigua. La respuesta trae `warning` cuando pasa.
    """
    if encoder is not None and encoder not in ("auto", "ffmpeg", "opencv"):
        raise HTTPException(422, "encoder debe ser 'auto', 'ffmpeg' u 'opencv'")
    cfg = STORE.cfg
    antiguo = str(STORE.root)
    for campo, valor in (("root_dir", root_dir), ("max_age_days", max_age_days),
                         ("max_total_gb", max_total_gb),
                         ("sweep_interval_sec", sweep_interval_sec),
                         ("min_free_gb", min_free_gb),
                         ("encoder", encoder), ("fourcc", fourcc),
                         ("ffmpeg_path", ffmpeg_path), ("crf", crf),
                         ("preset", preset), ("fps_min", fps_min),
                         ("fps_max", fps_max), ("save_thumbnail", save_thumbnail),
                         ("queue_maxsize", queue_maxsize),
                         ("preroll_max_mb", preroll_max_mb)):
        if valor is not None:
            setattr(cfg, campo, valor)
    save_recordings_config(cfg)
    out = {**cfg.dict(), "root_dir_resolved": str(STORE.root)}
    if root_dir is not None and str(STORE.root) != antiguo:
        out["warning"] = (f"los clips que ya había en {antiguo} siguen ahí y la "
                          f"retención ya no los vigila: muévelos o bórralos a mano")
    return out


@app.post("/recordings/sweep", summary="Aplica la retención ahora mismo")
async def sweep_recordings(
    dry_run: bool = Form(False, description="A true no borra nada, solo dice qué borraría. Úsalo antes de bajar max_age_days o max_total_gb."),
):
    """Fuerza el barrido sin esperar al hilo de retención."""
    return await asyncio.to_thread(STORE.sweep, dry_run)


@app.get("/recordings/{clip_id:path}/thumbnail",
         summary="La imagen que disparó un clip")
async def recording_thumbnail(clip_id: str):
    try:
        clip = STORE.resolve(clip_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    thumb = clip.with_suffix(".jpg")
    if not thumb.is_file():
        raise HTTPException(404, "ese clip se grabó sin miniatura (save_thumbnail=false)")
    return FileResponse(thumb, media_type="image/jpeg")


@app.get("/recordings/{clip_id:path}", summary="Descarga o reproduce un clip")
async def get_recording(clip_id: str):
    """Sirve el MP4. Admite cabeceras Range, que es lo que permite hacer seek.

    Sin Range, el reproductor de Home Assistant tendría que descargarse el clip
    entero antes de poder saltar a un minuto concreto. Lo aporta Starlette, no
    hay que hacer nada.
    """
    try:
        clip = STORE.resolve(clip_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return FileResponse(clip, media_type="video/mp4", filename=clip.name)


@app.delete("/recordings/{clip_id:path}", summary="Borra un clip")
async def delete_recording(clip_id: str):
    """Borra el MP4 con su sidecar y su miniatura: los tres o ninguno."""
    if clip_id in STORE.in_progress():
        raise HTTPException(409, "ese clip se está grabando ahora mismo; "
                                 "páralo antes con POST /cameras/{id}/record/stop")
    try:
        clip = STORE.resolve(clip_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    from clips import ClipEntry
    st = clip.stat()
    freed = STORE.delete(ClipEntry(clip_id=clip_id, path=clip, size=st.st_size,
                                   mtime=st.st_mtime))
    return {"deleted": clip_id, "freed_bytes": freed}
