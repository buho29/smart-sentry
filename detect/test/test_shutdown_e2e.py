"""E2E del apagado: uvicorn de verdad, dos streams abiertos y un Ctrl+C.

Es la regresión del bloqueo circular del apagado. `Server.shutdown()` de
uvicorn apaga en tres pasos: (1) deja de aceptar conexiones, (2) ESPERA a que
terminen las respuestas en vuelo, (3) ejecuta el shutdown del `lifespan`, que
es donde paramos las cámaras. Como los streams MJPEG son bucles infinitos, un
generador que solo mirase `_stop_event` no podía salir hasta el paso 3, que no
llega hasta que el paso 2 se rinde por timeout:

    INFO:     Waiting for connections to close. (CTRL+C to force quit)
    ERROR:    Cancel 2 running task(s), timeout graceful shutdown exceeded
    ...un CancelledError por cada cliente conectado...

`test_cierre.py` (casos 6 y 7) cubre las piezas por separado; esto comprueba el
camino real de uvicorn de punta a punta.

    cd detect
    venv\\Scripts\\python.exe test\\test_apagado_e2e.py

No necesita las placas: monta un servidor MJPEG local. Tampoco toca el
`cameras_config.json` real, porque copia `main.py` a un directorio temporal y
`CAMERAS_CONFIG_FILE` se calcula con `Path(__file__).with_name(...)`, o sea
relativo a esa copia.
"""

import http.server
import os
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import requests

DETECT = Path(__file__).resolve().parent.parent
PY = DETECT / "venv" / "Scripts" / "python.exe"
if not PY.exists():  # fuera de Windows o sin venv
    PY = Path(sys.executable)

MODELO = "yolo11n"
ARRANQUE_TIMEOUT = 120  # el primer import de torch/ultralytics es lento

fallos: list[str] = []


def check(nombre, cond, extra=""):
    print(f"  {'PASS ' if cond else 'FALLO'}  {nombre} {extra}")
    if not cond:
        fallos.append(nombre)


def puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def servidor_mjpeg_local():
    """Cámara de mentira que emite un JPEG **real** sin parar.

    Tiene que ser un JPEG válido: con bytes basura `cv2.imdecode` devuelve
    None, el lector descarta el frame y `_process_loop` no publica nada, así
    que los generadores nunca reciben el `notify_all()` y el test mediría el
    timeout de espera en vez del camino real.
    """
    ok, buf = cv2.imencode(".jpg", np.zeros((48, 64, 3), dtype=np.uint8))
    assert ok, "no se pudo generar el JPEG de prueba"
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

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def abrir_stream(url, listo, parar, errores):
    """Consume un stream MJPEG de verdad hasta que se le diga que pare.

    Tiene que seguir leyendo en bucle: si solo se pide un chunk y se suelta el
    iterador de `iter_content`, el recolector lo cierra y con él la conexión.
    Entonces uvicorn no tiene ninguna respuesta en vuelo que esperar al apagar
    y el test pasaría siempre, aunque la regresión estuviera presente.
    """
    try:
        r = requests.get(url, stream=True, timeout=30)
        it = r.iter_content(chunk_size=4096)
        next(it)  # confirmar que ya emite antes de dar el visto bueno
        listo.set()
        try:
            for _ in it:
                if parar.is_set():
                    break
        finally:
            r.close()
    except Exception as e:
        errores.append(f"{url}: {e!r}")
        listo.set()


