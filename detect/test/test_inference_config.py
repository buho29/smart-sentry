"""Comprueba POST /cameras/{id}/config/inference con body JSON: lo que no se
envía se conserva, y /openapi.json lleva los valores actuales de cada cámara
como ejemplos para que Swagger los muestre.

No necesita placa ni GPU: las sesiones nacen paradas y aquí nunca se arrancan.

    cd detect
    venv\\Scripts\\python.exe test\\test_inference_config.py
"""

import json
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


# Que el test no pise el cameras_config.json real
_registry.CAMERAS_CONFIG_FILE = Path(tempfile.mkdtemp()) / "cameras_config.json"
_registry.CAMERAS.clear()

client = TestClient(_main.app)
URL = "/cameras/cam/config/inference"


def fresh(camera_id="cam"):
    _registry.CAMERAS.pop(camera_id, None)
    cfg = camera.CameraConfig(
        camera_id=camera_id, stream_url="http://192.0.2.1:8080/",
        device="cpu", model_name="yolo11n", confidence=0.4, imgsz=320,
        classes=[0, 16], always_infer=False,
    )
    return _registry.register_camera(cfg)


print("\n=== 1. Body vacío no cambia nada ===")
s = fresh()
r = client.post(URL, json={})
check("200", r.status_code == 200, r.text[:120])
check("no relaunched", r.json()["relaunched"] is False)
check("cfg intacta", s.cfg.confidence == 0.4 and s.cfg.classes == [0, 16] and s.cfg.always_infer is False)

print("\n=== 2. Cambiar un campo conserva el resto ===")
s = fresh()
r = client.post(URL, json={"confidence": 0.9})
check("200", r.status_code == 200, r.text[:120])
check("confidence actualizada", s.cfg.confidence == 0.9)
check("imgsz intacto", s.cfg.imgsz == 320)
check("classes intactas", s.cfg.classes == [0, 16])
check("always_infer intacto", s.cfg.always_infer is False)
check("misma sesión, no relanzada", _registry.CAMERAS["cam"] is s and r.json()["relaunched"] is False)
saved = json.loads(_registry.CAMERAS_CONFIG_FILE.read_text())
check("persistido a disco", any(c["camera_id"] == "cam" and c["confidence"] == 0.9 for c in saved))

print("\n=== 3. Reenviar la config entera (el ejemplo de Swagger) no relanza ===")
s = fresh()
full = s.cfg.model_dump(include=set(_main.InferenceConfig.model_fields))
check("el ejemplo cubre todos los campos", set(full) == {"confidence", "imgsz", "always_infer", "classes", "model_name", "device"}, str(full))
r = client.post(URL, json=full)
check("200", r.status_code == 200, r.text[:120])
check("no relaunched", r.json()["relaunched"] is False)

print("\n=== 4. classes: null = todas; null/vacío en el resto = no tocar ===")
s = fresh()
r = client.post(URL, json={"classes": None, "confidence": None, "model_name": ""})
check("200", r.status_code == 200, r.text[:120])
check("classes -> None", s.cfg.classes is None)
check("confidence no toca", s.cfg.confidence == 0.4)
check("model_name no toca", s.cfg.model_name == "yolo11n")

print("\n=== 5. device inválido -> 400 sin tocar nada ===")
s = fresh()
r = client.post(URL, json={"device": "tpu", "confidence": 0.99})
check("400", r.status_code == 400, r.text)
check("confidence no cambió", s.cfg.confidence == 0.4)

print("\n=== 6. 404 si no existe ===")
r = client.post("/cameras/noexiste/config/inference", json={"confidence": 0.5})
check("404", r.status_code == 404, r.text)

print("\n=== 7. /openapi.json lleva un ejemplo por cámara con sus valores ===")
fresh()
fresh("otra").cfg.confidence = 0.7


def examples():
    spec = client.get("/openapi.json").json()
    body = spec["paths"]["/cameras/{camera_id}/config/inference"]["post"]["requestBody"]
    return body["content"]["application/json"].get("examples", {})


ex = examples()
check("un ejemplo por cámara", set(ex) == {"cam", "otra"}, str(list(ex)))
check("con los valores reales", ex.get("otra", {}).get("value", {}).get("confidence") == 0.7
      and ex.get("cam", {}).get("value", {}).get("classes") == [0, 16])
check("solo campos de inferencia", "stream_url" not in ex.get("cam", {}).get("value", {}))
_registry.CAMERAS.pop("otra", None)
check("se regenera en cada carga", set(examples()) == {"cam"})
_registry.CAMERAS.pop("cam", None)

print()
if failures:
    print(f"{len(failures)} FALLO(S): {failures}")
    sys.exit(1)
print("todo OK")
