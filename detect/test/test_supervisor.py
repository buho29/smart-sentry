"""Comprueba supervisor.py: start / stop / restart / status y el watchdog.

No lanza uvicorn de verdad (cargar torch tarda mucho): el "servicio" es un
script Python de mentira que duerme y sale limpio con el mismo Ctrl+Break que
el supervisor manda a uvicorn.

    cd detect
    venv\\Scripts\\python.exe test\\test_supervisor.py
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import supervisor  # noqa: E402

failures: list[str] = []


def check(name, cond, extra=""):
    print(f"  {'PASS ' if cond else 'FALLO'}  {name} {extra}")
    if not cond:
        failures.append(name)


def wait_until(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


# Hijo de mentira: sale con 0 ante SIGINT/SIGBREAK, como haría uvicorn.
FAKE = Path(tempfile.mkdtemp()) / "fake_service.py"
FAKE.write_text(
    "import signal, sys, time\n"
    "def bye(*_): sys.exit(0)\n"
    "for s in ('SIGINT', 'SIGTERM', 'SIGBREAK'):\n"
    "    if hasattr(signal, s): signal.signal(getattr(signal, s), bye)\n"
    "while True: time.sleep(0.1)\n"
)

supervisor.AUTOSTART = False
supervisor.RELAUNCH_BACKOFF_SEC = (0.2, 0.2)
svc = supervisor.service = supervisor.Service([sys.executable, str(FAKE)], FAKE.parent)

with TestClient(supervisor.app) as client:
    print("\n=== status sin proceso ===")
    r = client.get("/service/status")
    check("200", r.status_code == 200, r.text[:120])
    check("running=false", r.json()["running"] is False)
    check("manual_stop=false", r.json()["manual_stop"] is False)

    print("\n=== POST /service/start ===")
    r = client.post("/service/start")
    check("launched=true", r.json()["launched"] is True, r.text[:120])
    check("running=true con pid", r.json()["running"] is True and r.json()["pid"])
    pid1 = r.json()["pid"]
    r = client.post("/service/start")
    check("segundo start no relanza", r.json()["launched"] is False and r.json()["pid"] == pid1)

    print("\n=== POST /service/stop ===")
    time.sleep(0.5)  # que el hijo tenga sus handlers puestos, como uvicorn ya arrancado
    t0 = time.monotonic()
    r = client.post("/service/stop")
    check("stopped=true", r.json()["stopped"] is True, r.text[:120])
    check("running=false", r.json()["running"] is False)
    check("manual_stop=true", r.json()["manual_stop"] is True)
    check("exit 0 (parada ordenada, no terminate)", r.json()["last_exit_code"] == 0, str(r.json()["last_exit_code"]))
    check("murió en < 3s", time.monotonic() - t0 < 3, f"{time.monotonic() - t0:.1f}s")
    r = client.post("/service/stop")
    check("segundo stop no hace nada", r.json()["stopped"] is False)

    print("\n=== watchdog: con manual_stop no relanza ===")
    time.sleep(1.5)
    check("sigue parado", client.get("/service/status").json()["running"] is False)

    print("\n=== watchdog: un crash sí se relanza ===")
    r = client.post("/service/start")
    pid2 = r.json()["pid"]
    check("manual_stop=false tras start", r.json()["manual_stop"] is False)
    subprocess.run(["taskkill", "/F", "/PID", str(pid2)], capture_output=True) if sys.platform == "win32" \
        else svc._proc.kill()
    check("el watchdog lo relanza", wait_until(lambda: svc.running and svc.pid != pid2, timeout=8),
          str(svc.status()))
    check("last_exit_code del crash != 0", svc.last_exit_code not in (None, 0), str(svc.last_exit_code))

    print("\n=== POST /service/restart ===")
    pid3 = svc.pid
    r = client.post("/service/restart")
    check("running=true", r.json()["running"] is True, r.text[:120])
    check("pid distinto", r.json()["pid"] != pid3, f"{pid3} -> {r.json()['pid']}")
    client.post("/service/stop")
    r = client.post("/service/restart")
    check("restart con manual_stop lo levanta y limpia manual_stop",
          r.json()["running"] is True and r.json()["manual_stop"] is False)

    print("\n=== hijo que ignora la señal: se mata el árbol entero ===")
    # venv\Scripts\python.exe es un lanzador: el intérprete real es SU hijo,
    # y es ese pid (el que escribe el script) el que no puede quedar huérfano.
    client.post("/service/stop")
    STUBBORN = FAKE.with_name("stubborn.py")
    PIDFILE = FAKE.with_name("stubborn.pid")
    STUBBORN.write_text(
        "import os, signal, time\n"
        f"open(r'{PIDFILE}', 'w').write(str(os.getpid()))\n"
        "for s in ('SIGINT', 'SIGTERM', 'SIGBREAK'):\n"
        "    if hasattr(signal, s): signal.signal(getattr(signal, s), lambda *_: None)\n"
        "while True: time.sleep(0.1)\n"
    )
    svc.cmd = [sys.executable, str(STUBBORN)]
    supervisor.STOP_TIMEOUT_SEC = 1.0
    client.post("/service/start")
    check("el hijo real arrancó", wait_until(PIDFILE.exists, timeout=5))
    time.sleep(0.5)
    real_pid = int(PIDFILE.read_text())
    t0 = time.monotonic()
    r = client.post("/service/stop")
    check("stopped=true pese a ignorar la señal", r.json()["stopped"] is True and r.json()["running"] is False)
    check("tardó ~STOP_TIMEOUT", 0.8 < time.monotonic() - t0 < 6, f"{time.monotonic() - t0:.1f}s")

    def alive(pid):
        if sys.platform == "win32":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
            return str(pid) in out
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    check("el intérprete real (nieto) también murió", wait_until(lambda: not alive(real_pid), timeout=5), str(real_pid))
    svc.cmd = [sys.executable, str(FAKE)]
    supervisor.STOP_TIMEOUT_SEC = 12.0
    client.post("/service/start")

print("\n=== al cerrar el supervisor muere el hijo ===")
check("running=false tras el lifespan", wait_until(lambda: not svc.running, timeout=5))

print("\n=== log a fichero: supervisor + salida del hijo, con rotación ===")
LOGDIR = Path(tempfile.mkdtemp())
LOGFILE = LOGDIR / "supervisor.log"
supervisor.log.enable_file(LOGFILE, max_bytes=4096, backups=2)


def run_child(code: str) -> str:
    """Lanza un hijo que ejecuta `code`, espera a que acabe y devuelve el log."""
    script = FAKE.with_name("talker.py")
    script.write_text(code, encoding="utf-8")
    child = supervisor.Service([sys.executable, str(script)], script.parent)
    child.start()
    check("el hijo termina", wait_until(lambda: not child.running, timeout=10))
    time.sleep(0.5)  # que _pump vacíe el pipe
    return "".join(p.read_text(encoding="utf-8") for p in sorted(LOGDIR.glob("supervisor.log*")))


text = run_child(
    "import sys\n"
    "print('hola cámara ñandú')\n"
    "print('esto va por stderr', file=sys.stderr)\n"
    "raise SystemExit(3)\n"
)
check("mensaje del supervisor en el log", "servicio lanzado" in text)
check("stdout del hijo con tildes intactas", "hola cámara ñandú" in text)
check("stderr del hijo también", "esto va por stderr" in text)

text = run_child("for i in range(400): print(f'relleno {i:04d} ' + 'x' * 40)\n")
check("la última línea no se pierde (sin buffer)", "relleno 0399" in text)
files = sorted(p.name for p in LOGDIR.glob("supervisor.log*"))
check("rota y guarda como mucho backups copias",
      files == ["supervisor.log", "supervisor.log.1", "supervisor.log.2"], str(files))
check("ningún fichero pasa de max_bytes",
      all(p.stat().st_size <= 4096 for p in LOGDIR.glob("supervisor.log*")))

print()
if failures:
    print(f"{len(failures)} FALLO(S): {failures}")
    sys.exit(1)
print("todo OK")
