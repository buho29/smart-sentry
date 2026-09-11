"""Comprueba POST /cameras/start y /cameras/stop: arrancar/parar todas de golpe.

No arranca hilos de verdad: se sustituyen start()/stop() de cada sesión por
contadores, así que no hace falta placa ni GPU.

    cd detect
    venv\\Scripts\\python.exe test\\test_start_stop_all.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import camera  # noqa: E402
import main as _main  # noqa: E402
import registry as _registry  # noqa: E402

failures: list[str] = []


def check(name, cond, extra=""):
    print(f"  {'PASS ' if cond else 'FALLO'}  {name} {extra}")
    if not cond:
        failures.append(name)


_registry.CAMERAS_CONFIG_FILE = Path(tempfile.mkdtemp()) / "cameras_config.json"
_registry.CAMERAS.clear()
client = TestClient(_main.app)

calls: dict[str, list] = {"start": [], "stop": []}
sessions = []
for cid in ("a", "b"):
    s = _registry.register_camera(camera.CameraConfig(
        camera_id=cid, stream_url="http://192.0.2.1:8080/", device="cpu", model_name="yolo11n"))
    s.start = (lambda cid: lambda explicit=False: calls["start"].append((cid, explicit)))(cid)
    s.stop = (lambda cid: lambda explicit=False: calls["stop"].append((cid, explicit)))(cid)
    sessions.append(s)

print("\n=== POST /cameras/start ===")
r = client.post("/cameras/start")
check("200", r.status_code == 200, r.text[:120])
check("start(explicit=True) en todas", sorted(calls["start"]) == [("a", True), ("b", True)], str(calls["start"]))
check("devuelve un status por cámara", sorted(x["camera_id"] for x in r.json()) == ["a", "b"])
check("no toca el endpoint individual", "/cameras/{camera_id}/start" in {r.path for r in _main.app.routes})

print("\n=== POST /cameras/stop ===")
r = client.post("/cameras/stop")
check("200", r.status_code == 200, r.text[:120])
check("stop(explicit=True) en todas", sorted(calls["stop"]) == [("a", True), ("b", True)], str(calls["stop"]))
check("devuelve un status por cámara", sorted(x["camera_id"] for x in r.json()) == ["a", "b"])

print("\n=== Las rutas no se comen a las individuales ===")
r = client.post("/cameras/a/start")
check("/cameras/a/start sigue yendo a la individual", r.status_code == 200 and r.json()["camera_id"] == "a"
      and calls["start"].count(("a", True)) == 2)
r = client.post("/cameras/noexiste/start")
check("404 en la individual con id inexistente", r.status_code == 404)

_registry.CAMERAS.clear()
print()
if failures:
    print(f"{len(failures)} FALLO(S): {failures}")
    sys.exit(1)
print("todo OK")