def mandar_señal_de_parada(proc):
    """Lo más parecido a un Ctrl+C que se puede mandar a otro proceso.

    En Windows, CTRL_C_EVENT no se puede dirigir a un proceso concreto (va a
    toda la consola), así que se lanza con CREATE_NEW_PROCESS_GROUP y se le
    manda CTRL_BREAK_EVENT, que uvicorn también captura (SIGBREAK está en su
    HANDLED_SIGNALS en Windows, y nuestro hook se encadena a las tres).
    """
    if sys.platform == "win32":
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGINT)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="e2e_apagado_"))
    srv = proc = None
    streams = []
    parar_lectores = threading.Event()  # definido aquí porque el finally lo usa
    try:
        # Todos los módulos del servicio, no solo main.py: importa a sus
        # vecinos (detections, servo_tracker) y en el temporal no está el
        # directorio original en sys.path.
        for modulo in DETECT.glob("*.py"):
            shutil.copy(modulo, tmp / modulo.name)
        # Copiamos los pesos si los hay, para que ultralytics no se los baje
        # otra vez dentro del temporal en cada ejecución.
        pesos = DETECT / f"{MODELO}.pt"
        if pesos.exists():
            shutil.copy(pesos, tmp / pesos.name)
        print(f"copia aislada de main.py en {tmp}")

        srv, puerto_cam = servidor_mjpeg_local()
        puerto_api = puerto_libre()
        print(f"cámara falsa en :{puerto_cam}, API en :{puerto_api}")

        proc = subprocess.Popen(
            [str(PY), "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
             "--port", str(puerto_api), "--workers", "1",
             "--timeout-graceful-shutdown", "5"],
            cwd=tmp,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            # Sin esto, nuestros print() van a un pipe -> stdout queda
            # block-buffered, y uvicorn (que al final re-lanza la señal
            # capturada) mata el proceso antes de vaciarlo: se perderían justo
            # las líneas del apagado que queremos comprobar.
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
               if sys.platform == "win32" else {}),
        )

        salida: list[str] = []
        threading.Thread(
            target=lambda: [salida.append(l.rstrip()) for l in proc.stdout],
            daemon=True,
        ).start()

        base = f"http://127.0.0.1:{puerto_api}"
        for _ in range(ARRANQUE_TIMEOUT * 2):
            if proc.poll() is not None:
                print("el servidor murió al arrancar:\n" + "\n".join(salida))
                return 1
            try:
                if requests.get(f"{base}/health", timeout=1).ok:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            print("el servidor no arrancó a tiempo:\n" + "\n".join(salida))
            return 1
        print("uvicorn arrancado")

        for i in (1, 2):
            # El alta va por formulario, no por JSON
            r = requests.post(f"{base}/cameras", data={
                "camera_id": f"cam{i}",
                "stream_url": f"http://127.0.0.1:{puerto_cam}/",
                "model_name": MODELO, "device": "cpu", "default_infer": "false",
            }, timeout=60)
            assert r.ok, f"alta de cam{i} falló: {r.status_code} {r.text}"
            # explicit_start=True, que es como quedan las cámaras de verdad
            # cuando la placa publica `estado=on`. Sin esto, al salir el
            # generador saltaría el autostop por quedarse sin clientes y el
            # log no se parecería al de producción.
            requests.post(f"{base}/cameras/cam{i}/start", timeout=30).raise_for_status()

        errores: list[str] = []
        for i in (1, 2):
            listo = threading.Event()
            h = threading.Thread(
                target=abrir_stream,
                args=(f"{base}/cameras/cam{i}/stream?infer=false",
                      listo, parar_lectores, errores),
                daemon=True, name=f"cliente-cam{i}",
            )
            h.start()
            streams.append(h)
            assert listo.wait(timeout=60), f"el stream de cam{i} no llegó a emitir"
            print(f"  stream de cam{i} abierto y emitiendo")
        assert not errores, f"errores abriendo streams: {errores}"

        time.sleep(2)  # que el pipeline se asiente
        check("los dos clientes siguen conectados",
              all(h.is_alive() for h in streams))

        print("\n>>> señal de parada con los dos streams abiertos...")
        t0 = time.perf_counter()
        mandar_señal_de_parada(proc)
        proc.wait(timeout=60)
        dt = time.perf_counter() - t0
        log = "\n".join(salida)

        print(f">>> el proceso terminó en {dt:.2f}s\n")
        print("---------------- log del servidor ----------------")
        print(log)
        print("--------------------------------------------------")

        check("sin 'Cancel N running task(s)'", "running task(s)" not in log)
        check("sin CancelledError", "CancelledError" not in log)
        check("sin 'Exception in ASGI application'",
              "Exception in ASGI application" not in log)
        check("el lifespan llegó a ejecutarse", "Apagando: deteniendo" in log)
        check("las dos cámaras paradas", log.count("lectura detenida") >= 2,
              f"({log.count('lectura detenida')})")
        check("apagado completo", "Application shutdown complete" in log)
        check("sin el log duplicado de autostop",
              "sin clientes, lectura detenida" not in log)
        # Antes del arreglo esto se comía los 5s enteros del
        # --timeout-graceful-shutdown esperando a los generadores.
        check("no espera al timeout de gracia (< 3s)", dt < 3.0, f"({dt:.2f}s)")
    finally:
        parar_lectores.set()
        for h in streams:
            h.join(timeout=5)
        if proc is not None and proc.poll() is None:
            proc.kill()
        if srv is not None:
            srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + ("E2E OK" if not fallos else f"FALLOS: {fallos}"))
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
