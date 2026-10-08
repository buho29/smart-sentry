"""Comprueba la lógica de seguimiento con servos, sin GPU ni ESP32 delante.

ServoTracker solo depende de objetos `Detection` planos y de algo con
`call_service()`, así que aquí se le dan detecciones a mano y una placa de mentira
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

failures: list[str] = []

W, H = 640, 480


def check(name, cond, extra=""):
    print(f"  {'PASS ' if cond else 'FALLO'}  {name} {extra}")
    if not cond:
        failures.append(name)


class FakeBoard:
    """Doble de EsphomeController: solo apunta las llamadas recibidas."""

    def __init__(self, has_servo_service=True, service="set_servo_position"):
        self._services = {service} if has_servo_service else set()
        self.commands: list[tuple[float, float]] = []
        # Nombre del servicio de cada llamada, para poder comprobar que se
        # llama al que dice la config y no a uno cableado en el código.
        self.calls: list[str] = []
        # Llamadas a set_servo_hold, aparte: no son órdenes de posición.
        self.holds: list[bool] = []

    def has_service(self, name):
        return name in self._services

    @property
    def services(self):
        return tuple(self._services)

    def call_service(self, name, /, **args):
        if "hold" in args:
            self.holds.append(args["hold"])
            return True
        self.calls.append(name)
        self.commands.append((args["pan"], args["tilt"]))
        return True

    # Ajustes de la placa (number), por object_id. Vacío = firmware sin ellos.
    states: dict = {}

    def get_state(self, object_id):
        return self.states.get(object_id)

    def set_number(self, object_id, value):
        self.states = {**self.states, object_id: value}
        return True


def det(cx, cy, track_id=1, label="person", width=40, height=80):
    """Una detección centrada en (cx, cy)."""
    return Detection(
        x1=cx - width / 2, y1=cy - height / 2, x2=cx + width / 2, y2=cy + height / 2,
        cls=0, label=label, conf=0.9, track_id=track_id,
    )


def make_tracker(**kwargs):
    """Tracker con rate limit desactivado salvo que el test lo pida."""
    kwargs.setdefault("min_interval_sec", 0.0)
    board = FakeBoard()
    return ServoTracker(ServoConfig(**kwargs), board, camera_id="test"), board


print("\n=== 1. Puntería: corrige hacia el lado del objetivo ===")
t, board = make_tracker()
t.on_detections([det(cx=W * 0.9, cy=H / 2)], W, H)  # objetivo a la DERECHA
check("se mueve", len(board.commands) == 1, f"({board.commands})")
pan_right = board.commands[-1][0]

t2, board2 = make_tracker()
t2.on_detections([det(cx=W * 0.1, cy=H / 2)], W, H)  # objetivo a la IZQUIERDA
pan_left = board2.commands[-1][0]
check("izquierda y derecha mueven en sentidos opuestos",
      pan_right * pan_left < 0, f"({pan_right:.3f} vs {pan_left:.3f})")

t3, board3 = make_tracker(invert_pan=True)
t3.on_detections([det(cx=W * 0.9, cy=H / 2)], W, H)
check("invert_pan da la vuelta al sentido",
      board3.commands[-1][0] == -pan_right, f"({board3.commands[-1][0]:.3f})")

# Cámara girada 90°: el frame llega en vertical (480x640). Un objetivo a la
# derecha/abajo de la imagen derecha tiene que mover los servos igual que en
# apaisado, porque la imagen ya está en los ejes de pan y tilt.
tp, boardp = make_tracker()
tp.on_detections([det(cx=H * 0.9, cy=W * 0.75)], H, W)
tl, boardl = make_tracker()
tl.on_detections([det(cx=W * 0.9, cy=H * 0.75)], W, H)
check("en vertical, pan y tilt van en el mismo sentido que en apaisado",
      boardp.commands[-1] == boardl.commands[-1],
      f"({boardp.commands[-1]} vs {boardl.commands[-1]})")

# Tilt con reducción 21:63: la cámara gira un tercio que el servo, así que el
# paso de tilt tiene que triplicarse y el de pan quedarse como estaba.
tg1, bg1 = make_tracker(gain=0.1)   # pasos por debajo del tope por orden
tg1.on_detections([det(cx=W * 0.8, cy=H * 0.8)], W, H)
tg3, bg3 = make_tracker(gain=0.1, tilt_gear_ratio=63 / 21)
tg3.on_detections([det(cx=W * 0.8, cy=H * 0.8)], W, H)
check("tilt_gear_ratio=3 triplica el paso de tilt",
      abs(bg3.commands[-1][1] - 3 * bg1.commands[-1][1]) < 1e-9,
      f"({bg3.commands[-1][1]:.3f} vs {bg1.commands[-1][1]:.3f})")
check("y no toca el pan", bg3.commands[-1][0] == bg1.commands[-1][0])

# Aire en los 4 lados, con prioridad arriba: de pie cerca de la cámara la
# cabeza queda fuera y la caja llega hasta y1=0.
def box(y1, y2, x1=W / 2 - 20, x2=W / 2 + 20):
    return Detection(x1=x1, y1=y1, x2=x2, y2=y2, cls=0, label="person",
                     conf=0.9, track_id=1)


def last_cmd(**kwargs):
    """Última orden que manda un tracker nuevo ante una sola detección."""
    dets = kwargs.pop("dets")
    t, b = make_tracker(**kwargs)
    t.on_detections(dets, W, H)
    return b.commands[-1] if b.commands else None


up = last_cmd(dets=[det(cx=W / 2, cy=H * 0.2)])       # objetivo por encima del centro
down = last_cmd(dets=[det(cx=W / 2, cy=H * 0.8)])     # por debajo
left = last_cmd(dets=[det(cx=W * 0.2, cy=H / 2)])     # a la izquierda
right = last_cmd(dets=[det(cx=W * 0.8, cy=H / 2)])    # a la derecha

from servo_tracker import _CLIPPED_EDGE_ERROR  # noqa: E402
FULL_STEP = ServoConfig().gain * _CLIPPED_EDGE_ERROR   # paso por borde cortado, con ratio 1
c = last_cmd(dets=[box(0, H * 0.7)])
check("cortada arriba: el tilt sube a paso máximo",
      c and c[1] * up[1] > 0 and abs(c[1]) >= FULL_STEP - 1e-9 and c[0] == 0.0,
      f"({c} vs {up})")
c = last_cmd(dets=[box(0, H)])
check("cortada arriba Y abajo: manda arriba, sube",
      c and c[1] * up[1] > 0, f"({c})")
c = last_cmd(dets=[box(H * 0.05, H * 0.95)])
check("persona alta con la cabeza cerca de arriba: centrar no baja la cámara",
      c is None or c[1] * down[1] <= 0, f"({c})")
c = last_cmd(dets=[box(H * 0.2, H * 0.98)])
check("persona a 1 m con la cabeza en el 20% superior: centrar no baja la cámara",
      c is None or c[1] * down[1] <= 0, f"({c})")
c = last_cmd(dets=[box(H * 0.45, H * 0.95)])
check("con la cabeza lejos de arriba sí baja a centrar",
      c and c[1] * down[1] > 0, f"({c})")
c = last_cmd(dets=[box(H * 0.45, H)])
c_uncut = last_cmd(dets=[Detection(x1=W / 2 - 20, y1=H * 0.45, x2=W / 2 + 20, y2=H - 10,
                                   cls=0, label="person", conf=0.9, track_id=1)])
check("cortada abajo: no se persigue, se trata como si no lo estuviera",
      c is not None and c_uncut is not None and abs(c[1] - c_uncut[1]) < 1e-9,
      f"({c} vs {c_uncut})")
c = last_cmd(dets=[box(H * 0.3, H * 0.7, x1=0, x2=W * 0.4)])
check("estrecha y cortada a la izquierda: el pan va a la izquierda a paso máximo",
      c and c[0] * left[0] > 0 and abs(c[0]) >= FULL_STEP - 1e-9, f"({c} vs {left})")
c = last_cmd(dets=[box(H * 0.3, H * 0.7, x1=W * 0.6, x2=W)])
check("estrecha y cortada a la derecha: el pan va a la derecha a paso máximo",
      c and c[0] * right[0] > 0 and abs(c[0]) >= FULL_STEP - 1e-9, f"({c} vs {right})")
c = last_cmd(dets=[box(H * 0.3, H * 0.7, x1=0, x2=W * 0.9)])
check("ancha (persona a 1 m) tocando la izquierda: solo centra, sin golpe",
      c is None or abs(c[0]) < FULL_STEP, f"({c})")
c = last_cmd(dets=[box(H * 0.3, H * 0.7, x1=0, x2=W)])
check("más ancha que el frame y centrada: el pan no se mueve", c is None, f"({c})")

# Anticipación: un objetivo que cruza hacia la derecha con la cámara quieta.
def walk_right(**kwargs):
    """Objetivo cerca del centro moviéndose a ~200 px/s a la derecha. Devuelve
    el tracker y la placa; la cámara se da por quieta desde hace rato."""
    kwargs.setdefault("min_interval_sec", 100.0)   # que no envíe mientras mide
    kwargs.setdefault("lead_sec", 0.5)             # la anticipación va apagada por defecto
    t, b = make_tracker(**kwargs)
    t._last_send = time.monotonic() - 10
    cx = W / 2 - 30
    for _ in range(5):
        t.on_detections([det(cx=cx, cy=H / 2)], W, H)
        time.sleep(0.05)
        cx += 10
    return t, b

t, _ = walk_right()
check("con la cámara quieta mide la velocidad hacia la derecha",
      t._vx > 50, f"({t._vx:.0f} px/s)")
t._last_send = time.monotonic() - 10
t.cfg = t.cfg.model_copy(update={"min_interval_sec": 0.0})
t.on_detections([det(cx=W / 2 + 10, cy=H / 2)], W, H)   # casi centrado
check("casi centrado no se mueve aunque ande: la zona muerta va sin anticipar",
      t._esphome.commands == [], f"({t._esphome.commands})")

# Fuera de la zona muerta (ex 0.125 > 0.08) sí se anticipa.
OFF = 40
t.on_detections([det(cx=W / 2 + OFF, cy=H / 2)], W, H)
lead_cmd = t._esphome.commands[-1] if t._esphome.commands else None
t0, _ = walk_right(lead_sec=0.0)
t0._last_send = time.monotonic() - 10
t0.cfg = t0.cfg.model_copy(update={"min_interval_sec": 0.0})
t0.on_detections([det(cx=W / 2 + OFF, cy=H / 2)], W, H)
plain_cmd = t0._esphome.commands[-1] if t0._esphome.commands else None
check("descentrado y andando: gira hacia la derecha más que sin anticipar",
      lead_cmd and plain_cmd and lead_cmd[0] * right[0] > 0
      and abs(lead_cmd[0]) > abs(plain_cmd[0]),
      f"({lead_cmd} vs sin anticipar {plain_cmd})")

# Estirar el brazo: solo se mueve el borde derecho, el cuerpo sigue quieto.
t, _ = make_tracker(min_interval_sec=100.0)
t._last_send = time.monotonic() - 10
x2 = W / 2 + 60
for _ in range(5):
    t.on_detections([Detection(x1=W / 2 - 60, y1=100, x2=x2, y2=400, cls=0,
                               label="person", conf=0.9, track_id=1)], W, H)
    time.sleep(0.05)
    x2 += 15
check("una caja que solo se ensancha (brazo) no cuenta como movimiento",
      t._vx == 0.0, f"({t._vx:.0f} px/s)")

t, _ = walk_right()
t._vx_at = time.monotonic() - 2.0          # la última medida es de hace 2 s
t.cfg = t.cfg.model_copy(update={"min_interval_sec": 0.0})
t._last_send = time.monotonic()            # cámara moviéndose: no se mide nada nuevo
t.on_detections([det(cx=W / 2 + OFF, cy=H / 2)], W, H)
expected = -t.cfg.gain * OFF / (W / 2)     # el paso de sin anticipar
check("una velocidad vieja caduca: el paso es el de sin anticipar",
      t._esphome.commands and abs(t._esphome.commands[-1][0] - expected) < 1e-9
      and t._vx == 0.0,
      f"({t._esphome.commands[-1:]}, esperado {expected:.4f}, vx={t._vx})")

t, _ = walk_right()
t._release_target()
t.on_detections([det(cx=W / 2, cy=H / 2, track_id=99)], W, H)
check("y un objetivo nuevo empieza sin velocidad", t._vx == 0.0, f"({t._vx})")

print("\n=== 2. Converge al centro y no se pasa ===")
t, board = make_tracker(gain=0.3, deadzone=0.06)
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
      all(-1.0 <= p <= 1.0 and -1.0 <= ti <= 1.0 for p, ti in board.commands))
steps = [abs(b[0] - a[0]) for a, b in zip(board.commands, board.commands[1:])]
check("cada corrección es menor que la anterior (no oscila)",
      all(b <= a for a, b in zip(steps, steps[1:])), f"({[round(p, 3) for p in steps]})")
check("deja de mandar órdenes antes de agotar los frames (ha convergido)",
      len(board.commands) < FRAMES, f"({len(board.commands)} órdenes en {FRAMES} frames)")

print("\n=== 3. Zona muerta: centrado = no se mueve ===")
t, board = make_tracker(deadzone=0.1)
t.on_detections([det(cx=W / 2 + 2, cy=H / 2 - 2)], W, H)
check("objetivo centrado no genera ninguna orden", board.commands == [], f"({board.commands})")
t.on_detections([det(cx=W * 0.9, cy=H / 2 + 5)], W, H)  # tilt dentro de la zona muerta
check("pero descentrado sí", len(board.commands) == 1)
check("y el eje que ya estaba centrado (tilt) no se toca",
      board.commands[-1][1] == 0.0, f"({board.commands[-1]})")

print("\n=== 4. Rate limit: no una orden por frame ===")
t, board = make_tracker(min_interval_sec=10.0)
for _ in range(5):
    t.on_detections([det(cx=W * 0.9, cy=H / 2)], W, H)
check("5 frames seguidos -> 1 sola orden", len(board.commands) == 1, f"({len(board.commands)})")
t.move_to(0.5, 0.5)
check("el control manual sí salta el rate limit", len(board.commands) == 2)
check("y manda lo pedido", board.commands[-1] == (0.5, 0.5), f"({board.commands[-1]})")

print("\n=== 4a. Velocidad de la placa y corrección desde la orden ===")
# La velocidad solo vive en la placa (number servo_transition); aquí solo se
# muestra en /status.
t, b = make_tracker()
b.set_number("servo_transition", 1.0)
check("/status dice que el valor viene de la placa",
      t.status()["transition_sec"] == 1.0 and t.status()["transition_source"] == "placa",
      f"({t.status()['transition_sec']}, {t.status()['transition_source']})")
t, _ = make_tracker()
check("sin el number en la placa se supone salto directo",
      t.status()["transition_sec"] == 0.0 and t.status()["transition_source"] == "sin dato")
check("y sin suavizado publicado, smoothing_sec es None",
      t.status()["smoothing_sec"] is None, f"({t.status()['smoothing_sec']})")
t, b = make_tracker()
b.set_number("servo_smoothing", 0.03)
check("/status muestra el suavizado de la placa",
      t.status()["smoothing_sec"] == 0.03, f"({t.status()['smoothing_sec']})")

# Con la placa interpolando despacio: corrige desde la ORDEN, no desde la
# posición real (que va a medio camino y desharía la orden).
t, b = make_tracker(gain=0.1)
b.set_number("servo_transition", 2.0)
t.on_detections([det(cx=W * 0.8, cy=H / 2)], W, H)
t.on_detections([det(cx=W * 0.8, cy=H / 2)], W, H)
p1, p2 = b.commands[-2][0], b.commands[-1][0]
check("con transición lenta: incremental sobre la orden anterior",
      abs(p2 - 2 * p1) < 1e-9, f"({p1:.3f}, {p2:.3f})")

t, board = make_tracker(gain=0.2)
t.on_detections([det(cx=W / 2, cy=H * 0.8)], W, H)        # mueve solo el tilt
tilt_moved = board.commands[-1][1]
t.on_detections([det(cx=W * 0.8, cy=H / 2)], W, H)        # ahora solo el pan
check("un eje sin error se queda donde está",
      board.commands[-1][1] == tilt_moved, f"({board.commands[-1]} vs tilt {tilt_moved})")

# Tope por orden: ni con ganancia alta se dan saltos de 0.5.
t, board = make_tracker(gain=0.9)
t.on_detections([det(cx=W * 0.95, cy=H / 2)], W, H)
check("ninguna orden del seguimiento se aleja más de 0.15 de la anterior",
      abs(board.commands[-1][0]) <= 0.15 + 1e-9, f"({board.commands[-1]})")

print("\n=== 4b. Límites de recorrido ===")
t, board = make_tracker()
t.move_to(1.0, -1.0)
check("el manual se recorta a ±0.9 por defecto", board.commands[-1] == (0.9, -0.9),
      f"({board.commands[-1]})")
t, board = make_tracker(pan_limit=0.5, gain=0.5)
for _ in range(20):
    t.on_detections([det(cx=W * 0.95, cy=H / 2)], W, H)
check("el seguimiento no pasa de pan_limit aunque empuje",
      all(abs(p) <= 0.5 + 1e-9 for p, _ in board.commands) and abs(t.pan) == 0.5,
      f"({t.pan})")

print("\n=== 5. Bloqueo de objetivo por track ID ===")
t, board = make_tracker()
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
check("y apunta hacia donde está el suyo", board.commands[-1][0] < board.commands[-2][0],
      f"({board.commands[-2:]})")

print("\n=== 6. Cajas sin track ID confirmado se ignoran ===")
t, board = make_tracker()
t.on_detections([det(cx=W * 0.9, cy=H / 2, track_id=None)], W, H)
check("sin ID no se engancha nada", t.target_id is None)
check("y no se mueve", board.commands == [])

print("\n=== 7. Objetivo perdido: margen y luego suelta ===")
t, board = make_tracker(lost_target_sec=0.3)
t.on_detections([det(cx=W * 0.9, cy=H / 2, track_id=7)], W, H)
check("enganchado", t.target_id == 7)
t.on_detections([], W, H)  # parpadeo del detector
check("no lo suelta al primer frame vacío (puede ser una oclusión)", t.target_id == 7)
t.on_detections([det(cx=W * 0.1, cy=H / 2, track_id=9)], W, H)   # otro, en otro sitio
check("ni se engancha a otro dentro del margen", t.target_id == 7, f"(#{t.target_id})")
time.sleep(0.35)
t.on_detections([det(cx=W * 0.1, cy=H / 2, track_id=9)], W, H)
check("pasado el plazo, engancha el nuevo", t.target_id == 9, f"(#{t.target_id})")

print("\n=== 7b. Reenganche: ByteTrack cambia el ID de la misma persona ===")
t, board = make_tracker()
t.on_detections([det(cx=W * 0.7, cy=H / 2, track_id=1, width=200, height=300)], W, H)
n = len(board.commands)
# Mismo sitio, caja algo deformada (un brazo), ID nuevo.
t.on_detections([det(cx=W * 0.72, cy=H / 2, track_id=3, width=240, height=300)], W, H)
check("mismo sitio con otro ID: reengancha en el acto", t.target_id == 3, f"(#{t.target_id})")
check("y sigue corrigiendo sin esperar", len(board.commands) > n, f"({board.commands})")

t, board = make_tracker()
t.on_detections([det(cx=W * 0.7, cy=H / 2, track_id=1)], W, H)
t.on_detections([det(cx=W * 0.2, cy=H / 2, track_id=3)], W, H)
check("un ID nuevo en otra zona no se reengancha", t.target_id == 1, f"(#{t.target_id})")

t, board = make_tracker()
t.on_detections([det(cx=W * 0.7, cy=H / 2, track_id=1)], W, H)
t.on_detections([det(cx=W * 0.7, cy=H / 2, track_id=3, label="cat")], W, H)
check("otra clase en el mismo sitio no se reengancha", t.target_id == 1, f"(#{t.target_id})")

print("\n=== 8. on_idle (la cámara deja de dar imagen) ===")
t, board = make_tracker(lost_target_sec=0.3)
t.on_detections([det(cx=W * 0.9, cy=H / 2, track_id=3)], W, H)
t.on_idle()
check("no lo suelta enseguida", t.target_id == 3)
time.sleep(0.35)
t.on_idle()
check("pero caduca igual que con imagen", t.target_id is None)

print("\n=== 9. return_home_on_lost ===")
t, board = make_tracker(lost_target_sec=0.1, return_home_on_lost=True, home_pan=0.0, home_tilt=-0.2)
t.on_detections([det(cx=W * 0.9, cy=H / 2, track_id=4)], W, H)
time.sleep(0.15)
t.on_idle()
check("vuelve a reposo al perder el objetivo", board.commands[-1] == (0.0, -0.2),
      f"({board.commands[-1]})")

print("\n=== 10. enabled=False lo deja mudo ===")
t, board = make_tracker(enabled=False)
t.on_detections([det(cx=W * 0.9, cy=H / 2)], W, H)
check("no se mueve", board.commands == [])
check("y no pide inferencia (no calienta la GPU para nada)", t.wants_inference() is False)

print("\n=== 10b. El servicio se coge de la config, no está cableado ===")
# Lo que permite que la pistola (relé de disparo) sea un consumidor nuevo sin
# tocar esphome_api.py: el nombre del servicio lo pone cada variante.
t, board = make_tracker()
t.on_detections([det(cx=W * 0.9, cy=H / 2)], W, H)
check("por defecto llama a set_servo_position",
      board.calls == ["set_servo_position"], f"({board.calls})")

other_board = FakeBoard(service="move_turret")
t_other = ServoTracker(ServoConfig(service="move_turret", min_interval_sec=0.0),
                       other_board, camera_id="test")
t_other.on_detections([det(cx=W * 0.9, cy=H / 2)], W, H)
check("con otro nombre en la config, llama a ese",
      other_board.calls == ["move_turret"], f"({other_board.calls})")
check("y lo reconoce como publicado", t_other.status()["servo_service"] is True)
check("el status dice a cuál llama", t_other.status()["service"] == "move_turret")

print("\n=== 11. status() dice si la placa publica el servicio ===")
cfg = ServoConfig()
t_without = ServoTracker(cfg, FakeBoard(has_servo_service=False), "test")
check("firmware sin servos se ve en status", t_without.status()["servo_service"] is False,
      f"({t_without.status()})")
t_with = ServoTracker(cfg, FakeBoard(has_servo_service=True), "test")
check("y con servos también", t_with.status()["servo_service"] is True)

print("\n=== 12. shutdown() deja la torreta en reposo ===")
t, board = make_tracker(home_pan=0.1, home_tilt=-0.1)
t.on_detections([det(cx=W * 0.9, cy=H / 2)], W, H)
t.shutdown()
check("última orden = posición de reposo", board.commands[-1] == (0.1, -0.1),
      f"({board.commands[-1]})")
check("y suelta el hold: vuelve el auto-detach", board.holds[-1] is False, f"({board.holds})")

print("\n=== 12b. Hold: con el seguimiento activo la placa no suelta los servos ===")
t, board = make_tracker(lost_target_sec=0.2)
t.on_idle()
check("sin objetivo ya se pide hold", board.holds == [True], f"({board.holds})")
check("y status lo dice", t.status()["hold"] is True)
t.on_detections([], W, H)
t.on_detections([det(cx=W / 2, cy=H / 2, track_id=5)], W, H)
check("no se repite en cada frame ni al enganchar", board.holds == [True], f"({board.holds})")
time.sleep(0.25)
t.on_idle()
check("al perder el objetivo NO se suelta", t.target_id is None and board.holds == [True],
      f"({board.holds})")
t._hold_sent_at = time.monotonic() - 10  # como si llevara rato sin refrescar
t.on_idle()
check("se refresca aunque no haya objetivo", board.holds == [True, True], f"({board.holds})")

t.set_config(t.cfg.model_copy(update={"enabled": False}))
check("apagar el seguimiento suelta en el acto",
      board.holds[-1] is False and t.status()["hold"] is False, f"({board.holds})")
n = len(board.holds)
t._hold_sent_at = time.monotonic() - 10
t.on_idle()
t.on_detections([det(cx=W / 2, cy=H / 2)], W, H)
check("apagado no se refresca nada", len(board.holds) == n, f"({board.holds})")
t.set_config(t.cfg.model_copy(update={"enabled": True}))
check("encenderlo vuelve a pedir hold", board.holds[-1] is True, f"({board.holds})")

t, board = make_tracker(enabled=False)
t.on_idle()
check("con el seguimiento apagado desde el inicio no se pide hold", board.holds == [],
      f"({board.holds})")

print("\n=== 13. Frames degenerados no revientan ===")
t, board = make_tracker()
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
import registry  # noqa: E402


class Spy:
    """Consumidor de mentira: apunta lo que le llega desde el hilo de proceso."""

    def __init__(self, wants_inference=True):
        self._wants = wants_inference
        self.frames = 0
        self.idles = 0
        self.sizes: list[tuple[int, int]] = []
        self.closed = False

    def on_detections(self, dets, width, height):
        self.frames += 1
        self.sizes.append((width, height))

    def on_idle(self):
        self.idles += 1

    def wants_inference(self):
        return self._wants

    def status(self):
        return {"frames": self.frames}

    def shutdown(self):
        self.closed = True


def local_mjpeg_server():
    """Igual que en test_shutdown.py: emite un JPEG real sin parar."""
    import cv2
    import numpy as np

    ok, buf = cv2.imencode(".jpg", np.zeros((48, 64, 3), dtype=np.uint8))
    assert ok
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
                    time.sleep(0.1)
            except Exception:
                pass

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


srv, port = local_mjpeg_server()
cfg_cam = camera.CameraConfig(
    camera_id="pipeline", stream_url=f"http://127.0.0.1:{port}/",
    device="cpu", model_name="yolo11n",
    # A false a propósito: así lo ÚNICO que puede estar manteniendo viva la
    # inferencia es el consumidor, que es justo lo que se quiere comprobar. Con
    # el valor por defecto (true) la sesión inferiría igual y el test pasaría
    # aunque el enganche del consumidor estuviera roto.
    always_infer=False,
)
session = camera.CameraSession(cfg_cam)
spy = Spy()
session._consumers.append(spy)
session.start(explicit=True)
time.sleep(3.0)  # sin clientes HTTP: solo el consumidor mantiene la inferencia
check("el consumidor recibe detecciones sin ningún cliente HTTP conectado",
      spy.frames > 0, f"({spy.frames} frames)")
check("y recibe el tamaño real del frame (64x48)",
      spy.sizes and spy.sizes[0] == (64, 48), f"({spy.sizes[:1]})")
check("la inferencia corrió de verdad", session.last_inference_ms is not None,
      f"({session.last_inference_ms})")
check("el estado del consumidor sale en /status",
      session.status().get("consumers", {}).get("Spy") == {"frames": spy.frames},
      f"({session.status().get('consumers')})")
session.shutdown()
check("shutdown() avisa al consumidor", spy.closed)
srv.shutdown()

print("\n=== 15. Sin consumidores, el pipeline se comporta igual que antes ===")
srv2, port2 = local_mjpeg_server()
cfg_alone = camera.CameraConfig(
    camera_id="camera-alone", stream_url=f"http://127.0.0.1:{port2}/",
    device="cpu", model_name="yolo11n",
    # Con always_infer a false, lo único que podría encender la inferencia son
    # los clientes o los consumidores, y aquí no hay ni unos ni otros.
    # (El caso always_infer=True lo cubre test_shutdown.py, caso 3d.)
    always_infer=False,
)
session2 = camera.CameraSession(cfg_alone)
check("una cámara sin sección 'servo' no crea consumidores", session2._consumers == [])
check("ni tracker", session2.servo_tracker is None)
session2.start(explicit=True)
time.sleep(2.0)
check("con always_infer=false, sin clientes ni consumidores no hay inferencia",
      session2.last_inference_ms is None, f"({session2.last_inference_ms})")
check("y sin nadie que lo lea tampoco se codifica ningún JPEG",
      session2.snapshot(infer=False) is None, f"({session2.snapshot(infer=False) is None})")
# Antes aquí se comprobaba lo contrario ("el frame crudo se sigue publicando
# para /snapshot"): se codificaba un JPEG por frame aunque no lo leyera nadie.
# Ahora /snapshot se registra como cliente y fuerza uno recién hecho, que es lo
# que cubre el caso 3d-bis de test_shutdown.py.
session2.shutdown()
srv2.shutdown()

print("\n=== 16. Los endpoints distinguen POR QUÉ no se mueve la torreta ===")
# El caso que más despista es "todo conectado pero el firmware no tiene servos":
# llamar a un servicio que la placa no publica falla en silencio, y desde fuera
# es idéntico a apuntar mal. Cada motivo tiene que dar un error distinto.
from fastapi import HTTPException  # noqa: E402


class FakeEsphome:
    def __init__(self, is_connected=True, has_servo_service=True,
                 service="set_servo_position"):
        self.is_connected = is_connected
        self._services = {service} if has_servo_service else set()
        self.commands = []

    def has_service(self, name):
        return name in self._services

    @property
    def services(self):
        return tuple(self._services)

    def call_service(self, name, /, **args):
        if "hold" in args:
            return True
        self.commands.append((args["pan"], args["tilt"]))
        return True


def fake_session(camera_id, with_servo=True, **board_kw):
    """Sesión registrada en CAMERAS pero SIN arrancar hilos ni tocar el disco."""
    cfg = camera.CameraConfig(
        camera_id=camera_id, stream_url="http://192.0.2.1:8080/",
        device="cpu", model_name="yolo11n",
        servo=ServoConfig() if with_servo else None,
    )
    # Sin noise_psk la sesión no crea EsphomeController ni tracker (y avisa por
    # log de que ignora la sección 'servo': ese aviso en la salida del test es
    # esperado). Se los ponemos a mano con dobles, que es justo lo que queremos
    # controlar aquí; con un noise_psk de verdad arrancaría un hilo intentando
    # conectar contra una IP muerta.
    s = camera.CameraSession(cfg)
    if with_servo:
        s.esphome = FakeEsphome(**board_kw)
        s.servo_tracker = ServoTracker(cfg.servo, s.esphome, camera_id)
        s._consumers = [s.servo_tracker]
    registry.CAMERAS[camera_id] = s  # a mano: register_camera persistiría a disco
    return s


def status_code_of(camera_id):
    try:
        main.get_servo_tracker(camera_id)
        return 200
    except HTTPException as e:
        return e.status_code


fake_session("no-servo", with_servo=False)
check("cámara sin sección 'servo' -> 400", status_code_of("no-servo") == 400,
      f"({status_code_of('no-servo')})")

fake_session("disconnected", is_connected=False)
check("placa desconectada -> 503", status_code_of("disconnected") == 503)

fake_session("no-firmware", has_servo_service=False)
check("firmware sin 'set_servo_position' -> 503", status_code_of("no-firmware") == 503)

s_ok = fake_session("turret-ok")
check("todo en orden -> devuelve el tracker", status_code_of("turret-ok") == 200)
main.get_servo_tracker("turret-ok").move_to(0.25, -0.25)
check("y la orden llega a la placa", s_ok.esphome.commands == [(0.25, -0.25)],
      f"({s_ok.esphome.commands})")

check("cámara inexistente -> 404", status_code_of("missing") == 404)

routes = {r.path for r in main.app.routes}
# La config del servo vive bajo /config/ como el resto de ajustes; /servo a
# secas es el movimiento manual, que es una acción y no configuración.
check("las rutas del servo están registradas",
      {"/cameras/{camera_id}/servo", "/cameras/{camera_id}/config/servo"} <= routes)
check("todos los ajustes cuelgan de /config/",
      {"/cameras/{camera_id}/config/inference",
       "/cameras/{camera_id}/config/stream", "/cameras/{camera_id}/config/servo"} <= routes)
# El keep-alive de GPU se quitó entero: con un modelo ligero no evitaba las
# bajadas a P5 de la GTX 1080, y la solución es un modelo pesado.
check("el keep-alive ya no tiene ruta, ni global ni por cámara",
      not ({"/config/keepalive", "/cameras/{camera_id}/config/keepalive"} & routes))
# gl_keeper: el interruptor de la ventana OpenGL (ver gl_keeper.py).
check("la ruta de gl_keeper está registrada", "/config/gl-keeper" in routes)
check("y ya no quedan las rutas viejas",
      not ({"/cameras/{camera_id}/inference/config", "/cameras/{camera_id}/stream/config",
            "/cameras/{camera_id}/servo/tracking"} & routes))
check("ni las de diagnóstico",
      not ({"/gpu", "/cameras/{camera_id}/selftest",
            "/cameras/{camera_id}/tracker/reset"} & routes))

for cid in ("no-servo", "disconnected", "no-firmware", "turret-ok"):
    registry.CAMERAS.pop(cid, None)

print("\n" + ("TODO OK" if not failures else f"FALLOS: {failures}"))
sys.exit(1 if failures else 0)
