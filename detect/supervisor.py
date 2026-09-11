"""Supervisor del servicio: el proceso que lanza, para y reinicia uvicorn.

Un proceso muerto no puede arrancarse a si mismo, asi que para poder
apagar/reiniciar/arrancar el servicio desde Swagger o desde Home Assistant
hace falta algo pequeno que viva siempre: esto. No importa torch ni camera.py,
arranca en milisegundos y no toca la GPU.

    cd detect
    venv\\Scripts\\python.exe supervisor.py            # lanza uvicorn en :8080 y escucha en :8081
    venv\\Scripts\\python.exe supervisor.py --no-autostart

Swagger propio en http://<host>:8081/docs. main.py expone /service/restart,
/service/shutdown y /service/status en el :8080 como proxies hacia aqui.

Parar a mano (POST /service/stop) es definitivo: el watchdog no relanza el
servicio hasta el siguiente POST /service/start. Si uvicorn se cae solo, si lo
relanza (con backoff). Misma semantica que el manual_stop de las camaras.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI

from log import print


DETECT = Path(__file__).resolve().parent
PY = DETECT / "venv" / "Scripts" / "python.exe"
if not PY.exists():  # fuera de Windows o sin venv
    PY = Path(sys.executable)

SERVICE_HOST = "0.0.0.0"
SERVICE_PORT = 8080
SUPERVISOR_PORT = 8081


def service_cmd(port: int = SERVICE_PORT) -> list[str]:
    """El mismo comando que documenta el README (los dos flags son obligatorios:
    --workers 1 porque el estado vive en memoria, --timeout-graceful-shutdown 5
    para que los hilos de cámara tengan tiempo de hacer join() al apagar)."""
    return [
        str(PY), "-m", "uvicorn", "main:app",
        "--host", SERVICE_HOST, "--port", str(port),
        "--workers", "1", "--timeout-graceful-shutdown", "5",
    ]


# Parada ordenada: 5s de graceful de uvicorn + los ~4s que se da el lifespan
# por sesión + margen. Pasado esto, terminate().
STOP_TIMEOUT_SEC = 12.0
# Cada cuánto se reenvía la señal de parada mientras el hijo siga vivo
STOP_RESEND_SEC = 2.0

# Backoff del watchdog cuando el servicio se cae solo (no cuando se para a mano)
RELAUNCH_BACKOFF_SEC = (2, 4, 8, 16, 30)


class Service:
    """El proceso hijo de uvicorn y lo que sabemos de él."""

    def __init__(self, cmd: list[str], cwd: Path, env: Optional[dict] = None):
        self.cmd = cmd
        self.cwd = cwd
        self.env = env
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._started_at: Optional[float] = None
        self.last_exit_code: Optional[int] = None
        # A True solo por POST /service/stop: el watchdog lo respeta hasta el
        # siguiente start. Los crashes lo dejan a False y se relanzan.
        self.manual_stop = False
        self._crashes = 0
        self._watchdog_stop = threading.Event()
        self._watchdog = threading.Thread(target=self._watch, daemon=True, name="watchdog")

    # -- estado ------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self.running else None

    def status(self) -> dict:
        return {
            "running": self.running,
            "pid": self.pid,
            "uptime_sec": round(time.monotonic() - self._started_at, 1)
            if self.running and self._started_at is not None else None,
            "last_exit_code": self.last_exit_code,
            "manual_stop": self.manual_stop,
            "cmd": " ".join(self.cmd),
        }

    # -- arranque / parada ---------------------------------------------------

    def start(self, manual: bool = False) -> bool:
        """Lanza uvicorn si no está ya. Devuelve si llegó a lanzarlo."""
        with self._lock:
            if manual:
                self.manual_stop = False
                self._crashes = 0
            if self.running:
                return False
            # Grupo de procesos propio en Windows: así se le puede mandar un
            # CTRL_BREAK_EVENT solo a él (CTRL_C_EVENT iría a toda la consola,
            # supervisor incluido). Ver send_stop_signal.
            self._proc = subprocess.Popen(
                self.cmd, cwd=self.cwd, env=self.env,
                **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                   if sys.platform == "win32" else {}),
            )
            self._started_at = time.monotonic()
            print(f"servicio lanzado (pid {self._proc.pid}): {' '.join(self.cmd)}")
            return True

    def stop(self, manual: bool = False) -> bool:
        """Parada ordenada (la misma señal que un Ctrl+C) y espera a que muera.
        Devuelve si había algo que parar."""
        with self._lock:
            if manual:
                self.manual_stop = True
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return False
            # La señal se reenvía cada pocos segundos, no una sola vez: un
            # proceso recién lanzado (aún sin sus handlers instalados) se la
            # traga sin más y seguiría vivo hasta el terminate(). Medido con un
            # hijo de prueba: parado a <10ms del arranque no moría nunca.
            deadline = time.monotonic() + STOP_TIMEOUT_SEC
            while True:
                send_stop_signal(proc)
                try:
                    proc.wait(timeout=min(STOP_RESEND_SEC, max(0.1, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        print(f"el servicio (pid {proc.pid}) no murió en {STOP_TIMEOUT_SEC:.0f}s, lo mato a la fuerza")
                        kill_tree(proc)
                        proc.wait(timeout=5)
                        break
            self.last_exit_code = proc.returncode
            # A None para que el watchdog no lo tome por un crash y lo relance
            self._proc = None
            print(f"servicio parado (pid {proc.pid}, exit {proc.returncode})")
            return True

    def restart(self) -> dict:
        """Para y arranca. Reiniciar es querer que corra: levanta la parada manual."""
        self.stop()
        self.start(manual=True)
        return self.status()

    # -- watchdog ------------------------------------------------------------

    def start_watchdog(self):
        self._watchdog.start()

    def stop_watchdog(self):
        self._watchdog_stop.set()

    def _watch(self):
        while not self._watchdog_stop.wait(1.0):
            with self._lock:
                proc = self._proc
                if proc is None or proc.poll() is None:
                    continue
                # Murió sin que lo parásemos nosotros: stop() deja _proc a
                # None, así que un proceso muerto aquí es siempre un crash.
                self.last_exit_code = proc.returncode
                self._proc = None
                if self.manual_stop:
                    continue
                delay = RELAUNCH_BACKOFF_SEC[min(self._crashes, len(RELAUNCH_BACKOFF_SEC) - 1)]
                self._crashes += 1
                print(f"el servicio murió solo (exit {proc.returncode}), relanzo en {delay}s "
                      f"(intento {self._crashes})")
            if self._watchdog_stop.wait(delay):
                return
            self.start()


def send_stop_signal(proc: subprocess.Popen):
    """Lo más parecido a un Ctrl+C que se puede mandar a otro proceso.

    En Windows, CTRL_C_EVENT no se puede dirigir a un proceso concreto (va a
    toda la consola), así que se lanza con CREATE_NEW_PROCESS_GROUP y se le
    manda CTRL_BREAK_EVENT, que uvicorn también captura (SIGBREAK está en su
    HANDLED_SIGNALS en Windows, y el hook de shutdown.py se encadena a las tres).
    """
    if sys.platform == "win32":
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGINT)


def kill_tree(proc: subprocess.Popen):
    """Último recurso: matar el proceso y todos sus descendientes.

    No vale proc.terminate(): en Windows, venv\\Scripts\\python.exe es un
    lanzador que arranca el intérprete real como proceso hijo (comprobado:
    el pid de Popen y el `Started server process [pid]` de uvicorn no
    coinciden). terminate() mataría solo al lanzador y uvicorn seguiría vivo,
    huérfano y con el puerto ocupado. El Ctrl+Break no tiene este problema
    porque va a todo el grupo de procesos.
    """
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

service = Service(service_cmd(), DETECT)
AUTOSTART = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    if AUTOSTART:
        service.start(manual=True)
    service.start_watchdog()
    yield
    service.stop_watchdog()
    print("supervisor apagando: parando el servicio...")
    await asyncio.to_thread(service.stop)


app = FastAPI(
    title="YOLO Camera Service - supervisor",
    description="Lanza, para y reinicia el servicio de :8080. Pensado para llamarlo "
                "desde Swagger o desde Home Assistant (rest_command).",
    lifespan=lifespan,
)


@app.get("/service/status")
async def service_status():
    return service.status()


@app.post("/service/start")
async def service_start():
    """Arranca el servicio si no está en marcha y levanta la parada manual."""
    launched = await asyncio.to_thread(service.start, True)
    return {"launched": launched, **service.status()}


@app.post("/service/stop")
async def service_stop():
    """Parada ordenada (como un Ctrl+C) y definitiva: no se relanza hasta
    POST /service/start. Bloquea hasta que el proceso muere (unos segundos)."""
    stopped = await asyncio.to_thread(service.stop, True)
    return {"stopped": stopped, **service.status()}


@app.post("/service/restart")
async def service_restart():
    """Para y vuelve a arrancar. Bloquea hasta que el nuevo proceso está lanzado
    (no hasta que uvicorn haya cargado torch: eso tarda bastante más)."""
    return await asyncio.to_thread(service.restart)


def main():
    global AUTOSTART, service
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=SUPERVISOR_PORT, help="puerto del supervisor (default 8081)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--service-port", type=int, default=SERVICE_PORT, help="puerto de uvicorn (default 8080)")
    ap.add_argument("--no-autostart", action="store_true", help="no lanzar el servicio al arrancar")
    args = ap.parse_args()
    AUTOSTART = not args.no_autostart
    # El hijo necesita saber dónde estamos para sus proxies /service/*
    service = Service(service_cmd(args.service_port), DETECT,
                      env={**os.environ, "DETECT_SUPERVISOR_PORT": str(args.port)})
    print(f"supervisor en http://{args.host}:{args.port}/docs (servicio en :{args.service_port})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
