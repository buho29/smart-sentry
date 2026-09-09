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
    de verdad viva contra la que probar stop()."""

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\n"
                        b"Content-Length: 4\r\n\r\nAAAA\r\n"
                    )
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

print("\n=== Tracebacks no capturados en hilos ===")
if tracebacks_de_hilos:
    for t in tracebacks_de_hilos:
        print(t)
    fallos.append("tracebacks en hilos")
else:
    print("  PASS   ninguno")

print("\n" + ("TODO OK" if not fallos else f"FALLOS: {fallos}"))
sys.exit(1 if fallos else 0)
