"""Servicio de deteccion de objetos en tiempo real con YOLO y ESPHome.

Este modulo es solo la API HTTP. El pipeline de video esta en camera.py, la
conexion con las placas en esphome_api.py y el registro de camaras en
registro.py.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Optional

import cv2
import numpy as np
import torch
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from apagado import _install_shutdown_signal_hook
from camera import (
    DEFAULT_DEVICE,
    GLOBAL_CONFIG,
    CameraConfig,
    CameraSession,  # solo para la anotación de _shutdown_one
    get_model,
)
from log import print
from registro import (
    CAMERAS,
    _cameras_lock,
    get_camera,
    load_cameras_from_disk,
    register_camera,
    save_cameras_to_disk,
)
from servo_tracker import ServoConfig, ServoTracker


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
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "cameras": list(CAMERAS.keys())}


@app.get("/config")
async def get_config():
    return GLOBAL_CONFIG.as_dict()


@app.post("/config/keepalive")
async def set_global_keepalive(
    enabled: bool = Form(True, description="Mantener la GPU ocupada con inferencias mínimas entre frames. La GTX 1080 devuelve detecciones corruptas cuando el driver le baja el estado de energía, y esto lo evita. Por defecto activado."),
    interval_sec: float = Form(0.05, gt=0.0, description="Cada cuánto se lanza esa inferencia de relleno mientras no llega un frame real. Por defecto 0.05 s (20 por segundo)."),
    idle_limit_sec: float = Form(3.0, gt=0.0, description="Segundos sin recibir ningún frame tras los cuales se deja de calentar la GPU: si la cámara no da imagen, seguir ocupándola es gastar para nada. Por defecto 3 s."),
):
    """Keep-alive de GPU, global para todas las cámaras.

    Cada cámara puede desactivarlo solo para ella en
    `POST /cameras/{camera_id}/config/keepalive`, pero el intervalo y el
    tiempo de reposo son de aquí.
    """
    GLOBAL_CONFIG.keepalive_enabled = enabled
    GLOBAL_CONFIG.keepalive_interval_sec = interval_sec
    GLOBAL_CONFIG.keepalive_idle_limit_sec = idle_limit_sec
    return GLOBAL_CONFIG.as_dict()


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


def _validar_device(device: str) -> str:
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
    model_name: str = Form("yolo11m", description="Pesos YOLO a usar, sin el .pt. Se descargan solos la primera vez."),
    device: str = Form("cuda", description="Dónde corre la inferencia: 'cuda' o 'cpu'."),
    confidence: float = Form(0.5, ge=0.0, le=1.0, description="Confianza mínima para dar una detección por buena. Por defecto 0.5."),
    imgsz: int = Form(640, description="Lado al que YOLO reescala el frame antes de inferir. Más grande ve objetos más pequeños, pero cuesta más. Por defecto 640."),
    classes: Optional[str] = Form(None, description="IDs de clase COCO separados por comas a los que limitar la detección (0 = personas, 16 = pájaros). Vacío = todas."),
    default_infer: bool = Form(True, description="Qué devuelven /stream y /snapshot cuando no se pasa ?infer=: true = anotado, false = crudo."),
    always_infer: bool = Form(True, description="Correr YOLO aunque nadie mire el stream, para que la cámara siga detectando con el navegador cerrado. A false ahorra GPU en una cámara que solo sirva vídeo."),
    keepalive_enabled: Optional[bool] = Form(None, description="Override del keep-alive de GPU para esta cámara. Vacío = usar el global de /config/keepalive."),
    noise_psk: Optional[str] = Form(None, description="api.encryption.key del YAML de la placa. Si se rellena, la sesión abre además la API nativa de ESPHome y arranca/para siguiendo el sensor de estado."),
    esphome_state_object_id: str = Form("estado", description="object_id del sensor de la placa que dice si está despierta."),
):
    """Da de alta una cámara.

    Los servos **no** se configuran aquí: si esta placa lleva torreta, se
    montan después con `POST /cameras/{camera_id}/config/servo`, que necesita
    que la cámara ya exista y tenga `noise_psk`.
    """
    cfg = CameraConfig(
        camera_id=camera_id, stream_url=stream_url,
        model_name=model_name, device=_validar_device(device),
        confidence=confidence, imgsz=imgsz, classes=_parse_classes(classes),
        keepalive_enabled=keepalive_enabled,
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


@app.post("/cameras/{camera_id}/config/inference")
async def set_inference_config(
    camera_id: str,
    confidence: float = Form(0.5, ge=0.0, le=1.0, description="Confianza mínima para dar una detección por buena. Por defecto 0.5."),
    imgsz: int = Form(640, description="Lado al que YOLO reescala el frame antes de inferir. Más grande ve objetos más pequeños, pero cuesta más GPU. Por defecto 640."),
    always_infer: bool = Form(True, description="Correr YOLO aunque nadie mire el stream, para seguir detectando con el navegador cerrado."),
    classes: Optional[str] = Form(None, description="IDs de clase COCO separados por comas (0 = personas, 16 = pájaros). Vacío = todas las clases."),
    model_name: Optional[str] = Form(None, description="Cambiar los pesos YOLO, sin el .pt. VACÍO = dejar el que ya tiene. Cambiarlo relanza la sesión."),
    device: Optional[str] = Form(None, description="Cambiar dónde corre: 'cuda' o 'cpu'. VACÍO = dejar el que ya tiene. Cambiarlo relanza la sesión."),
):
    """Ajustes de la detección de una cámara.

    `confidence`, `imgsz`, `always_infer` y `classes` se releen en cada frame,
    así que el cambio se nota al instante y **se aplican siempre** con lo que
    traiga el formulario. `model_name` y `device`, en cambio, solo se resuelven
    al arrancar los hilos: se dejan vacíos si no se quieren tocar, y cuando se
    cambian la sesión se relanza sola.
    """
    session = get_camera(camera_id)

    # El modelo se carga aquí y no en el hilo de proceso para que un nombre
    # inventado salga como un 400 en vez de reventar la cámara por lo bajo. Va
    # en un hilo porque cargar y precalentar unos pesos tarda segundos.
    nuevo_modelo = (model_name or "").strip() or None
    nuevo_device = _validar_device(device) if (device or "").strip() else None
    relanzar = ((nuevo_modelo is not None and nuevo_modelo != session.cfg.model_name)
                or (nuevo_device is not None and nuevo_device != session.cfg.device))
    if relanzar:
        destino = nuevo_device or session.cfg.device
        try:
            await asyncio.to_thread(get_model, nuevo_modelo or session.cfg.model_name, destino)
        except Exception as e:
            raise HTTPException(400, f"no pude cargar el modelo: {e}")

    session.cfg.confidence = confidence
    session.cfg.imgsz = imgsz
    session.cfg.always_infer = always_infer
    session.cfg.classes = _parse_classes(classes)
    if nuevo_modelo is not None:
        session.cfg.model_name = nuevo_modelo
    if nuevo_device is not None:
        session.cfg.device = nuevo_device

    relanzada = await asyncio.to_thread(session.restart) if relanzar else False
    save_cameras_to_disk()
    return {"relanzada": relanzada, **session.status()}


@app.post("/cameras/{camera_id}/config/keepalive")
async def set_camera_keepalive(
    camera_id: str,
    enabled: Optional[bool] = Form(None, description="Override del keep-alive de GPU solo para esta cámara. Vacío = seguir el global. El intervalo y el tiempo de reposo son globales: se tocan en POST /config/keepalive."),
):
    """Activa o desactiva el keep-alive de GPU en una cámara concreta."""
    session = get_camera(camera_id)
    session.cfg.keepalive_enabled = enabled
    save_cameras_to_disk()
    return session.status()


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
    servicio = session.servo_tracker.cfg.service
    if session.esphome is not None and not session.esphome.tiene_servicio(servicio):
        # El caso que más despista: todo conectado, pero el firmware no expone
        # el servicio, así que la llamada no haría absolutamente nada.
        raise HTTPException(503, f"la placa de '{camera_id}' no publica el servicio "
                                 f"'{servicio}' (¿firmware sin servos?). Publica: "
                                 f"{', '.join(session.esphome.servicios) or 'ninguno'}")
    return session.servo_tracker


@app.post("/cameras/{camera_id}/servo")
async def move_servo(camera_id: str, pos: ServoPosition):
    """Control manual directo, para verificar el hardware sin depender de que
    haya detecciones. Salta el rate limit del seguimiento a propósito."""
    tracker = get_servo_tracker(camera_id)
    tracker.move_to(pos.pan, pos.tilt)
    return tracker.status()


@app.post("/cameras/{camera_id}/config/servo")
async def set_servo_config(
    camera_id: str,
    enabled: bool = Form(True, description="Seguimiento automático. A false la torreta se queda quieta pero sigue aceptando el control manual de POST /cameras/{camera_id}/servo."),
    service: str = Form("set_servo_position", description="Cómo se llama en el YAML de la placa el servicio (api: services:) que mueve la torreta. Solo hay que tocarlo si tu firmware lo nombra distinto."),
    gain: float = Form(0.25, gt=0.0, le=1.0, description="Fracción del error que se corrige en cada envío. Más alto = más rápido, pero con riesgo de pasarse del objetivo y oscilar. Por defecto 0.25."),
    deadzone: float = Form(0.06, ge=0.0, le=1.0, description="Error por debajo del cual se considera centrado y NO se mueve. Sin zona muerta el servo tiembla persiguiendo el ruido de la caja. Por defecto 0.06."),
    min_interval_sec: float = Form(0.08, gt=0.0, description="Tope de frecuencia de envío. Mandar una orden por frame satura la API de la placa sin ganar nada: el servo tarda más en llegar que el frame siguiente. Por defecto 0.08 s."),
    invert_pan: bool = Form(False, description="Invertir el sentido horizontal, según cómo haya quedado montado el servo."),
    invert_tilt: bool = Form(False, description="Invertir el sentido vertical."),
    lost_target_sec: float = Form(1.5, gt=0.0, description="Tiempo sin ver el objetivo antes de soltarlo y poder enganchar otro. Da margen para oclusiones de un par de frames. Por defecto 1.5 s."),
    home_pan: float = Form(0.0, ge=-1.0, le=1.0, description="Posición de reposo horizontal: -1 y 1 son los extremos, 0 el centro."),
    home_tilt: float = Form(0.0, ge=-1.0, le=1.0, description="Posición de reposo vertical."),
    return_home_on_lost: bool = Form(False, description="Al perder el objetivo, ¿volver a reposo? Por defecto no: suele interesar más quedarse mirando por donde se perdió, que es por donde reaparecerá."),
):
    """Configura la torreta pan/tilt de una cámara.

    Aquí es donde se le ponen servos a una cámara: al dar de alta no se
    piden, así que la primera llamada a este endpoint es la que monta el
    seguimiento. Necesita que la cámara tenga `noise_psk`, porque las órdenes
    van por la API nativa de la placa.
    """
    session = get_camera(camera_id)
    primera_vez = session.servo_tracker is None
    if not session.configurar_servo(ServoConfig(
        enabled=enabled, service=service, gain=gain, deadzone=deadzone,
        min_interval_sec=min_interval_sec,
        invert_pan=invert_pan, invert_tilt=invert_tilt,
        lost_target_sec=lost_target_sec,
        home_pan=home_pan, home_tilt=home_tilt,
        return_home_on_lost=return_home_on_lost,
    )):
        raise HTTPException(400, f"la cámara '{camera_id}' no tiene noise_psk, así que no "
                                 f"hay API de ESPHome por la que mover unos servos")
    if primera_vez:
        print(f"[{camera_id}] torreta configurada, seguimiento "
              f"{'activado' if enabled else 'desactivado'}")
    save_cameras_to_disk()
    return session.status()


@app.post("/cameras/{camera_id}/config/stream")
async def set_stream_default(
    camera_id: str,
    default_infer: bool = Form(True, description="Qué devuelven /stream y /snapshot cuando no se pasa ?infer= explícito: true = con las cajas dibujadas, false = vídeo crudo. Por defecto true."),
):
    """Los streams ya abiertos en modo "por defecto" cambian en caliente, sin
    tener que reconectar."""
    session = get_camera(camera_id)
    session.cfg.default_infer = default_infer
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
    # Lo que hubiera guardado ANTES de registrarnos. El pipeline solo codifica
    # para quien esté mirando, así que sin clientes esto puede llevar rancio
    # desde que se fue el último: hay que esperar a uno recién hecho. Cada
    # imencode devuelve un bytes nuevo, así que comparar por identidad es
    # exactamente "lo han vuelto a codificar desde que miré".
    viejo = session.snapshot(infer)
    session.add_client(mode)
    try:
        deadline = time.time() + 2.0
        jpg = None
        while time.time() < deadline:
            actual = session.snapshot(infer)
            if actual is not None and actual is not viejo:
                jpg = actual
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
        jpg = viejo
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

    salida = results.plot()  # numpy array (BGR) con las cajas ya pintadas

    # Mismo overlay que el stream de las cámaras. Aquí es además la única forma
    # de ver los ms y cuántas detecciones hubo, porque la respuesta es una
    # imagen y no hay JSON donde mirarlo. El modelo va delante porque este
    # endpoint existe justo para comparar modelos.
    alto, ancho = salida.shape[:2]
    # A diferencia del stream, que siempre es 640x480, aquí la imagen la sube
    # quien llama: puede ser una miniatura o una foto de 4000 px. Se usa la
    # misma escala que ultralytics para sus etiquetas (Annotator saca un grosor
    # del tamaño de la imagen y usa fontScale = grosor/3), o el overlay sale
    # ilegible al lado de las cajas que results.plot() acaba de pintar.
    lw = max(round((alto + ancho) / 2 * 0.003), 2)
    escala, grosor = lw / 3, max(lw - 1, 1)
    texto = (f"{model_name} | {inference_ms:.0f} ms ({DEFAULT_DEVICE}) | "
             f"{len(results.boxes)} detecciones")
    (tw, th), base = cv2.getTextSize(texto, cv2.FONT_HERSHEY_SIMPLEX, escala, grosor)
    # Fondo sólido: el color fijo del stream se pierde sobre una imagen clara.
    cv2.rectangle(salida, (0, 0), (tw + 2 * lw, th + base + 2 * lw), (0, 0, 0), -1)
    cv2.putText(salida, texto, (lw, th + lw), cv2.FONT_HERSHEY_SIMPLEX,
                escala, (255, 255, 0), grosor, cv2.LINE_AA)

    ok, buf = cv2.imencode(".jpg", salida)
    if not ok:
        raise HTTPException(500, "No se pudo codificar la imagen anotada")
    return Response(content=buf.tobytes(), media_type="image/jpeg")
