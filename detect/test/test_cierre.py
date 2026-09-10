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

import main  # noqa: E402

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
c = main.EsphomeController(address=DORMIDA, noise_psk=None, watch_entity_object_id="estado")
time.sleep(0.2)  # le pillamos con el connect() en vuelo
t0 = time.perf_counter()
c.shutdown()
dt = time.perf_counter() - t0
check("hilo terminado", not c._thread.is_alive())
check("loop cerrado", c._loop.is_closed())
check("_closed marcado", c._closed.is_set())
check("tarda < 2s", dt < 2.0, f"({dt:.2f}s)")

print("\n=== 2. EsphomeController: shutdown durante la espera de safety_retry ===")
c2 = main.EsphomeController(
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

print("\n=== 3. shutdown() idempotente; notify_awake()/move_servo() después no revientan ===")
try:
    c2.shutdown()
    c2.notify_awake()
    c2.move_servo(90, 90)
    check("segunda llamada sin excepción", True)
except Exception as e:
    check("segunda llamada sin excepción", False, repr(e))

print("\n=== 3b. El lector, al rendirse, para la sesión entera ===")
# Regresión de la GPU al 54% para siempre: el lector se rendía ("la placa
# parece dormida, dejo de reintentar"), moría solo ese hilo, y el de proceso
# seguía vivo con la cola vacía lanzando inferencias dummy de keep-alive cada
# 50 ms indefinidamente. Aquí nadie llama a stop(): tiene que pararse sola.
cfg_rendir = main.CameraConfig(
    camera_id="rendirse", stream_url=f"http://{DORMIDA}:8080/",
    device="cpu", model_name="yolo11n",
    noise_psk="JzwVvMMnr1H0kAtYHDnWKfZ/7L0RYVqOeGjWxwpIzRc=",
)
s_r = main.CameraSession(cfg_rendir)
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

print("\n=== 3c. El keep-alive de GPU caduca sin frames ===")
for nombre, args, esperado in [
    ("recién llegado un frame -> calienta", (True, True, 0.5, 3.0), True),
    ("justo antes del límite -> calienta", (True, True, 2.9, 3.0), True),
    ("pasado el límite -> NO calienta", (True, True, 3.1, 3.0), False),
    ("mucho después -> NO calienta", (True, True, 600.0, 3.0), False),
    ("en CPU -> nunca calienta", (False, True, 0.1, 3.0), False),
    ("keepalive desactivado -> nunca", (True, False, 0.1, 3.0), False),
]:
    check(nombre, main._toca_keepalive(*args) is esperado)

print("\n=== 4. CameraSession completa (stream y placa inalcanzables) ===")
cfg = main.CameraConfig(
    camera_id="test", stream_url=f"http://{DORMIDA}:8080/",
    device="cpu", model_name="yolo11n",
    noise_psk="JzwVvMMnr1H0kAtYHDnWKfZ/7L0RYVqOeGjWxwpIzRc=",
)
s = main.CameraSession(cfg)
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
cfg2 = main.CameraConfig(
    camera_id="local", stream_url=f"http://127.0.0.1:{puerto}/",
    device="cpu", model_name="yolo11n",
)
s2 = main.CameraSession(cfg2)
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
cfg3 = main.CameraConfig(
    camera_id="apagado", stream_url=f"http://127.0.0.1:{puerto2}/",
    device="cpu", model_name="yolo11n", default_infer=False,
)
s3 = main.CameraSession(cfg3)
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
main._SHUTTING_DOWN = True
try:
    hilo_gen.join(timeout=3)
    dt = time.perf_counter() - t0
    check("el generador termina solo, sin stop()", not hilo_gen.is_alive(), f"({dt:.2f}s)")
    check("y lo hace rápido (< 1s)", dt < 1.0, f"({dt:.2f}s)")
    check("el cliente quedó descontado", s3.client_count == 0, f"({s3.client_count})")
    check("la sesión sigue viva (solo salió el generador)", s3.is_running)
finally:
    main._SHUTTING_DOWN = False  # no contaminar el resto del proceso
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
    main._SHUTTING_DOWN = False
    main._install_shutdown_signal_hook()
    check("no levanta la bandera antes de tiempo", not main.is_shutting_down())

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
    check("la señal levanta la bandera", main.is_shutting_down())
    check("y delega en el handler de uvicorn", llamadas == [signal.SIGINT], f"({llamadas})")
finally:
    main._SHUTTING_DOWN = False
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
