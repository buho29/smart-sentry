"""Comprueba que todo se cierra bien, sin necesidad de tener el ESP32 delante.

Cubre lo que pasa cuando la placa se duerme: parar la sesión con el stream
abierto, cerrar el socket de la API nativa, y que ningún hilo se quede vivo ni
muera con un traceback.

    cd detect
    venv\\Scripts\\python.exe test\\test_shutdown.py

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

failures: list[str] = []
thread_tracebacks: list[str] = []


def _hook(args):
    thread_tracebacks.append(
        f"{args.thread.name}: "
        + "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    )


threading.excepthook = _hook

import numpy as np  # noqa: E402
import shutdown  # noqa: E402
import camera  # noqa: E402
import esphome_api  # noqa: E402

ASLEEP = "192.0.2.1"


def check(name, cond, extra=""):
    print(f"  {'PASS ' if cond else 'FALLO'}  {name} {extra}")
    if not cond:
        failures.append(name)


def local_mjpeg_server():
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
    part = (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(part)
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
c = esphome_api.EsphomeController(address=ASLEEP, noise_psk=None, watch_entity_object_id="awake")
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
    address=ASLEEP, noise_psk=None, watch_entity_object_id="awake",
    connect_attempt_timeout_sec=0.3, safety_retry_sec=30.0,
)
time.sleep(1.5)  # ya falló el connect y está esperando los 30s
t0 = time.perf_counter()
c2.shutdown()
dt = time.perf_counter() - t0
check("hilo terminado", not c2._thread.is_alive())
check("loop cerrado", c2._loop.is_closed())
check("no espera los 30s de safety_retry", dt < 2.0, f"({dt:.2f}s)")

print("\n=== 3. shutdown() idempotente; notify_awake()/call_service() después no revientan ===")
try:
    c2.shutdown()
    c2.notify_awake()
    c2.call_service("whatever", pan=0.0, tilt=0.0)
    check("segunda llamada sin excepción", True)
except Exception as e:
    check("segunda llamada sin excepción", False, repr(e))

print("\n=== 3a. call_service() con un servicio que la placa no publica ===")
# Es el camino que usará cualquier variante nueva (el relé de la pistola) para
# saber si el firmware la soporta: tiene que devolver False sin lanzar, no
# fallar en silencio ni reventar el hilo que lo llame.
c3 = esphome_api.EsphomeController(address=ASLEEP, noise_psk=None,
                                   connect_attempt_timeout_sec=0.3)
try:
    check("sin conectar, has_service() dice que no",
          c3.has_service("set_servo_position") is False)
    check("y no publica ninguno", c3.services == (), f"({c3.services})")
    check("call_service() devuelve False en vez de lanzar",
          c3.call_service("fire") is False)
    check("aunque lleve argumentos",
          c3.call_service("set_servo_position", pan=0.5, tilt=-0.5) is False)
finally:
    c3.shutdown()
check("y sigue devolviendo False después del shutdown",
      c3.call_service("fire") is False)

print("\n=== 3b. El lector, al rendirse, para la sesión entera ===")
# Regresión de la GPU al 54% para siempre: el lector se rendía ("la placa
# parece dormida, dejo de reintentar"), moría solo ese hilo, y el de proceso
# seguía vivo con la cola vacía (entonces lanzando inferencias dummy de
# keep-alive cada 50 ms). Aquí nadie llama a stop(): tiene que pararse sola.
cfg_giveup = camera.CameraConfig(
    camera_id="give-up", stream_url=f"http://{ASLEEP}:8080/",
    device="cpu", model_name="yolo11n",
    noise_psk="JzwVvMMnr1H0kAtYHDnWKfZ/7L0RYVqOeGjWxwpIzRc=",
)
s_g = camera.CameraSession(cfg_giveup)
s_g.start(explicit=True)
check("arranca con explicit_start", s_g.explicit_start and s_g.is_running)
# El EsphomeController nunca conecta contra TEST-NET-1, así que en cuanto
# falle el stream el lector entra por el break de "la placa parece dormida".
deadline = time.monotonic() + 30
while time.monotonic() < deadline and s_g.is_running:
    time.sleep(0.5)
check("la sesión se para sola, sin que nadie llame a stop()", not s_g.is_running)
check("_stop_event marcado", s_g._stop_event.is_set())
check("hilo de proceso muerto (era el que quemaba la GPU)",
      not s_g._processing_thread.is_alive())
check("explicit_start limpiado, para que un awake=on rearranque",
      not s_g.explicit_start)
s_g.shutdown()

print("\n=== 3bis. El parser multipart del stream del ESP32 ===")
# Antes vivía incrustado en _read_loop y solo se probaba de refilón, con un
# servidor falso que manda partes perfectas de un solo chunk. Justo los casos
# que motivaron escribirlo por Content-Length no se probaban nunca.


def part(body: bytes, with_length: bool = True) -> bytes:
    header = b"--frame\r\nContent-Type: image/jpeg\r\n"
    if with_length:
        header += b"Content-Length: " + str(len(body)).encode() + b"\r\n"
    return header + b"\r\n" + body + b"\r\n"


def chunk(data: bytes, n: int) -> list[bytes]:
    return [data[i:i + n] for i in range(0, len(data), n)]


A, B = b"PRIMERO-jpeg", b"SEGUNDO"
stream = part(A) + part(B)

check("dos partes en un solo chunk",
      list(camera._iter_jpegs([stream])) == [A, B])
check("cabecera y cuerpo partidos byte a byte",
      list(camera._iter_jpegs(chunk(stream, 1))) == [A, B])
check("troceado en 3 bytes", list(camera._iter_jpegs(chunk(stream, 3))) == [A, B])

# El cuelgue original: los marcadores JPEG dentro de los datos comprimidos.
# Buscándolos a mano el stream se desincronizaba; por Content-Length da igual.
dirty = b"\xff\xd8" + b"ruido\xff\xd9mas\xff\xd8ruido" + b"\xff\xd9"
check("un JPEG con \\xff\\xd8/\\xff\\xd9 dentro sale entero",
      list(camera._iter_jpegs([part(dirty)])) == [dirty])
check("y también troceado", list(camera._iter_jpegs(chunk(part(dirty), 2))) == [dirty])

# Una parte sin Content-Length (p.ej. la línea de boundary suelta) no debe
# romper el flujo: se ignora y se resincroniza con la siguiente cabecera.
check("parte sin Content-Length -> se salta, no rompe",
      list(camera._iter_jpegs([b"--frame\r\n\r\n" + part(A)])) == [A])

check("sin datos no devuelve nada", list(camera._iter_jpegs([])) == [])
check("basura sin cabecera no inventa frames",
      list(camera._iter_jpegs([b"no hay nada que parsear aqui"])) == [])

# Salvavidas del buffer: un cuerpo que nunca llega a completarse no puede
# hacer crecer la memoria sin fin.
huge = b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 999999999\r\n\r\n"
output = list(camera._iter_jpegs([huge] + [b"x" * 100_000] * 12, max_buffer=500_000))
check("un cuerpo imposible no cuelga ni devuelve basura", output == [])

print("\n=== 3c. El keep-alive de GPU ya no existe ===")
# Se quitó entero: ni con Force P2 = Off ni con "Prefer maximum performance" en
# el driver dejaba la GTX 1080 de bajar a P5 con yolo26n, y la solución de
# verdad es un modelo pesado. yolo26n se puede seguir eligiendo: las
# detecciones corruptas se descartan y se cuentan (_is_corrupt, más abajo).
check("el modelo por defecto es yolo26m",
      camera.DEFAULT_MODEL == "yolo26m"
      and camera.CameraConfig.model_fields["model_name"].default is camera.DEFAULT_MODEL)
for gone in ("_should_keepalive", "KEEPALIVE_INTERVAL_SEC", "KEEPALIVE_IDLE_LIMIT_SEC",
             "keepalive_enabled",
             "resolve_keepalive", "gpu_underclocked", "_is_pascal", "_any_pascal",
             "gpu_clock_pct", "_gpu_state", "KEEPALIVE_INTERVAL_LIGHT_SEC"):
    check(f"ya no existe {gone}",
          not hasattr(camera, gone) and not hasattr(camera.GLOBAL_CONFIG, gone))
for gone in ("_instrument_tracker", "cached_model", "latest_frame"):
    check(f"CameraSession ya no tiene {gone}",
          not hasattr(camera.CameraSession, gone))
check("CameraConfig no tiene campos de keep-alive",
      not [f for f in camera.CameraConfig.model_fields if f.startswith("keepalive")],
      str([f for f in camera.CameraConfig.model_fields if f.startswith("keepalive")]))
check("/config no expone nada del keep-alive",
      not [k for k in camera.GLOBAL_CONFIG.as_dict() if k.startswith("keepalive")])

# Un global_config.json de una version anterior trae claves que ya no existen.
# Tiene que cargar igual, ignorandolas.
import tempfile as _tmp0  # noqa: E402
_old_file = camera.GLOBAL_CONFIG_FILE
camera.GLOBAL_CONFIG_FILE = Path(_tmp0.mkdtemp()) / "global_config.json"
camera.GLOBAL_CONFIG_FILE.write_text(
    '{"keepalive_enabled": true, "keepalive_clock_ratio": 0.7,'
    ' "keepalive_interval_sec": 0.05, "keepalive_idle_limit_sec": 9.0,'
    ' "reconnect_delay_sec": 2.5}',
    encoding="utf-8")
camera.GLOBAL_CONFIG.reconnect_delay_sec = 1.0
camera.GLOBAL_CONFIG.load()
check("un global_config.json viejo carga ignorando lo que sobra",
      camera.GLOBAL_CONFIG.reconnect_delay_sec == 2.5)
check("y no se cuelan las claves muertas como atributos",
      not any(hasattr(camera.GLOBAL_CONFIG, k) for k in
              ("keepalive_enabled", "keepalive_clock_ratio",
               "keepalive_interval_sec", "keepalive_idle_limit_sec")))

# Una clave que SI existe pero con un tipo que el codigo no acepta no puede
# colarse: se ignora y manda el defecto.
camera.GLOBAL_CONFIG_FILE.write_text(
    '{"reconnect_delay_sec": "uno"}', encoding="utf-8")
camera.GLOBAL_CONFIG.reconnect_delay_sec = 1.0
camera.GLOBAL_CONFIG.load()
check("un numero mal escrito se ignora y manda el defecto",
      camera.GLOBAL_CONFIG.reconnect_delay_sec == 1.0,
      f"({camera.GLOBAL_CONFIG.reconnect_delay_sec!r})")

# Un entero donde se espera float SI vale: JSON escribe 3 donde el defecto es
# 3.0. Un bool no, aunque Python lo considere subclase de int.
camera.GLOBAL_CONFIG_FILE.write_text(
    '{"reconnect_delay_sec": 12}', encoding="utf-8")
camera.GLOBAL_CONFIG.load()
check("un int donde se espera float se acepta y se convierte",
      camera.GLOBAL_CONFIG.reconnect_delay_sec == 12.0
      and isinstance(camera.GLOBAL_CONFIG.reconnect_delay_sec, float))
camera.GLOBAL_CONFIG_FILE.write_text(
    '{"reconnect_delay_sec": true}', encoding="utf-8")
camera.GLOBAL_CONFIG.load()
check("pero un bool donde se espera float, no",
      camera.GLOBAL_CONFIG.reconnect_delay_sec == 12.0)

camera.GLOBAL_CONFIG_FILE.write_text('[1, 2, 3]', encoding="utf-8")
camera.GLOBAL_CONFIG.load()
check("un JSON que no es un objeto tampoco rompe nada",
      camera.GLOBAL_CONFIG.reconnect_delay_sec == 12.0)
camera.GLOBAL_CONFIG.reconnect_delay_sec = 1.0
camera.GLOBAL_CONFIG_FILE = _old_file

# El canario del bug de P-state. En produccion se vieron una confianza de 1.812
# y otra de 3.58e15: no son detecciones raras, es la GPU devolviendo basura.
from detections import Detection as _Det  # noqa: E402

_sana = _Det(x1=10, y1=10, x2=30, y2=40, cls=0, label="person", conf=0.9)
for nombre, d, esperado in [
    ("una deteccion normal no es corrupta", _sana, False),
    ("conf por encima de 1 es corrupta",
     _Det(x1=10, y1=10, x2=30, y2=40, cls=0, label="cat", conf=1.812), True),
    ("conf enorme (memoria sin inicializar) es corrupta",
     _Det(x1=10, y1=10, x2=30, y2=40, cls=0, label="cat", conf=3.58e15), True),
    ("conf negativa es corrupta",
     _Det(x1=10, y1=10, x2=30, y2=40, cls=0, label="cat", conf=-0.5), True),
    ("conf justo en los limites NO es corrupta",
     _Det(x1=10, y1=10, x2=30, y2=40, cls=0, label="cat", conf=1.0), False),
    ("coordenada NaN es corrupta",
     _Det(x1=float("nan"), y1=10, x2=30, y2=40, cls=0, label="cat", conf=0.9), True),
    ("coordenada infinita es corrupta",
     _Det(x1=10, y1=10, x2=float("inf"), y2=40, cls=0, label="cat", conf=0.9), True),
]:
    check(nombre, camera._is_corrupt(d) is esperado)

# Se cuentan FRAMES corruptos, no cajas: un frame roto puede traer cientos de
# cajas basura y el contador de cajas hacía parecer mil fallos lo que eran tres.
_s_st = camera.CameraSession(camera.CameraConfig(
    camera_id="status-corrupt", stream_url="http://127.0.0.1:1/",
    device="cpu", model_name="yolo11n"))
_st = _s_st.status()
check("/status trae corrupt_frames y empieza en 0", _st.get("corrupt_frames") == 0, str(_st.get("corrupt_frames")))
check("y ya no corrupt_detections (contaba cajas)", "corrupt_detections" not in _st)

# Un frame con alguna caja imposible se descarta ENTERO: si la inferencia salió
# rota, las cajas "normales" de ese frame tampoco son de fiar.
_rota = _Det(x1=10, y1=10, x2=30, y2=40, cls=0, label="person", conf=1.812)
_out = _s_st._drop_corrupt([_rota, _rota, _rota, _sana])
check("frame con 3 cajas imposibles y 1 sana -> se descarta entero", _out == [], str(_out))
check("y cuenta UN frame, no tres cajas", _s_st._corrupt_frames == 1, str(_s_st._corrupt_frames))
_limpio = [_sana, _sana]
check("un frame sano pasa entero (también dos cajas idénticas)",
      _s_st._drop_corrupt(_limpio) == _limpio)
check("y no toca el contador", _s_st._corrupt_frames == 1)
_s_st._drop_corrupt([_rota])
check("otro frame roto -> 2", _s_st._corrupt_frames == 2, str(_s_st._corrupt_frames))
check("/status lo refleja", _s_st.status()["corrupt_frames"] == 2)
_s_st.shutdown()


# GLOBAL_CONFIG se persiste. Antes no, y cada reinicio se llevaba por delante lo
# ajustado por API: cuesta explicar que un POST "no haya servido de nada".
import tempfile as _tmp  # noqa: E402
_cfg_before = camera.GLOBAL_CONFIG_FILE
camera.GLOBAL_CONFIG_FILE = Path(_tmp.mkdtemp()) / "global_config.json"
camera.GLOBAL_CONFIG.reconnect_delay_sec = 4.0
camera.GLOBAL_CONFIG.save()
camera.GLOBAL_CONFIG.reconnect_delay_sec = 1.0       # simula un reinicio
camera.GLOBAL_CONFIG.load()
check("los ajustes globales sobreviven a un reinicio",
      camera.GLOBAL_CONFIG.reconnect_delay_sec == 4.0)
camera.GLOBAL_CONFIG_FILE.write_text("{esto no es json", encoding="utf-8")
camera.GLOBAL_CONFIG.load()
check("un global_config.json roto no impide arrancar",
      camera.GLOBAL_CONFIG.reconnect_delay_sec == 4.0)
camera.GLOBAL_CONFIG.reconnect_delay_sec = 1.0
camera.GLOBAL_CONFIG_FILE = _cfg_before

# gl_keeper: ventana OpenGL para que la GTX 1080 no baje a P5 con yolo26n.
# Tiene que venir apagada (solo sirve con un ajuste concreto del driver) y no
# abrir nada mientras lo esté.
import gl_keeper  # noqa: E402

check("gl_keeper_enabled viene apagado", camera.GlobalConfig().gl_keeper_enabled is False)
check("y se persiste", "gl_keeper_enabled" in camera.GlobalConfig._PERSISTED)
for nombre, args, esperado in [
    ("apagado y sin cámaras -> no", (False, False), False),
    ("apagado con cámaras -> no", (False, True), False),
    ("encendido sin cámaras -> no (la GPU puede bajar a reposo)", (True, False), False),
    ("encendido con cámaras -> sí", (True, True), True),
]:
    check(f"gl_keeper: {nombre}", gl_keeper._should_run(*args) is esperado)

camera.GLOBAL_CONFIG.gl_keeper_enabled = False
_keeper = gl_keeper.GlKeeper(lambda: True)
_keeper.start()
time.sleep(0.3)
check("apagado no abre la ventana aunque haya cámaras", _keeper.status()["active"] is False)
_keeper.stop()
check("stop() termina el hilo", not _keeper._thread.is_alive())
_raro = gl_keeper.GlKeeper(lambda: 1 / 0)
check("un is_needed que falla cuenta como 'no hace falta'", _raro._wants() is False)


# Cada camara tiene su PROPIA instancia del modelo. La cache estuvo indexada
# solo por modelo+device, asi que dos camaras con el mismo yolo26n en cuda
# recibian el mismo objeto YOLO: mismo predictor y mismo ByteTrack. Con una sola
# camara no se nota; con dos, los track_id saltan entre escenas, un
# reset_tracker en una afecta a la otra, y dos hilos llaman a track() sobre el
# mismo predictor sin lock.
_m_a = camera.get_model("yolo11n", "cpu", "camA")
_m_b = camera.get_model("yolo11n", "cpu", "camB")
_m_shared = camera.get_model("yolo11n", "cpu")
check("dos camaras con el mismo modelo NO comparten instancia",
      _m_a is not _m_b)
check("la misma camara reutiliza la suya",
      camera.get_model("yolo11n", "cpu", "camA") is _m_a)
check("quien no pide owner (detect-file, scripts) comparte",
      camera.get_model("yolo11n", "cpu") is _m_shared)
check("y esa compartida es distinta de las de las camaras",
      _m_shared is not _m_a and _m_shared is not _m_b)
check("release_model suelta solo la de esa camara",
      camera.release_model("yolo11n", "cpu", "camA") is True
      and camera.model_key("yolo11n", "cpu", "camB") in camera._loaded_models)
check("y no se puede soltar dos veces",
      camera.release_model("yolo11n", "cpu", "camA") is False)
camera.release_model("yolo11n", "cpu", "camB")

print("\n=== 3d. always_infer: detecta sin nadie mirando el stream ===")
# Lo pedido: la cámara tiene que seguir detectando con el navegador cerrado.
# Ni un solo cliente ni consumidores en toda esta prueba.
srv_ai, port_ai = local_mjpeg_server()


def infers_alone(always_infer: bool, seconds: float = 6.0) -> bool:
    cfg_ai = camera.CameraConfig(
        camera_id=f"ai-{always_infer}", stream_url=f"http://127.0.0.1:{port_ai}/",
        device="cpu", model_name="yolo11n", always_infer=always_infer,
    )
    s_ai = camera.CameraSession(cfg_ai)
    s_ai.start(explicit=True)
    try:
        assert s_ai.client_count == 0, "el test no debe tener clientes"
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if s_ai.last_inference_ms is not None:
                return True
            time.sleep(0.2)
        return False
    finally:
        s_ai.shutdown()


check("con always_infer=True infiere sin clientes", infers_alone(True))
check("con always_infer=False no infiere sin clientes", not infers_alone(False))
srv_ai.shutdown()

print("\n=== 3d-bis. Sin clientes no se codifica JPEG, pero /snapshot sigue fresco ===")
# Con always_infer y nadie mirando, antes se codificaba un JPEG por frame que no
# leía nadie (~15/s por cámara). Ahora solo se codifica para quien mire; el
# precio es que _latest_raw_jpeg se queda rancio, así que /snapshot tiene que
# esperar a uno nuevo en vez de devolver el último guardado.
import cv2 as _cv2  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import main as _main  # noqa: E402
import registry as _registry  # noqa: E402

srv_e, port_e = local_mjpeg_server()
_encodes = [0]
_orig_imencode = camera.cv2.imencode


def _counted(*a, **k):
    _encodes[0] += 1
    return _orig_imencode(*a, **k)


camera.cv2.imencode = _counted
try:
    cfg_e = camera.CameraConfig(
        camera_id="encodes", stream_url=f"http://127.0.0.1:{port_e}/",
        device="cpu", model_name="yolo11n",
    )
    s_e = camera.CameraSession(cfg_e)
    _registry.CAMERAS["encodes"] = s_e
    s_e.start(explicit=True)
    time.sleep(3)
    assert s_e.client_count == 0

    _encodes[0] = 0
    time.sleep(2)
    check("sin ningún cliente no se codifica nada", _encodes[0] == 0, f"({_encodes[0]})")

    before_request = _encodes[0]
    r_snap = TestClient(_main.app).get("/cameras/encodes/snapshot?infer=false")
    check("pero /snapshot responde 200", r_snap.status_code == 200)
    check("y fuerza un frame recién codificado, no uno rancio",
          _encodes[0] > before_request, f"({_encodes[0] - before_request} imencode)")
    check("que decodifica a una imagen de verdad",
          _cv2.imdecode(np.frombuffer(r_snap.content, np.uint8), _cv2.IMREAD_COLOR) is not None)
    s_e.shutdown()
finally:
    camera.cv2.imencode = _orig_imencode
    _registry.CAMERAS.pop("encodes", None)
    srv_e.shutdown()

print("\n=== 3e. restart() relanza los hilos (cambiar modelo/device en caliente) ===")
# _process_loop resuelve el modelo y el device UNA vez, en su primera línea, así
# que cambiarlos en cfg no hace nada hasta que hay hilos nuevos. Lo delicado es
# que start() mira is_running: si no se espera a que muera la generación vieja,
# se cree que ya está todo en marcha y no arranca nada.
srv_r, port_r = local_mjpeg_server()
cfg_r = camera.CameraConfig(
    camera_id="restart", stream_url=f"http://127.0.0.1:{port_r}/",
    device="cpu", model_name="yolo11n",
)
s_rst = camera.CameraSession(cfg_r)
check("restart() sin sesión arrancada no hace nada", s_rst.restart() is False)
s_rst.start(explicit=True)
time.sleep(3)
old_reader, old_processor = s_rst._reader_thread, s_rst._processing_thread
check("arranca e infiere", s_rst.is_running and s_rst.last_inference_ms is not None)

s_rst.last_inference_ms = None
s_rst.cfg.model_name = "yolo11m"
check("restart() con la sesión viva devuelve True", s_rst.restart() is True)
check("los hilos son otros", s_rst._reader_thread is not old_reader
      and s_rst._processing_thread is not old_processor)
check("y los viejos están muertos",
      not old_reader.is_alive() and not old_processor.is_alive())
check("explicit_start se conserva", s_rst.explicit_start)
time.sleep(4)
check("vuelve a inferir con el modelo nuevo", s_rst.last_inference_ms is not None)
s_rst.shutdown()
srv_r.shutdown()

print("\n=== 4. CameraSession completa (stream y placa inalcanzables) ===")
cfg = camera.CameraConfig(
    camera_id="test", stream_url=f"http://{ASLEEP}:8080/",
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
srv, port = local_mjpeg_server()
cfg2 = camera.CameraConfig(
    camera_id="local", stream_url=f"http://127.0.0.1:{port}/",
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
srv2, port2 = local_mjpeg_server()
cfg3 = camera.CameraConfig(
    camera_id="shutdown", stream_url=f"http://127.0.0.1:{port2}/",
    device="cpu", model_name="yolo11n", default_infer=False,
)
s3 = camera.CameraSession(cfg3)
s3.start(explicit=True)
time.sleep(1.0)

gen_state = {}


def consume_gen():
    t_start = time.perf_counter()
    n = 0
    for _ in s3.mjpeg_generator(infer=False):
        n += 1
    gen_state["dt"] = time.perf_counter() - t_start
    gen_state["frames"] = n


gen_thread = threading.Thread(target=consume_gen, daemon=True, name="gen-shutdown")
gen_thread.start()
time.sleep(1.0)
check("generador emitiendo frames", gen_thread.is_alive())

t0 = time.perf_counter()
shutdown._SHUTTING_DOWN = True
try:
    gen_thread.join(timeout=3)
    dt = time.perf_counter() - t0
    check("el generador termina solo, sin stop()", not gen_thread.is_alive(), f"({dt:.2f}s)")
    check("y lo hace rápido (< 1s)", dt < 1.0, f"({dt:.2f}s)")
    check("el cliente quedó descontado", s3.client_count == 0, f"({s3.client_count})")
    check("la sesión sigue viva (solo salió el generador)", s3.is_running)
finally:
    shutdown._SHUTTING_DOWN = False  # no contaminar el resto del proceso
s3.shutdown()
srv2.shutdown()

print("\n=== 7. el hook de señal se encadena al handler de uvicorn ===")
# _install_shutdown_signal_hook tiene que hacer DOS cosas: levantar la bandera
# y seguir llamando al handler que ya estaba puesto (Server.handle_exit), que
# es quien de verdad arranca el apagado de uvicorn. Si se comiera la señal, el
# servidor no se enteraría del Ctrl+C y no se apagaría nunca.
previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
calls = []
try:
    signal.signal(signal.SIGINT, lambda signum, frame: calls.append(signum))
    shutdown._SHUTTING_DOWN = False
    shutdown._install_shutdown_signal_hook()
    check("no levanta la bandera antes de tiempo", not shutdown.is_shutting_down())

    # Invocamos el handler instalado en vez de signal.raise_signal(): una señal
    # de verdad hace que CPython escriba en el signal wakeup fd, que en este
    # proceso de test está sin conectar, y ensucia la salida con un
    # "Exception ignored ... WinError 10057" que no dice nada. Cogerlo con
    # getsignal() comprueba igualmente que _install_shutdown_signal_hook lo
    # dejó puesto: si no lo hubiera instalado, aquí estaría todavía el fake y
    # la bandera no se levantaría.
    installed = signal.getsignal(signal.SIGINT)
    check("el hook quedó instalado", installed is not None and callable(installed))
    installed(signal.SIGINT, None)
    check("la señal levanta la bandera", shutdown.is_shutting_down())
    check("y delega en el handler de uvicorn", calls == [signal.SIGINT], f"({calls})")
finally:
    shutdown._SHUTTING_DOWN = False
    for s, h in previous.items():
        try:
            signal.signal(s, h)
        except (ValueError, OSError, TypeError):
            pass

print("\n=== Tracebacks no capturados en hilos ===")
if thread_tracebacks:
    for t in thread_tracebacks:
        print(t)
    failures.append("tracebacks en hilos")
else:
    print("  PASS   ninguno")

print("\n" + ("TODO OK" if not failures else f"FALLOS: {failures}"))
sys.exit(1 if failures else 0)
