"""Comprueba que todo se cierra bien, sin necesidad de tener el ESP32 delante.

Cubre lo que pasa cuando la placa se duerme: parar la sesión con el stream
abierto, cerrar el socket de la API nativa, y que ningún hilo se quede vivo ni
muera con un traceback.

    cd detect
    venv\\Scripts\\python.exe test\\test_cierre.py

192.0.2.1 es TEST-NET-1 (RFC 5737): no enruta a ningún sitio, así que sirve
para simular una placa dormida o fuera de cobertura.
"""

import http.server
import signal
import socketserver
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

fallos: list[str] = []
tracebacks_de_hilos: list[str] = []


def _hook(args):
    tracebacks_de_hilos.append(
        f"{args.thread.name}: "
        + "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    )


threading.excepthook = _hook

import numpy as np  # noqa: E402
import apagado  # noqa: E402
import camera  # noqa: E402
import esphome_api  # noqa: E402

DORMIDA = "192.0.2.1"


def check(nombre, cond, extra=""):
    print(f"  {'PASS ' if cond else 'FALLO'}  {nombre} {extra}")
    if not cond:
        fallos.append(nombre)


def servidor_mjpeg_local():
    """Servidor MJPEG de mentira que emite sin parar, para tener una conexión
    de verdad viva contra la que probar stop().

    Manda un JPEG **de verdad**: con bytes basura, cv2.imdecode devuelve None,
    el lector descarta el frame y _process_loop no publica nada, así que los
    generadores nunca reciben el notify_all() y solo despiertan por timeout.
    """
    import cv2
    import numpy as np

    ok, buf = cv2.imencode(".jpg", np.zeros((48, 64, 3), dtype=np.uint8))
    assert ok, "no se pudo generar el JPEG de prueba"
    jpg = buf.tobytes()
    parte = (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
             + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(parte)
                    self.wfile.flush()
                    time.sleep(0.2)
            except Exception:
                pass

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


print("\n=== 1. EsphomeController: shutdown durante el intento de conexión ===")
c = esphome_api.EsphomeController(address=DORMIDA, noise_psk=None, watch_entity_object_id="estado")
time.sleep(0.2)  # le pillamos con el connect() en vuelo
t0 = time.perf_counter()
c.shutdown()
dt = time.perf_counter() - t0
check("hilo terminado", not c._thread.is_alive())
check("loop cerrado", c._loop.is_closed())
check("_closed marcado", c._closed.is_set())
check("tarda < 2s", dt < 2.0, f"({dt:.2f}s)")

print("\n=== 2. EsphomeController: shutdown durante la espera de safety_retry ===")
c2 = esphome_api.EsphomeController(
    address=DORMIDA, noise_psk=None, watch_entity_object_id="estado",
    connect_attempt_timeout_sec=0.3, safety_retry_sec=30.0,
)
time.sleep(1.5)  # ya falló el connect y está esperando los 30s
t0 = time.perf_counter()
c2.shutdown()
dt = time.perf_counter() - t0
check("hilo terminado", not c2._thread.is_alive())
check("loop cerrado", c2._loop.is_closed())
check("no espera los 30s de safety_retry", dt < 2.0, f"({dt:.2f}s)")

print("\n=== 3. shutdown() idempotente; notify_awake()/llamar_servicio() después no revientan ===")
try:
    c2.shutdown()
    c2.notify_awake()
    c2.llamar_servicio("lo_que_sea", pan=0.0, tilt=0.0)
    check("segunda llamada sin excepción", True)
except Exception as e:
    check("segunda llamada sin excepción", False, repr(e))

print("\n=== 3a. llamar_servicio() con un servicio que la placa no publica ===")
# Es el camino que usará cualquier variante nueva (el relé de la pistola) para
# saber si el firmware la soporta: tiene que devolver False sin lanzar, no
# fallar en silencio ni reventar el hilo que lo llame.
c3 = esphome_api.EsphomeController(address=DORMIDA, noise_psk=None,
                                   connect_attempt_timeout_sec=0.3)
try:
    check("sin conectar, tiene_servicio() dice que no",
          c3.tiene_servicio("set_servo_position") is False)
    check("y no publica ninguno", c3.servicios == (), f"({c3.servicios})")
    check("llamar_servicio() devuelve False en vez de lanzar",
          c3.llamar_servicio("disparar") is False)
    check("aunque lleve argumentos",
          c3.llamar_servicio("set_servo_position", pan=0.5, tilt=-0.5) is False)
finally:
    c3.shutdown()
check("y sigue devolviendo False después del shutdown",
      c3.llamar_servicio("disparar") is False)

print("\n=== 3b. El lector, al rendirse, para la sesión entera ===")
# Regresión de la GPU al 54% para siempre: el lector se rendía ("la placa
# parece dormida, dejo de reintentar"), moría solo ese hilo, y el de proceso
# seguía vivo con la cola vacía lanzando inferencias dummy de keep-alive cada
# 50 ms indefinidamente. Aquí nadie llama a stop(): tiene que pararse sola.
cfg_rendir = camera.CameraConfig(
    camera_id="rendirse", stream_url=f"http://{DORMIDA}:8080/",
    device="cpu", model_name="yolo11n",
    noise_psk="JzwVvMMnr1H0kAtYHDnWKfZ/7L0RYVqOeGjWxwpIzRc=",
)
s_r = camera.CameraSession(cfg_rendir)
s_r.start(explicit=True)
check("arranca con explicit_start", s_r.explicit_start and s_r.is_running)
# El EsphomeController nunca conecta contra TEST-NET-1, así que en cuanto
# falle el stream el lector entra por el break de "la placa parece dormida".
espera = time.monotonic() + 30
while time.monotonic() < espera and s_r.is_running:
    time.sleep(0.5)
check("la sesión se para sola, sin que nadie llame a stop()", not s_r.is_running)
check("_stop_event marcado", s_r._stop_event.is_set())
check("hilo de proceso muerto (era el que quemaba la GPU)",
      not s_r._processing_thread.is_alive())
check("explicit_start limpiado, para que un estado=on rearranque",
      not s_r.explicit_start)
s_r.shutdown()

print("\n=== 3bis. El parser multipart del stream del ESP32 ===")
# Antes vivía incrustado en _read_loop y solo se probaba de refilón, con un
# servidor falso que manda partes perfectas de un solo chunk. Justo los casos
# que motivaron escribirlo por Content-Length no se probaban nunca.


def parte(cuerpo: bytes, con_longitud: bool = True) -> bytes:
    cab = b"--frame\r\nContent-Type: image/jpeg\r\n"
    if con_longitud:
        cab += b"Content-Length: " + str(len(cuerpo)).encode() + b"\r\n"
    return cab + b"\r\n" + cuerpo + b"\r\n"


def trocear(datos: bytes, n: int) -> list[bytes]:
    return [datos[i:i + n] for i in range(0, len(datos), n)]


A, B = b"PRIMERO-jpeg", b"SEGUNDO"
flujo = parte(A) + parte(B)

check("dos partes en un solo chunk",
      list(camera._iter_jpegs([flujo])) == [A, B])
check("cabecera y cuerpo partidos byte a byte",
      list(camera._iter_jpegs(trocear(flujo, 1))) == [A, B])
check("troceado en 3 bytes", list(camera._iter_jpegs(trocear(flujo, 3))) == [A, B])

# El cuelgue original: los marcadores JPEG dentro de los datos comprimidos.
# Buscándolos a mano el stream se desincronizaba; por Content-Length da igual.
sucio = b"\xff\xd8" + b"ruido\xff\xd9mas\xff\xd8ruido" + b"\xff\xd9"
check("un JPEG con \\xff\\xd8/\\xff\\xd9 dentro sale entero",
      list(camera._iter_jpegs([parte(sucio)])) == [sucio])
check("y también troceado", list(camera._iter_jpegs(trocear(parte(sucio), 2))) == [sucio])

# Una parte sin Content-Length (p.ej. la línea de boundary suelta) no debe
# romper el flujo: se ignora y se resincroniza con la siguiente cabecera.
check("parte sin Content-Length -> se salta, no rompe",
      list(camera._iter_jpegs([b"--frame\r\n\r\n" + parte(A)])) == [A])

check("sin datos no devuelve nada", list(camera._iter_jpegs([])) == [])
check("basura sin cabecera no inventa frames",
      list(camera._iter_jpegs([b"no hay nada que parsear aqui"])) == [])

# Salvavidas del buffer: un cuerpo que nunca llega a completarse no puede
# hacer crecer la memoria sin fin.
enorme = b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 999999999\r\n\r\n"
salida = list(camera._iter_jpegs([enorme] + [b"x" * 100_000] * 12, max_buffer=500_000))
check("un cuerpo imposible no cuelga ni devuelve basura", salida == [])

print("\n=== 3c. El keep-alive de GPU caduca sin frames y sin inferencia ===")
# (is_cuda, keepalive_activo, segundos_sin_frame, limite, hay_inferencia)
for nombre, args, esperado in [
    ("recién llegado un frame -> calienta", (True, True, 0.5, 3.0, True), True),
    ("justo antes del límite -> calienta", (True, True, 2.9, 3.0, True), True),
    ("pasado el límite -> NO calienta", (True, True, 3.1, 3.0, True), False),
    ("mucho después -> NO calienta", (True, True, 600.0, 3.0, True), False),
    ("en CPU -> nunca calienta", (False, True, 0.1, 3.0, True), False),
    ("keepalive desactivado -> nunca", (True, False, 0.1, 3.0, True), False),
    # Lo que arregla el 10% de GPU en reposo: sin inferencia que proteger, no
    # tiene sentido tener la GPU ocupada por muy recientes que sean los frames.
    ("sin inferencia -> NO calienta", (True, True, 0.1, 3.0, False), False),
    ("sin inferencia, frame recién llegado -> NO", (True, True, 0.0, 3.0, False), False),
]:
    check(nombre, camera._toca_keepalive(*args) is esperado)

print("\n=== 3d. always_infer: detecta sin nadie mirando el stream ===")
# Lo pedido: la cámara tiene que seguir detectando con el navegador cerrado.
# Ni un solo cliente ni consumidores en toda esta prueba.
srv_ai, puerto_ai = servidor_mjpeg_local()


def infiere_sola(always_infer: bool, segundos: float = 6.0) -> bool:
    cfg_ai = camera.CameraConfig(
        camera_id=f"ai-{always_infer}", stream_url=f"http://127.0.0.1:{puerto_ai}/",
        device="cpu", model_name="yolo11n", always_infer=always_infer,
    )
    s_ai = camera.CameraSession(cfg_ai)
    s_ai.start(explicit=True)
    try:
        assert s_ai.client_count == 0, "el test no debe tener clientes"
        fin = time.monotonic() + segundos
        while time.monotonic() < fin:
            if s_ai.last_inference_ms is not None:
                return True
            time.sleep(0.2)
        return False
    finally:
        s_ai.shutdown()


check("con always_infer=True infiere sin clientes", infiere_sola(True))
check("con always_infer=False no infiere sin clientes", not infiere_sola(False))
srv_ai.shutdown()

print("\n=== 3d-bis. Sin clientes no se codifica JPEG, pero /snapshot sigue fresco ===")
# Con always_infer y nadie mirando, antes se codificaba un JPEG por frame que no
# leía nadie (~15/s por cámara). Ahora solo se codifica para quien mire; el
# precio es que _latest_raw_jpeg se queda rancio, así que /snapshot tiene que
# esperar a uno nuevo en vez de devolver el último guardado.
import cv2 as _cv2  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import main as _main  # noqa: E402
import registro as _registro  # noqa: E402

srv_e, puerto_e = servidor_mjpeg_local()
_encodes = [0]
_orig_imencode = camera.cv2.imencode


def _contado(*a, **k):
    _encodes[0] += 1
    return _orig_imencode(*a, **k)


camera.cv2.imencode = _contado
try:
    cfg_e = camera.CameraConfig(
        camera_id="encodes", stream_url=f"http://127.0.0.1:{puerto_e}/",
        device="cpu", model_name="yolo11n",
    )
    s_e = camera.CameraSession(cfg_e)
    _registro.CAMERAS["encodes"] = s_e
    s_e.start(explicit=True)
    time.sleep(3)
    assert s_e.client_count == 0

    _encodes[0] = 0
    time.sleep(2)
    check("sin ningún cliente no se codifica nada", _encodes[0] == 0, f"({_encodes[0]})")

    antes_de_pedir = _encodes[0]
    r_snap = TestClient(_main.app).get("/cameras/encodes/snapshot?infer=false")
    check("pero /snapshot responde 200", r_snap.status_code == 200)
    check("y fuerza un frame recién codificado, no uno rancio",
          _encodes[0] > antes_de_pedir, f"({_encodes[0] - antes_de_pedir} imencode)")
    check("que decodifica a una imagen de verdad",
          _cv2.imdecode(np.frombuffer(r_snap.content, np.uint8), _cv2.IMREAD_COLOR) is not None)
    s_e.shutdown()
finally:
    camera.cv2.imencode = _orig_imencode
    _registro.CAMERAS.pop("encodes", None)
    srv_e.shutdown()

print("\n=== 3e. restart() relanza los hilos (cambiar modelo/device en caliente) ===")
# _process_loop resuelve el modelo y el device UNA vez, en su primera línea, así
# que cambiarlos en cfg no hace nada hasta que hay hilos nuevos. Lo delicado es
# que start() mira is_running: si no se espera a que muera la generación vieja,
# se cree que ya está todo en marcha y no arranca nada.
srv_r, puerto_r = servidor_mjpeg_local()
cfg_r = camera.CameraConfig(
    camera_id="restart", stream_url=f"http://127.0.0.1:{puerto_r}/",
    device="cpu", model_name="yolo11n",
)
s_rst = camera.CameraSession(cfg_r)
check("restart() sin sesión arrancada no hace nada", s_rst.restart() is False)
s_rst.start(explicit=True)
time.sleep(3)
viejo_lector, viejo_proceso = s_rst._reader_thread, s_rst._processing_thread
check("arranca e infiere", s_rst.is_running and s_rst.last_inference_ms is not None)

s_rst.last_inference_ms = None
s_rst.cfg.model_name = "yolo11m"
check("restart() con la sesión viva devuelve True", s_rst.restart() is True)
check("los hilos son otros", s_rst._reader_thread is not viejo_lector
      and s_rst._processing_thread is not viejo_proceso)
check("y los viejos están muertos",
      not viejo_lector.is_alive() and not viejo_proceso.is_alive())
check("explicit_start se conserva", s_rst.explicit_start)
time.sleep(4)
check("vuelve a inferir con el modelo nuevo", s_rst.last_inference_ms is not None)
s_rst.shutdown()
srv_r.shutdown()

print("\n=== 4. CameraSession completa (stream y placa inalcanzables) ===")
cfg = camera.CameraConfig(
    camera_id="test", stream_url=f"http://{DORMIDA}:8080/",
    device="cpu", model_name="yolo11n",
    noise_psk="JzwVvMMnr1H0kAtYHDnWKfZ/7L0RYVqOeGjWxwpIzRc=",
)
s = camera.CameraSession(cfg)
check("EsphomeController creado a partir del host de stream_url", s.esphome is not None)
s.start(explicit=True)
time.sleep(1.0)
t0 = time.perf_counter()
s.shutdown()
dt = time.perf_counter() - t0
s._reader_thread.join(timeout=5)  # puede estar dentro del connect timeout de 3s
check("hilo lector terminado", not s._reader_thread.is_alive())
check("hilo de proceso terminado", not s._processing_thread.is_alive())
check("hilo de esphome terminado", not s.esphome._thread.is_alive())
check("loop de esphome cerrado", s.esphome._loop.is_closed())
check("dentro del presupuesto de 5s de lifespan", dt < 5.0, f"({dt:.2f}s)")

print("\n=== 5. stop() con el lector conectado de verdad (la placa se duerme) ===")
srv, puerto = servidor_mjpeg_local()
cfg2 = camera.CameraConfig(
    camera_id="local", stream_url=f"http://127.0.0.1:{puerto}/",
    device="cpu", model_name="yolo11n",
)
s2 = camera.CameraSession(cfg2)
s2.start(explicit=True)
time.sleep(1.0)
check("lector corriendo", s2.is_running)
t0 = time.perf_counter()
s2.shutdown()
dt = time.perf_counter() - t0
check("cierra la conexión viva y termina", not s2.is_running, f"({dt:.2f}s)")
# Regresión: resp.close() a secas tardaba ~12s porque en Windows no interrumpe
# el recv() en curso del hilo lector. _force_close_response hace shutdown() del
# socket primero y baja el cierre a ~0.01s. stop() lo llama el loop de ESPHome,
# así que ese bloqueo congelaba la API entera.
check("cierre inmediato, sin bloquear a quien lo pide (< 1.5s)", dt < 1.5, f"({dt:.2f}s)")
srv.shutdown()

print("\n=== 6. mjpeg_generator sale solo con la señal de apagado ===")
# Regresión del bloqueo circular del Ctrl+C: uvicorn espera a que terminen las
# respuestas en vuelo ANTES de ejecutar el shutdown del lifespan, que es quien
# marca _stop_event. Un generador que solo mirase _stop_event no podía salir
# nunca a tiempo -> 5s de timeout y un CancelledError por cliente conectado.
# Aquí levantamos la bandera a mano (sin señal de verdad) y comprobamos que el
# generador termina SIN que nadie llame a stop().
srv2, puerto2 = servidor_mjpeg_local()
cfg3 = camera.CameraConfig(
    camera_id="apagado", stream_url=f"http://127.0.0.1:{puerto2}/",
    device="cpu", model_name="yolo11n", default_infer=False,
)
s3 = camera.CameraSession(cfg3)
s3.start(explicit=True)
time.sleep(1.0)

gen_estado = {}


def consumir_gen():
    t_ini = time.perf_counter()
    n = 0
    for _ in s3.mjpeg_generator(infer=False):
        n += 1
    gen_estado["dt"] = time.perf_counter() - t_ini
    gen_estado["frames"] = n


hilo_gen = threading.Thread(target=consumir_gen, daemon=True, name="gen-apagado")
hilo_gen.start()
time.sleep(1.0)
check("generador emitiendo frames", hilo_gen.is_alive())

t0 = time.perf_counter()
apagado._SHUTTING_DOWN = True
try:
    hilo_gen.join(timeout=3)
    dt = time.perf_counter() - t0
    check("el generador termina solo, sin stop()", not hilo_gen.is_alive(), f"({dt:.2f}s)")
    check("y lo hace rápido (< 1s)", dt < 1.0, f"({dt:.2f}s)")
    check("el cliente quedó descontado", s3.client_count == 0, f"({s3.client_count})")
    check("la sesión sigue viva (solo salió el generador)", s3.is_running)
finally:
    apagado._SHUTTING_DOWN = False  # no contaminar el resto del proceso
s3.shutdown()
srv2.shutdown()

print("\n=== 7. el hook de señal se encadena al handler de uvicorn ===")
# _install_shutdown_signal_hook tiene que hacer DOS cosas: levantar la bandera
# y seguir llamando al handler que ya estaba puesto (Server.handle_exit), que
# es quien de verdad arranca el apagado de uvicorn. Si se comiera la señal, el
# servidor no se enteraría del Ctrl+C y no se apagaría nunca.
previos = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
llamadas = []
try:
    signal.signal(signal.SIGINT, lambda signum, frame: llamadas.append(signum))
    apagado._SHUTTING_DOWN = False
    apagado._install_shutdown_signal_hook()
    check("no levanta la bandera antes de tiempo", not apagado.is_shutting_down())

    # Invocamos el handler instalado en vez de signal.raise_signal(): una señal
    # de verdad hace que CPython escriba en el signal wakeup fd, que en este
    # proceso de test está sin conectar, y ensucia la salida con un
    # "Exception ignored ... WinError 10057" que no dice nada. Cogerlo con
    # getsignal() comprueba igualmente que _install_shutdown_signal_hook lo
    # dejó puesto: si no lo hubiera instalado, aquí estaría todavía el fake y
    # la bandera no se levantaría.
    instalado = signal.getsignal(signal.SIGINT)
    check("el hook quedó instalado", instalado is not None and callable(instalado))
    instalado(signal.SIGINT, None)
    check("la señal levanta la bandera", apagado.is_shutting_down())
    check("y delega en el handler de uvicorn", llamadas == [signal.SIGINT], f"({llamadas})")
finally:
    apagado._SHUTTING_DOWN = False
    for s, h in previos.items():
        try:
            signal.signal(s, h)
        except (ValueError, OSError, TypeError):
            pass

print("\n=== Tracebacks no capturados en hilos ===")
if tracebacks_de_hilos:
    for t in tracebacks_de_hilos:
        print(t)
    fallos.append("tracebacks en hilos")
else:
    print("  PASS   ninguno")

print("\n" + ("TODO OK" if not fallos else f"FALLOS: {fallos}"))
sys.exit(1 if fallos else 0)
