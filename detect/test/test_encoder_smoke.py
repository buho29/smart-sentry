"""Graba un MP4 de verdad y comprueba que es H.264, no un vídeo en negro.

Es el único test que toca un codificador real, y existe por un motivo muy
concreto: el modo de fallo de la grabación no es una excepción, es un fichero
que pesa lo suyo, que VLC abre y que en Home Assistant se ve en negro. Eso no lo
detecta ningún test con dobles, así que aquí se codifica y se mira el resultado
por dentro.

Si en esta máquina no hay ningún codificador H.264 el test imprime SKIP y sale
con 0: no debe romper por el entorno, pero sí debe dejar dicho que los clips no
se verán en HA.

    cd detect
    venv\\Scripts\\python.exe test\\test_encoder_smoke.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encoders import (  # noqa: E402
    encoder_capabilities, find_ffmpeg, frames_for_gap, resolve_encoder,
)

failures: list[str] = []


def check(name, cond, extra=""):
    print(f"  {'PASS ' if cond else 'FALLO'}  {name} {extra}")
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------------------
print("\n-- frames_for_gap: la corrección de la deriva --")

check("hueco normal = un frame", frames_for_gap(1 / 12, 12) == 1)
check("hueco de medio segundo a 12 fps = 6 frames", frames_for_gap(0.5, 12) == 6,
      f"({frames_for_gap(0.5, 12)})")
check("nunca devuelve menos de 1", frames_for_gap(0.0, 12) == 1)
check("un hueco enorme queda acotado por max_repeat",
      frames_for_gap(120.0, 12, max_repeat=8) == 8)
check("fps inválido no revienta", frames_for_gap(0.5, 0) == 1)
# Redondeo: 0.09 s a 12 fps es algo más de un periodo, pero no dos.
check("redondea al periodo más cercano", frames_for_gap(0.09, 12) == 1,
      f"({frames_for_gap(0.09, 12)})")


# ---------------------------------------------------------------------------
print("\n-- qué se puede codificar en esta máquina --")

caps = encoder_capabilities()
print(f"  ffmpeg:  {caps['ffmpeg']}")
print(f"  encoder: {caps['encoder']}  ({caps['reason']})")

if not caps["h264"]:
    print("\n  SKIP: no hay codificador H.264 en esta máquina.")
    print("  " + caps.get("warning", ""))
    print("\nTODO OK (sin codificar)")
    sys.exit(0)


# ---------------------------------------------------------------------------
print("\n-- codificar un clip de verdad --")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

# 20 frames con un cuadrado que se mueve: si se grabara un solo frame repetido,
# x264 lo comprimiría a casi nada y el tamaño no diría nada útil.
jpegs = []
for i in range(20):
    frame = np.zeros((240, 320, 3), np.uint8)
    cv2.rectangle(frame, (i * 10, 60), (i * 10 + 40, 100), (0, 255, 0), -1)
    ok, buf = cv2.imencode(".jpg", frame)
    jpegs.append(buf.tobytes())

tmp = Path(tempfile.mkdtemp(prefix="enctest-"))
out = tmp / "smoke.mp4"

enc, reason = resolve_encoder("auto")
enc.open(out, 320, 240, 10.0)
for jpg in jpegs:
    enc.write(jpg)
enc.close()

check("el fichero existe y pesa algo", out.is_file() and out.stat().st_size > 0,
      f"({out.stat().st_size if out.is_file() else 0} bytes)")

head = out.read_bytes()[:32]
check("es un MP4 (caja ftyp en la cabecera)", b"ftyp" in head, f"({head[:16]!r})")
# Con +faststart el índice va delante: es lo que permite reproducir en HA sin
# descargar el clip entero.
check("el índice está al principio (faststart)",
      b"moov" in out.read_bytes()[:2048], "")

# La comprobación que de verdad importa: el códec. 'avc1' en la cabecera es
# H.264; 'mp4v' sería MPEG-4 Part 2, que se ve en negro en el navegador.
data = out.read_bytes()
check("el códec es H.264 (avc1), no mp4v",
      b"avc1" in data and b"mp4v" not in data[:4096],
      f"(avc1={b'avc1' in data}, mp4v={b'mp4v' in data[:4096]})")

ffprobe = shutil.which("ffprobe")
if ffprobe:
    info = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,pix_fmt,width,height,nb_frames",
         "-of", "default=nw=1", str(out)],
        capture_output=True, text=True).stdout
    print("  ffprobe:", " ".join(info.split()))
    check("ffprobe confirma h264", "codec_name=h264" in info)
    # Sin yuv420p el clip tampoco se ve, y es el fallo más sutil de los dos.
    check("y pixel format yuv420p", "pix_fmt=yuv420p" in info)
else:
    print("  (sin ffprobe: no se puede verificar el pix_fmt, solo la cabecera)")

print(f"\n  Clip de prueba en: {out}")
print("  Ábrelo en Chrome: si se ve el cuadrado moverse, HA también lo reproducirá.")

print("\n" + ("TODO OK" if not failures else f"FALLOS: {failures}"))
sys.exit(1 if failures else 0)
