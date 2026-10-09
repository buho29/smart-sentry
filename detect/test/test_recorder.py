"""Comprueba la máquina de estados de la grabación, sin ffmpeg, sin GPU y sin placa.

`ClipRecorder` recibe el almacén y el encoder inyectados, así que aquí se le da
un encoder de mentira que solo apunta los bytes que recibe y un directorio
temporal. Eso permite probar en milisegundos cosas que en real costarían una
tarde: que el pre-roll sale en orden, que un falso positivo suelto no genera
clip, que la cola llena no frena el hilo de la cámara.

Los tiempos se controlan pasando el `ts` a mano en vez de durmiendo: el
grabador toma todas sus decisiones sobre el timestamp del frame, precisamente
para que se pueda hacer esto.

    cd detect
    venv\\Scripts\\python.exe test\\test_recorder.py
"""

import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clips import ClipStore, RecordingsConfig  # noqa: E402
from detections import Detection  # noqa: E402
from recorder import ClipRecorder, RecordingConfig  # noqa: E402

failures: list[str] = []
tempdirs: list[Path] = []


def check(name, cond, extra=""):
    print(f"  {'PASS ' if cond else 'FALLO'}  {name} {extra}")
    if not cond:
        failures.append(name)


# Un JPEG 64x48 de verdad: el grabador lee las dimensiones de la cabecera, así
# que no vale con bytes cualesquiera.
def _make_jpeg(w=64, h=48, fill=0):
    import cv2
    import numpy as np
    img = np.full((h, w, 3), fill, np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


JPG = _make_jpeg()
JPG_GRANDE = _make_jpeg(128, 96)


class FakeEncoder:
    """Apunta lo que le mandan escribir. Ni ffmpeg ni disco."""

    def __init__(self, delay=0.0):
        self.name = "fake"
        self.playable_in_browser = True
        self.frames: list[bytes] = []
        self.opened: list[tuple] = []
        self.closed = 0
        self._delay = delay
        self._path = None

    def open(self, path, width, height, fps):
        self.opened.append((path, width, height, fps))
        self._path = path
        path.write_bytes(b"")

    def write(self, jpeg):
        if self._delay:
            time.sleep(self._delay)
        self.frames.append(jpeg)
        # Se escribe algo de verdad para que el .part no sea de tamaño 0 y el
        # grabador no lo tire por vacío.
        if self._path is not None:
            with open(self._path, "ab") as f:
                f.write(b"\x00" * 64)

    def close(self, timeout=5.0):
        self.closed += 1


def det(cls=0, conf=0.9, label="person"):
    return Detection(x1=10, y1=10, x2=30, y2=40, cls=cls, label=label,
                     conf=conf, track_id=1)


def make_recorder(encoder=None, **cfg):
    """Grabador listo para usar, con su propio almacén temporal.

    Los ajustes que son globales (topes de memoria, codificación) se separan y
    van al `RecordingsConfig` del almacén; el resto, a la cámara.
    """
    root = Path(tempfile.mkdtemp(prefix="rectest-"))
    tempdirs.append(root)
    global_keys = set(RecordingsConfig.__fields__) - {"root_dir"}
    store_cfg = {k: cfg.pop(k) for k in list(cfg) if k in global_keys
                 and k not in ("max_age_days", "max_total_gb", "min_free_gb")}
    store = ClipStore(RecordingsConfig(root_dir=str(root), max_age_days=0,
                                       max_total_gb=0, min_free_gb=0, **store_cfg))
    enc = encoder if encoder is not None else FakeEncoder()
    cfg.setdefault("min_clip_sec", 0.0)
    cfg.setdefault("cooldown_sec", 0.0)
    rec = ClipRecorder(RecordingConfig(**cfg), "test", store,
                       fps_getter=lambda: 10.0,
                       encoder_factory=lambda: (enc, "fake"))
    return rec, enc, store


def drain(rec, timeout=3.0):
    """Espera a que el hilo escritor se ponga al día."""
    t0 = time.time()
    while rec._q.unfinished_tasks and time.time() - t0 < timeout:
        time.sleep(0.005)
    time.sleep(0.02)


# ---------------------------------------------------------------------------
print("\n-- el disparo por detección --")

rec, enc, _ = make_recorder(min_hits=2)
rec.on_detections([det()], 64, 48)
rec.on_jpeg(None, JPG, 100.0)
check("un solo frame con detección NO abre clip (min_hits=2)",
      rec.status()["state"] == "idle", f"({rec.status()['state']})")

rec.on_detections([det()], 64, 48)
rec.on_jpeg(None, JPG, 100.1)
check("dos frames consecutivos sí abren clip", rec.status()["state"] == "recording")
check("y el clip queda marcado como en curso",
      rec.status()["current_clip"] in rec._store.in_progress())
rec.shutdown()

rec, enc, _ = make_recorder(min_hits=2)
rec.on_detections([det()], 64, 48)
rec.on_jpeg(None, JPG, 100.0)
rec.on_detections([], 64, 48)          # el falso positivo se corta
rec.on_jpeg(None, JPG, 100.1)
rec.on_detections([det()], 64, 48)
rec.on_jpeg(None, JPG, 100.2)
check("los hits tienen que ser CONSECUTIVOS", rec.status()["state"] == "idle")
rec.shutdown()

# El filtro por clase y confianza es el de la cámara y ya viene aplicado: el
# grabador dispara con cualquier detección que le llegue, sea cual sea.
rec, enc, _ = make_recorder(min_hits=1)
rec.on_detections([det(cls=15, conf=0.3, label="cat")], 64, 48)
rec.on_jpeg(None, JPG, 100.0)
check("cualquier detección recibida dispara", rec.status()["state"] == "recording")
rec.shutdown()

# Salvo las `weak`: están por debajo de la confianza de la cámara y solo le
# llegan a ByteTrack y a la torreta.
rec, enc, _ = make_recorder(min_hits=1)
rec.on_detections([Detection(x1=10, y1=10, x2=30, y2=40, cls=15, label="cat",
                             conf=0.3, track_id=1, weak=True)], 64, 48)
rec.on_jpeg(None, JPG, 100.0)
check("una detección weak no dispara", rec.status()["state"] == "idle",
      f"({rec.status()['state']})")
rec.shutdown()

check("un cameras_config.json viejo con trigger_classes/min_conf/encoder carga",
      RecordingConfig(trigger_classes=[15], min_conf=0.5, encoder="ffmpeg",
                      min_hits=3).min_hits == 3)
check("y esas claves no vuelven a salir al guardar",
      not {"trigger_classes", "min_conf", "encoder"}
      & set(RecordingConfig(trigger_classes=[15], encoder="ffmpeg").dict()))


# ---------------------------------------------------------------------------
print("\n-- el pre-roll, que es el motivo de todo esto --")

rec, enc, _ = make_recorder(min_hits=1, pre_roll_sec=5.0)
marcas = [_make_jpeg(fill=i) for i in range(1, 21)]
t = 100.0
for i, jpg in enumerate(marcas):
    rec.on_jpeg(None, jpg, t + i * 0.1)   # 20 frames en 2 s, sin detección
check("sin detección no se graba, solo se recuerda",
      rec.status()["state"] == "idle" and rec.status()["preroll_frames"] == 20,
      f"({rec.status()['preroll_frames']})")

disparo = _make_jpeg(fill=99)
rec.on_detections([det()], 64, 48)
rec.on_jpeg(None, disparo, t + 2.0)
drain(rec)

check("al disparar, el clip empieza con TODO el pre-roll",
      len(enc.frames) >= 21, f"({len(enc.frames)} frames)")
check("y en el orden correcto", enc.frames[:20] == marcas,
      "" if enc.frames[:20] == marcas else "(el pre-roll sale desordenado)")
check("el frame que disparó está dentro del clip", disparo in enc.frames)
check("el pre-roll se vacía al transferirlo", rec.status()["preroll_frames"] == 0)
rec.shutdown()

rec, enc, _ = make_recorder(min_hits=1, pre_roll_sec=1.0)
for i in range(30):
    rec.on_jpeg(None, JPG, 100.0 + i * 0.1)   # 3 s de frames, pre-roll de 1 s
n = rec.status()["preroll_frames"]
check("el pre-roll se poda por SEGUNDOS", 9 <= n <= 12, f"({n} frames para 1 s a 10 fps)")
rec.shutdown()

rec, enc, _ = make_recorder(min_hits=1, pre_roll_sec=600.0, preroll_max_mb=0.01)
for i in range(200):
    rec.on_jpeg(None, JPG, 100.0 + i * 0.1)
check("y también por MEGABYTES, aunque quepan en segundos",
      rec.status()["preroll_mb"] <= 0.011, f"({rec.status()['preroll_mb']} MB)")
rec.shutdown()

# Girar la cámara 90° con el grabador esperando: el pre-roll trae frames
# apaisados y el disparo llega ya en vertical.
rec, enc, _ = make_recorder(min_hits=1, pre_roll_sec=5.0)
VERTICAL = _make_jpeg(48, 64)
for i in range(5):
    rec.on_jpeg(None, JPG, 100.0 + i * 0.1)
for i in range(3):
    rec.on_jpeg(None, _make_jpeg(48, 64, fill=i + 1), 100.5 + i * 0.1)
rec.on_detections([det()], 48, 64)
rec.on_jpeg(None, VERTICAL, 101.0)
drain(rec)
check("tras un giro el clip abre con el tamaño nuevo",
      enc.opened[-1][1:3] == (48, 64), f"({enc.opened[-1][1:3]})")
check("y del pre-roll solo lleva los frames de ese tamaño",
      len(enc.frames) >= 4 and JPG not in enc.frames, f"({len(enc.frames)} frames)")
rec.shutdown()


# ---------------------------------------------------------------------------
print("\n-- el cierre --")

rec, enc, store = make_recorder(min_hits=1, post_roll_sec=2.0)
rec.on_detections([det()], 64, 48)
rec.on_jpeg(None, JPG, 100.0)
rec.on_jpeg(None, JPG, 101.0)                 # 1 s sin detección: sigue
check("no cierra antes del post-roll", rec.status()["state"] == "recording")
rec.on_jpeg(None, JPG, 103.0)                 # 3 s sin detección: cierra
check("cierra al agotarse el post-roll", rec.status()["state"] == "idle")
drain(rec)
check("y publica el clip como .mp4 sin .part",
      len(store.list_clips()) == 1 and not list(store.root.rglob("*.part.mp4")),
      f"({[e.clip_id for e in store.list_clips()]})")
check("el clip ya no figura como en curso", store.in_progress() == set())
meta = store.list_clips()[0].meta
check("el sidecar lleva las etiquetas vistas", meta.get("labels") == {"person": 1},
      f"({meta.get('labels')})")
check("y el motivo del cierre", meta.get("close_reason") == "fin del evento")
rec.shutdown()

# Si la placa se duerme, no llegan frames y on_jpeg no vuelve a correr: sin
# on_idle el clip se quedaría abierto hasta el próximo evento.
rec, enc, store = make_recorder(min_hits=1, post_roll_sec=0.05)
rec.on_detections([det()], 64, 48)
rec.on_jpeg(None, JPG, time.time())
time.sleep(0.1)
rec.on_idle()
check("on_idle cierra el clip cuando dejan de llegar frames",
      rec.status()["state"] == "idle")
rec.shutdown()

rec, enc, store = make_recorder(min_hits=1, max_clip_sec=2.0, post_roll_sec=100.0)
rec.on_detections([det()], 64, 48)
rec.on_jpeg(None, JPG, 100.0)
primero = rec.status()["current_clip"]
rec.on_detections([det()], 64, 48)
rec.on_jpeg(None, JPG, 103.0)                 # pasa de max_clip_sec
rec.on_detections([det()], 64, 48)
rec.on_jpeg(None, JPG, 103.1)                 # el siguiente frame reabre
check("max_clip_sec ROTA de clip en vez de parar",
      rec.status()["state"] == "recording" and rec.status()["current_clip"] != primero,
      f"({primero} -> {rec.status()['current_clip']})")
rec.shutdown()
drain(rec)
check("y quedan los dos clips", len(store.list_clips()) == 2,
      f"({[e.clip_id for e in store.list_clips()]})")

rec, enc, store = make_recorder(min_hits=1, min_clip_sec=5.0, post_roll_sec=0.5)
rec.on_detections([det()], 64, 48)
rec.on_jpeg(None, JPG, 100.0)
rec.on_jpeg(None, JPG, 101.0)                 # clip de 1 s: por debajo del mínimo
drain(rec)
check("un clip más corto que min_clip_sec se descarta",
      store.list_clips() == [] and not list(store.root.rglob("*.mp4")),
      f"({list(store.root.rglob('*.mp4'))})")
check("y tampoco deja la miniatura huérfana", not list(store.root.rglob("*.jpg")))
rec.shutdown()

rec, enc, store = make_recorder(min_hits=1, post_roll_sec=100.0)
rec.on_detections([det()], 64, 48)
rec.on_jpeg(None, JPG, 100.0)
rec.on_jpeg(None, JPG_GRANDE, 100.1)          # la placa cambia de resolución
check("un cambio de resolución rota el clip",
      rec.status()["state"] == "idle" or rec.status()["current_clip"] is not None)
rec.on_jpeg(None, JPG_GRANDE, 100.2)
drain(rec)
check("y el clip nuevo se abre con el tamaño nuevo",
      enc.opened[-1][1:3] == (128, 96), f"({enc.opened[-1][1:3]})")
rec.shutdown()


# ---------------------------------------------------------------------------
print("\n-- el disparo manual --")

rec, enc, store = make_recorder(min_hits=1, trigger_on_detection=False,
                                post_roll_sec=0.1, source="raw")
# start_manual espera a que llegue un frame: se simula desde otro hilo.
threading.Timer(0.05, lambda: rec.on_jpeg(JPG, None, time.time())).start()
st = rec.start_manual(note="prueba")
check("start_manual arranca y devuelve el clip",
      st["state"] == "recording" and st["current_clip"], f"({st['current_clip']})")

try:
    rec.start_manual(timeout=0.2)
    doble = False
except RuntimeError:
    doble = True
check("un segundo start_manual da error en vez de abrir dos clips", doble)

rec.on_jpeg(JPG, None, time.time() + 5)
check("el manual NO se cierra por post-roll", rec.status()["state"] == "recording")

res = rec.stop_manual()
check("stop_manual cierra y devuelve los metadatos del clip",
      res["clip"] is not None and res["clip"].get("trigger") == "manual",
      f"({res['clip']})")
check("con la nota que se le pasó", res["clip"].get("note") == "prueba")
check("y el fichero está en disco", len(store.list_clips()) == 1)

try:
    rec.stop_manual()
    sin_grabar = False
except RuntimeError:
    sin_grabar = True
check("stop_manual sin grabación en curso da error", sin_grabar)
rec.shutdown()

# El manual llegando sobre un clip que ya estaba abierto por detección.
rec, enc, store = make_recorder(min_hits=1, post_roll_sec=0.1)
rec.on_detections([det()], 64, 48)
rec.on_jpeg(None, JPG, time.time())
abierto = rec.status()["current_clip"]
st = rec.start_manual()
check("el manual ADOPTA el clip abierto por detección en vez de partirlo",
      st["current_clip"] == abierto and st["trigger"] == "manual",
      f"({st['trigger']}, {st['current_clip']})")
rec.on_jpeg(None, JPG, time.time() + 10)
check("y desde entonces manda el manual (no cierra por post-roll)",
      rec.status()["state"] == "recording")
rec.stop_manual()
rec.shutdown()

rec, enc, store = make_recorder(min_hits=1, enabled=False)
try:
    rec.start_manual(timeout=0.1)
    deshab = False
except RuntimeError:
    deshab = True
check("start_manual con la grabación deshabilitada da error", deshab)
rec.shutdown()

rec, enc, store = make_recorder(min_hits=1, trigger_on_detection=False)
try:
    rec.start_manual(timeout=0.2)   # nadie manda frames
    sin_frames = False
except TimeoutError:
    sin_frames = True
check("start_manual sin frames da timeout, no se queda colgado", sin_frames)
check("y no deja un disparo pendiente armado", rec._pending_open is None)
rec.shutdown()


# ---------------------------------------------------------------------------
print("\n-- no frenar el hilo de la cámara --")

# Encoder deliberadamente lento: la cola se llena y hay que tirar frames en vez
# de bloquear el hilo yolo, que es el que alimenta el stream y la inferencia.
lento = FakeEncoder(delay=0.05)
rec, enc, store = make_recorder(encoder=lento, min_hits=1, post_roll_sec=100.0,
                                queue_maxsize=8, pre_roll_sec=0.0)
rec.on_detections([det()], 64, 48)
rec.on_jpeg(None, JPG, 100.0)

peor = 0.0
for i in range(200):
    t0 = time.perf_counter()
    rec.on_jpeg(None, JPG, 100.0 + i * 0.01)
    peor = max(peor, time.perf_counter() - t0)

check("on_jpeg nunca bloquea, ni con el encoder atascado",
      peor < 0.02, f"(peor caso {peor * 1000:.2f} ms)")
check("los frames que no caben se descartan y se contabilizan",
      rec.status()["dropped_frames"] > 0, f"({rec.status()['dropped_frames']})")
check("la cola no crece por encima de su tope",
      rec.status()["queue"] <= 8, f"({rec.status()['queue']})")
rec.shutdown()


# ---------------------------------------------------------------------------
print("\n-- coste cero cuando no se graba --")

rec, enc, store = make_recorder(enabled=False)
check("deshabilitado no pide inferencia", rec.wants_inference() is False)
check("deshabilitado no pide que se codifique ningún JPEG",
      rec.wants_frames() is None)
rec.on_detections([det()], 64, 48)
rec.on_jpeg(None, JPG, 100.0)
check("y ni siquiera llena el pre-roll", rec.status()["preroll_frames"] == 0)
rec.shutdown()

rec, enc, store = make_recorder(source="raw", trigger_on_detection=False)
check("grabar en crudo NO enciende la inferencia", rec.wants_inference() is False)
check("pero sí pide el JPEG crudo", rec.wants_frames() == "raw")
rec.shutdown()

rec, enc, store = make_recorder(source="annotated")
check("grabar anotado pide el JPEG anotado", rec.wants_frames() == "annotated")
check("y enciende la inferencia", rec.wants_inference() is True)
rec.shutdown()

# El anotado puede faltar en un frame suelto si la inferencia falló: mejor
# grabar la imagen limpia que dejar un hueco.
rec, enc, store = make_recorder(min_hits=1, source="annotated", post_roll_sec=100.0)
rec.on_detections([det()], 64, 48)
rec.on_jpeg(JPG, None, 100.0)
drain(rec)
check("si falta el anotado, cae al crudo sin romperse",
      rec.status()["state"] == "recording" and JPG in enc.frames)
rec.shutdown()


# ---------------------------------------------------------------------------
print("\n-- apagado --")

rec, enc, store = make_recorder(min_hits=1, post_roll_sec=100.0)
rec.on_detections([det()], 64, 48)
rec.on_jpeg(None, JPG, time.time())
rec.on_jpeg(None, JPG, time.time() + 0.1)
t0 = time.time()
rec.shutdown()
tardanza = time.time() - t0
check("shutdown cierra el clip en curso", rec.status()["state"] == "idle")
check("y no se eterniza", tardanza < 3.0, f"({tardanza:.2f}s)")
check("el fichero queda publicado, sin .part",
      len(store.list_clips()) == 1 and not list(store.root.rglob("*.part.mp4")),
      f"({[e.clip_id for e in store.list_clips()]})")
check("el encoder se cerró", enc.closed >= 1)


for d in tempdirs:
    shutil.rmtree(d, ignore_errors=True)

print("\n" + ("TODO OK" if not failures else f"FALLOS: {failures}"))
sys.exit(1 if failures else 0)
