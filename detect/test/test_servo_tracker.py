"""Comprueba la lógica de seguimiento con servos, sin GPU ni ESP32 delante.

ServoTracker solo depende de objetos `Detection` planos y de algo con
`llamar_servicio()`, así que aquí se le dan detecciones a mano y una placa de mentira
que apunta lo que recibe. Eso permite probar la puntería, la zona muerta, el
rate limit y el bloqueo de objetivo en milisegundos, en vez de mirando si la
torreta tiembla.

    cd detect
    venv\\Scripts\\python.exe test\\test_servo_tracker.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from detections import Detection  # noqa: E402
from servo_tracker import ServoConfig, ServoTracker  # noqa: E402

fallos: list[str] = []

W, H = 640, 480


def check(nombre, cond, extra=""):
    print(f"  {'PASS ' if cond else 'FALLO'}  {nombre} {extra}")
    if not cond:
        fallos.append(nombre)


class PlacaFalsa:
    """Doble de EsphomeController: solo apunta las llamadas recibidas."""

    def __init__(self, has_servo_service=True, servicio="set_servo_position"):
        self._servicios = {servicio} if has_servo_service else set()
        self.ordenes: list[tuple[float, float]] = []
        # Nombre del servicio de cada llamada, para poder comprobar que se
        # llama al que dice la config y no a uno cableado en el código.
        self.llamadas: list[str] = []

    def tiene_servicio(self, nombre):
        return nombre in self._servicios

    @property
    def servicios(self):
        return tuple(self._servicios)

    def llamar_servicio(self, nombre, /, **args):
        self.llamadas.append(nombre)
        self.ordenes.append((args["pan"], args["tilt"]))
        return True


def det(cx, cy, track_id=1, label="person", ancho=40, alto=80):
    """Una detección centrada en (cx, cy)."""
    return Detection(
        x1=cx - ancho / 2, y1=cy - alto / 2, x2=cx + ancho / 2, y2=cy + alto / 2,
        cls=0, label=label, conf=0.9, track_id=track_id,
    )


def nuevo(**kwargs):
    """Tracker con rate limit desactivado salvo que el test lo pida."""
    kwargs.setdefault("min_interval_sec", 0.0)
    placa = PlacaFalsa()
    return ServoTracker(ServoConfig(**kwargs), placa, camera_id="test"), placa


print("\n=== 1. Puntería: corrige hacia el lado del objetivo ===")
t, placa = nuevo()
t.on_detections([det(cx=W * 0.9, cy=H / 2)], W, H)  # objetivo a la DERECHA
check("se mueve", len(placa.ordenes) == 1, f"({placa.ordenes})")
pan_derecha = placa.ordenes[-1][0]

t2, placa2 = nuevo()
t2.on_detections([det(cx=W * 0.1, cy=H / 2)], W, H)  # objetivo a la IZQUIERDA
pan_izquierda = placa2.ordenes[-1][0]
check("izquierda y derecha mueven en sentidos opuestos",
      pan_derecha * pan_izquierda < 0, f"({pan_derecha:.3f} vs {pan_izquierda:.3f})")

t3, placa3 = nuevo(invert_pan=True)
t3.on_detections([det(cx=W * 0.9, cy=H / 2)], W, H)
check("invert_pan da la vuelta al sentido",
      placa3.ordenes[-1][0] == -pan_derecha, f"({placa3.ordenes[-1][0]:.3f})")

print("\n=== 2. Converge al centro y no se pasa ===")
t, placa = nuevo(gain=0.3, deadzone=0.06)
# Simulamos el lazo cerrado a lo bruto: el objetivo se va acercando al centro a
# medida que la torreta apunta mejor. Lo que se comprueba es que el lazo
# converge (los pasos menguan y acaba callándose al entrar en la zona muerta),
# no que el error llegue a cero: para eso está justamente la zona muerta.
FRAMES = 20
cx = W * 0.95
for _ in range(FRAMES):
    t.on_detections([det(cx=cx, cy=H / 2)], W, H)
    # el objetivo se "acerca" al centro en proporción a lo que se movió el servo
    cx = W / 2 + (cx - W / 2) * 0.7
check("nunca sale del rango [-1, 1]",
      all(-1.0 <= p <= 1.0 and -1.0 <= ti <= 1.0 for p, ti in placa.ordenes))
pasos = [abs(b[0] - a[0]) for a, b in zip(placa.ordenes, placa.ordenes[1:])]
check("cada corrección es menor que la anterior (no oscila)",
      all(b <= a for a, b in zip(pasos, pasos[1:])), f"({[round(p, 3) for p in pasos]})")
check("deja de mandar órdenes antes de agotar los frames (ha convergido)",
      len(placa.ordenes) < FRAMES, f"({len(placa.ordenes)} órdenes en {FRAMES} frames)")

print("\n=== 3. Zona muerta: centrado = no se mueve ===")
t, placa = nuevo(deadzone=0.1)
t.on_detections([det(cx=W / 2 + 2, cy=H / 2 - 2)], W, H)
check("objetivo centrado no genera ninguna orden", placa.ordenes == [], f"({placa.ordenes})")
t.on_detections([det(cx=W * 0.9, cy=H / 2)], W, H)
check("pero descentrado sí", len(placa.ordenes) == 1)

print("\n=== 4. Rate limit: no una orden por frame ===")
t, placa = nuevo(min_interval_sec=10.0)
for _ in range(5):
    t.on_detections([det(cx=W * 0.9, cy=H / 2)], W, H)
check("5 frames seguidos -> 1 sola orden", len(placa.ordenes) == 1, f"({len(placa.ordenes)})")
t.move_to(0.5, 0.5)
check("el control manual sí salta el rate limit", len(placa.ordenes) == 2)
check("y manda lo pedido", placa.ordenes[-1] == (0.5, 0.5), f"({placa.ordenes[-1]})")

print("\n=== 5. Bloqueo de objetivo por track ID ===")
t, placa = nuevo()
# Dos objetos: engancha el más cercano al centro (#2)
t.on_detections([det(cx=W * 0.9, cy=H / 2, track_id=1),
                 det(cx=W * 0.55, cy=H / 2, track_id=2)], W, H)
check("engancha el más cercano al centro", t.target_id == 2, f"(#{t.target_id})")

# Ahora el #1 se acerca más al centro que el #2: NO debe saltar de objetivo.
t.on_detections([det(cx=W * 0.51, cy=H / 2, track_id=1),
                 det(cx=W * 0.9, cy=H / 2, track_id=2)], W, H)
check("no salta a otro objeto mientras el suyo siga visible", t.target_id == 2,
      f"(#{t.target_id})")

# Y sigue al suyo aunque esté al otro lado: la orden va hacia donde está el #2.
check("y apunta hacia donde está el suyo", placa.ordenes[-1][0] < placa.ordenes[-2][0],
      f"({placa.ordenes[-2:]})")

print("\n=== 6. Cajas sin track ID confirmado se ignoran ===")
t, placa = nuevo()
t.on_detections([det(cx=W * 0.9, cy=H / 2, track_id=None)], W, H)
check("sin ID no se engancha nada", t.target_id is None)
check("y no se mueve", placa.ordenes == [])

print("\n=== 7. Objetivo perdido: margen y luego suelta ===")
t, placa = nuevo(lost_target_sec=0.3)
t.on_detections([det(cx=W * 0.9, cy=H / 2, track_id=7)], W, H)
check("enganchado", t.target_id == 7)
t.on_detections([], W, H)  # parpadeo del detector
check("no lo suelta al primer frame vacío (puede ser una oclusión)", t.target_id == 7)
t.on_detections([det(cx=W * 0.9, cy=H / 2, track_id=9)], W, H)
check("ni se engancha a otro dentro del margen", t.target_id == 7, f"(#{t.target_id})")
time.sleep(0.35)
t.on_detections([det(cx=W * 0.9, cy=H / 2, track_id=9)], W, H)
check("pasado el plazo, engancha el nuevo", t.target_id == 9, f"(#{t.target_id})")

print("\n=== 8. on_idle (la cámara deja de dar imagen) ===")
t, placa = nuevo(lost_target_sec=0.3)
t.on_detections([det(cx=W * 0.9, cy=H / 2, track_id=3)], W, H)
t.on_idle()
check("no lo suelta enseguida", t.target_id == 3)
time.sleep(0.35)
t.on_idle()
check("pero caduca igual que con imagen", t.target_id is None)

print("\n=== 9. return_home_on_lost ===")
t, placa = nuevo(lost_target_sec=0.1, return_home_on_lost=True, home_pan=0.0, home_tilt=-0.2)
t.on_detections([det(cx=W * 0.9, cy=H / 2, track_id=4)], W, H)
time.sleep(0.15)
t.on_idle()
check("vuelve a reposo al perder el objetivo", placa.ordenes[-1] == (0.0, -0.2),
      f"({placa.ordenes[-1]})")

print("\n=== 10. enabled=False lo deja mudo ===")
t, placa = nuevo(enabled=False)
t.on_detections([det(cx=W * 0.9, cy=H / 2)], W, H)
check("no se mueve", placa.ordenes == [])
check("y no pide inferencia (no calienta la GPU para nada)", t.wants_inference() is False)

print("\n=== 10b. El servicio se coge de la config, no está cableado ===")
# Lo que permite que la pistola (relé de disparo) sea un consumidor nuevo sin
# tocar esphome_api.py: el nombre del servicio lo pone cada variante.
t, placa = nuevo()
t.on_detections([det(cx=W * 0.9, cy=H / 2)], W, H)
check("por defecto llama a set_servo_position",
      placa.llamadas == ["set_servo_position"], f"({placa.llamadas})")

placa_otra = PlacaFalsa(servicio="mover_torreta")
t_otra = ServoTracker(ServoConfig(service="mover_torreta", min_interval_sec=0.0),
                      placa_otra, camera_id="test")
t_otra.on_detections([det(cx=W * 0.9, cy=H / 2)], W, H)
check("con otro nombre en la config, llama a ese",
      placa_otra.llamadas == ["mover_torreta"], f"({placa_otra.llamadas})")
check("y lo reconoce como publicado", t_otra.status()["servo_service"] is True)
check("el status dice a cuál llama", t_otra.status()["service"] == "mover_torreta")

print("\n=== 11. status() dice si la placa publica el servicio ===")
cfg = ServoConfig()
t_sin = ServoTracker(cfg, PlacaFalsa(has_servo_service=False), "test")
check("firmware sin servos se ve en status", t_sin.status()["servo_service"] is False,
      f"({t_sin.status()})")
t_con = ServoTracker(cfg, PlacaFalsa(has_servo_service=True), "test")
check("y con servos también", t_con.status()["servo_service"] is True)

print("\n=== 12. shutdown() deja la torreta en reposo ===")
t, placa = nuevo(home_pan=0.1, home_tilt=-0.1)
t.on_detections([det(cx=W * 0.9, cy=H / 2)], W, H)
t.shutdown()
check("última orden = posición de reposo", placa.ordenes[-1] == (0.1, -0.1),
      f"({placa.ordenes[-1]})")

print("\n=== 13. Frames degenerados no revientan ===")
t, placa = nuevo()
try:
    t.on_detections([], 0, 0)
    t.on_detections([det(cx=0, cy=0)], W, H)
    check("width/height a 0 y caja en la esquina: sin excepción", True)
except Exception as e:
    check("width/height a 0 y caja en la esquina: sin excepción", False, repr(e))

print("\n=== 14. Enganche real en el pipeline (CameraSession -> consumidor) ===")
# Hasta aquí todo era el tracker aislado. Esto comprueba lo otro: que
# _process_loop de verdad construye las detecciones y se las pasa a los
# consumidores, y que un consumidor puede mantener viva la inferencia sin que
# haya ningún cliente HTTP mirando el stream.
import http.server  # noqa: E402
import socketserver  # noqa: E402
import threading  # noqa: E402

import camera  # noqa: E402
import main  # noqa: E402
import registro  # noqa: E402


class Espia:
    """Consumidor de mentira: apunta lo que le llega desde el hilo de proceso."""

    def __init__(self, quiere_inferencia=True):
        self._quiere = quiere_inferencia
        self.frames = 0
        self.idles = 0
        self.tamanos: list[tuple[int, int]] = []
        self.cerrado = False

    def on_detections(self, dets, width, height):
        self.frames += 1
        self.tamanos.append((width, height))

    def on_idle(self):
        self.idles += 1

    def wants_inference(self):
        return self._quiere

    def status(self):
        return {"frames": self.frames}

    def shutdown(self):
        self.cerrado = True


def servidor_mjpeg_local():
    """Igual que en test_cierre.py: emite un JPEG real sin parar."""
    import cv2
    import numpy as np

    ok, buf = cv2.imencode(".jpg", np.zeros((48, 64, 3), dtype=np.uint8))
    assert ok
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
                    time.sleep(0.1)
            except Exception:
                pass

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


srv, puerto = servidor_mjpeg_local()
cfg_cam = camera.CameraConfig(
    camera_id="pipeline", stream_url=f"http://127.0.0.1:{puerto}/",
    device="cpu", model_name="yolo11n",
    # A false a propósito: así lo ÚNICO que puede estar manteniendo viva la
    # inferencia es el consumidor, que es justo lo que se quiere comprobar. Con
    # el valor por defecto (true) la sesión inferiría igual y el test pasaría
    # aunque el enganche del consumidor estuviera roto.
    always_infer=False,
)
sesion = camera.CameraSession(cfg_cam)
espia = Espia()
sesion._consumers.append(espia)
sesion.start(explicit=True)
time.sleep(3.0)  # sin clientes HTTP: solo el consumidor mantiene la inferencia
check("el consumidor recibe detecciones sin ningún cliente HTTP conectado",
      espia.frames > 0, f"({espia.frames} frames)")
check("y recibe el tamaño real del frame (64x48)",
      espia.tamanos and espia.tamanos[0] == (64, 48), f"({espia.tamanos[:1]})")
check("la inferencia corrió de verdad", sesion.last_inference_ms is not None,
      f"({sesion.last_inference_ms})")
check("el estado del consumidor sale en /status",
      sesion.status().get("consumers", {}).get("Espia") == {"frames": espia.frames},
      f"({sesion.status().get('consumers')})")
sesion.shutdown()
check("shutdown() avisa al consumidor", espia.cerrado)
srv.shutdown()

print("\n=== 15. Sin consumidores, el pipeline se comporta igual que antes ===")
srv2, puerto2 = servidor_mjpeg_local()
cfg_sola = camera.CameraConfig(
    camera_id="camara-sola", stream_url=f"http://127.0.0.1:{puerto2}/",
    device="cpu", model_name="yolo11n",
    # Con always_infer a false, lo único que podría encender la inferencia son
    # los clientes o los consumidores, y aquí no hay ni unos ni otros.
    # (El caso always_infer=True lo cubre test_cierre.py, caso 3d.)
    always_infer=False,
)
sesion2 = camera.CameraSession(cfg_sola)
check("una cámara sin sección 'servo' no crea consumidores", sesion2._consumers == [])
check("ni tracker", sesion2.servo_tracker is None)
sesion2.start(explicit=True)
time.sleep(2.0)
check("con always_infer=false, sin clientes ni consumidores no hay inferencia",
      sesion2.last_inference_ms is None, f"({sesion2.last_inference_ms})")
check("y sin nadie que lo lea tampoco se codifica ningún JPEG",
      sesion2.snapshot(infer=False) is None, f"({sesion2.snapshot(infer=False) is None})")
# Antes aquí se comprobaba lo contrario ("el frame crudo se sigue publicando
# para /snapshot"): se codificaba un JPEG por frame aunque no lo leyera nadie.
# Ahora /snapshot se registra como cliente y fuerza uno recién hecho, que es lo
# que cubre el caso 3d-bis de test_cierre.py.
sesion2.shutdown()
srv2.shutdown()

print("\n=== 16. Los endpoints distinguen POR QUÉ no se mueve la torreta ===")
# El caso que más despista es "todo conectado pero el firmware no tiene servos":
# llamar a un servicio que la placa no publica falla en silencio, y desde fuera
# es idéntico a apuntar mal. Cada motivo tiene que dar un error distinto.
from fastapi import HTTPException  # noqa: E402


class EsphomeFalso:
    def __init__(self, is_connected=True, has_servo_service=True,
                 servicio="set_servo_position"):
        self.is_connected = is_connected
        self._servicios = {servicio} if has_servo_service else set()
        self.ordenes = []

    def tiene_servicio(self, nombre):
        return nombre in self._servicios

    @property
    def servicios(self):
        return tuple(self._servicios)

    def llamar_servicio(self, nombre, /, **args):
        self.ordenes.append((args["pan"], args["tilt"]))
        return True


def sesion_falsa(camera_id, con_servo=True, **kw_placa):
    """Sesión registrada en CAMERAS pero SIN arrancar hilos ni tocar el disco."""
    cfg = camera.CameraConfig(
        camera_id=camera_id, stream_url="http://192.0.2.1:8080/",
        device="cpu", model_name="yolo11n",
        servo=ServoConfig() if con_servo else None,
    )
    # Sin noise_psk la sesión no crea EsphomeController ni tracker (y avisa por
    # log de que ignora la sección 'servo': ese aviso en la salida del test es
    # esperado). Se los ponemos a mano con dobles, que es justo lo que queremos
    # controlar aquí; con un noise_psk de verdad arrancaría un hilo intentando
    # conectar contra una IP muerta.
    s = camera.CameraSession(cfg)
    if con_servo:
        s.esphome = EsphomeFalso(**kw_placa)
        s.servo_tracker = ServoTracker(cfg.servo, s.esphome, camera_id)
        s._consumers = [s.servo_tracker]
    registro.CAMERAS[camera_id] = s  # a mano: register_camera persistiría a disco
    return s


def codigo_de(camera_id):
    try:
        main.get_servo_tracker(camera_id)
        return 200
    except HTTPException as e:
        return e.status_code


sesion_falsa("sin-servo", con_servo=False)
check("cámara sin sección 'servo' -> 400", codigo_de("sin-servo") == 400,
      f"({codigo_de('sin-servo')})")

sesion_falsa("desconectada", is_connected=False)
check("placa desconectada -> 503", codigo_de("desconectada") == 503)

sesion_falsa("sin-firmware", has_servo_service=False)
check("firmware sin 'set_servo_position' -> 503", codigo_de("sin-firmware") == 503)

s_ok = sesion_falsa("torreta-ok")
check("todo en orden -> devuelve el tracker", codigo_de("torreta-ok") == 200)
main.get_servo_tracker("torreta-ok").move_to(0.25, -0.25)
check("y la orden llega a la placa", s_ok.esphome.ordenes == [(0.25, -0.25)],
      f"({s_ok.esphome.ordenes})")

check("cámara inexistente -> 404", codigo_de("no-existe") == 404)

rutas = {r.path for r in main.app.routes}
# La config del servo vive bajo /config/ como el resto de ajustes; /servo a
# secas es el movimiento manual, que es una acción y no configuración.
check("las rutas del servo están registradas",
      {"/cameras/{camera_id}/servo", "/cameras/{camera_id}/config/servo"} <= rutas)
check("todos los ajustes cuelgan de /config/",
      {"/cameras/{camera_id}/config/inference", "/cameras/{camera_id}/config/keepalive",
       "/cameras/{camera_id}/config/stream", "/cameras/{camera_id}/config/servo"} <= rutas)
check("y ya no quedan las rutas viejas",
      not ({"/cameras/{camera_id}/inference/config", "/cameras/{camera_id}/stream/config",
            "/cameras/{camera_id}/servo/tracking"} & rutas))

for cid in ("sin-servo", "desconectada", "sin-firmware", "torreta-ok"):
    registro.CAMERAS.pop(cid, None)

print("\n" + ("TODO OK" if not fallos else f"FALLOS: {fallos}"))
sys.exit(1 if fallos else 0)
