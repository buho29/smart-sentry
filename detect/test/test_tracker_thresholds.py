"""Comprueba que ByteTrack usa las dos confianzas de la cámara, sin GPU.

Alimenta el BYTETracker real de ultralytics con cajas a mano: una caja floja
(entre track_confidence y confidence) puede continuar un track que abrió una
buena, pero no abrir un ID nuevo. Con los umbrales por defecto del yaml (0.25)
sí lo abría, y la torreta se reenganchaba a ruido.

    cd detect
    venv\\Scripts\\python.exe test\\test_tracker_thresholds.py
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import torch  # noqa: E402
from ultralytics.engine.results import Boxes  # noqa: E402
from ultralytics.trackers.byte_tracker import BYTETracker  # noqa: E402
from ultralytics.utils import IterableSimpleNamespace, YAML  # noqa: E402
from ultralytics.utils.checks import check_yaml  # noqa: E402

import camera  # noqa: E402

failures: list[str] = []


def check(name, cond, extra=""):
    print(f"  [{'OK' if cond else 'FALLO'}] {name} {extra}")
    if not cond:
        failures.append(name)


SHAPE = (480, 640)
IMG = np.zeros((*SHAPE, 3), dtype=np.uint8)
CONF, TRACK_CONF = 0.6, 0.25


def boxes(*cajas):
    """(x1, y1, x2, y2, conf) -> lo que model.track le pasa al tracker."""
    data = torch.tensor([[*c, 15.0] for c in cajas], dtype=torch.float32).reshape(-1, 6)
    return Boxes(data, SHAPE).cpu().numpy()


def ids(tracks):
    """track_id -> conf de lo que devuelve el tracker (sin las cajas que suelta)."""
    return {int(t[4]): round(float(t[5]), 2) for t in tracks}


def new_model():
    """Un `model` como el de YOLO tras el primer track(): predictor.trackers."""
    args = IterableSimpleNamespace(**YAML.load(check_yaml("bytetrack.yaml")))
    tracker = BYTETracker(args=args)
    return SimpleNamespace(predictor=SimpleNamespace(trackers=[tracker])), tracker


GATO = (100, 100, 200, 200)
RUIDO = (450, 300, 520, 380)

print("\n=== 1. Umbrales que deja _sync_tracker_thresholds ===")
model, tracker = new_model()
camera._sync_tracker_thresholds(model, CONF, TRACK_CONF)
a = tracker.args
check("alto y de ID nuevo = confidence", a.track_high_thresh == CONF and a.new_track_thresh == CONF)
check("bajo = track_confidence", a.track_low_thresh == TRACK_CONF)
camera._sync_tracker_thresholds(model, 0.3, 0.5)
check("track_confidence por encima: bajo = alto (sin segunda pasada)",
      a.track_low_thresh == a.track_high_thresh == 0.3)
camera._sync_tracker_thresholds(SimpleNamespace(), CONF, TRACK_CONF)
camera._sync_tracker_thresholds(SimpleNamespace(predictor=SimpleNamespace()), CONF, TRACK_CONF)
check("sin predictor o sin trackers (primer frame) no revienta", True)

print("\n=== 2. Con los umbrales del yaml (el fallo) ===")
model, tracker = new_model()
for _ in range(3):
    tracker.update(boxes((*GATO, 0.8)), IMG)
got = {}
for _ in range(3):
    got = ids(tracker.update(boxes((*GATO, 0.8), (*RUIDO, 0.35)), IMG))
check("una floja de ruido abre su propio ID", len(got) == 2, str(got))

print("\n=== 3. Con los umbrales sincronizados ===")
model, tracker = new_model()
camera._sync_tracker_thresholds(model, CONF, TRACK_CONF)
for _ in range(3):
    got = ids(tracker.update(boxes((*GATO, 0.8)), IMG))
check("una buena abre ID", len(got) == 1, str(got))
gato_id = next(iter(got), None)
for _ in range(3):
    got = ids(tracker.update(boxes((*GATO, 0.8), (*RUIDO, 0.35)), IMG))
check("la floja de ruido NO abre ID", list(got) == [gato_id], str(got))
for _ in range(5):
    got = ids(tracker.update(boxes((*GATO, 0.35)), IMG))
check("pero el gato visto flojo conserva su ID", list(got) == [gato_id], str(got))

print("\n" + ("TODO OK" if not failures else f"FALLOS: {failures}"))
sys.exit(1 if failures else 0)
