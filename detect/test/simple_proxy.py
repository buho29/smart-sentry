# -----------------------------------------------------------------------------
# Versión "a prueba de balas" de simple_proxy_fps.py: un hilo watchdog
# independiente que cada 2s imprime en qué ETAPA EXACTA está cada hilo
# (recv, buscando marcadores JPEG, decode, poniendo en cola, sacando de
# cola, encode, notificando al cliente) y hace cuánto que se actualizó por
# última vez cada una. Cuando se congele, el heartbeat te dirá literalmente
# "llevo X segundos parado en la etapa Y" sin que tengamos que adivinar más.
#
# Uso:
#   uvicorn simple_proxy_fps_debug:app --host 0.0.0.0 --port 8080
#   abre http://localhost:8080/stream en el navegador (NO en Swagger)
#   cuando se congele, espera ~10-15s sin tocar nada y mira la consola
# -----------------------------------------------------------------------------

import queue
import re
import socket
import threading
import time
from urllib.parse import urlparse

import cv2
import numpy as np
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

ESP_STREAM_URL = "http://192.168.1.50:8080/"
_parsed = urlparse(ESP_STREAM_URL)
ESP_HOST = _parsed.hostname
ESP_PORT = _parsed.port or 80
ESP_PATH = _parsed.path or "/"

CONTENT_LENGTH_RE = re.compile(rb"Content-Length:\s*(\d+)", re.IGNORECASE)

app = FastAPI()

raw_queue: "queue.Queue" = queue.Queue(maxsize=1)
stop_event = threading.Event()

# --- estado para el heartbeat: timestamp de la última vez que cada etapa
# progresó, más en qué etapa está metido cada hilo AHORA MISMO. ---
STAGE = {
    "reader": "arrancando",
    "process": "arrancando",
}
LAST_PROGRESS = {
    "recv": time.time(),        # última vez que sock.recv() devolvió algo
    "frame_found": time.time(), # última vez que encontramos un jpg completo en el buffer
    "decoded": time.time(),     # última vez que cv2.imdecode devolvió un frame válido
    "queued": time.time(),      # última vez que metimos un frame en raw_queue
    "dequeued": time.time(),    # última vez que sacamos un frame de raw_queue
    "encoded": time.time(),     # última vez que cv2.imencode terminó
    "published": time.time(),   # última vez que notificamos al cliente
}
STATE_LOCK = threading.Lock()


def mark(stage_key: str):
    with STATE_LOCK:
        LAST_PROGRESS[stage_key] = time.time()


def set_stage(thread: str, stage: str):
    with STATE_LOCK:
        STAGE[thread] = stage


def heartbeat_loop():
    while not stop_event.is_set():
        time.sleep(2.0)
        now = time.time()
        with STATE_LOCK:
            stages = dict(STAGE)
            ages = {k: now - v for k, v in LAST_PROGRESS.items()}
        parts = ", ".join(f"{k}={v:.1f}s" for k, v in ages.items())
        print(f"[heartbeat] reader='{stages['reader']}' process='{stages['process']}' | edad: {parts}")


def reader_loop():
    while not stop_event.is_set():
        sock: "socket.socket | None" = None
        try:
            set_stage("reader", "conectando")
            print(f"[reader] conectando a {ESP_HOST}:{ESP_PORT}{ESP_PATH} ...")
            sock = socket.create_connection((ESP_HOST, ESP_PORT), timeout=5)
            sock.settimeout(15)
            sock.sendall(
                f"GET {ESP_PATH} HTTP/1.1\r\nHost: {ESP_HOST}\r\nConnection: keep-alive\r\n\r\n".encode()
            )
            print("[reader] conectado")

            buffer = b""
            header_skipped = False
            expected_len = None  # None = esperando la cabecera de la siguiente parte; int = bytes de JPEG que faltan por leer
            while not stop_event.is_set():
                set_stage("reader", "recv")
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    raise TimeoutError("sin datos durante 15s")
                if not chunk:
                    raise ConnectionError("el ESP cerró la conexión")
                mark("recv")
                buffer += chunk

                if not header_skipped:
                    set_stage("reader", "saltando cabecera HTTP")
                    idx = buffer.find(b"\r\n\r\n")
                    if idx == -1:
                        continue
                    buffer = buffer[idx + 4:]
                    header_skipped = True

                # Parseo real del multipart: usamos el Content-Length que el
                # propio ESP declara en cada parte (en vez de buscar a mano
                # \xff\xd8/\xff\xd9, que puede desincronizarse si esos bytes
                # aparecen por casualidad DENTRO de los datos comprimidos de
                # un JPEG real -- justo lo que causaba los cuelgues largos).
                progressed = True
                while progressed:
                    progressed = False
                    if expected_len is None:
                        set_stage("reader", "buscando cabecera de la parte (Content-Length)")
                        idx = buffer.find(b"\r\n\r\n")
                        if idx != -1:
                            header_block = buffer[:idx]
                            m = CONTENT_LENGTH_RE.search(header_block)
                            buffer = buffer[idx + 4:]
                            if m:
                                expected_len = int(m.group(1))
                            # si no hay Content-Length en este bloque (raro,
                            # normalmente sería el boundary suelto), seguimos
                            # sin fijar expected_len y reintentamos con el
                            # siguiente \r\n\r\n que aparezca
                            progressed = True
                    else:
                        if len(buffer) >= expected_len:
                            jpg = buffer[:expected_len]
                            buffer = buffer[expected_len:]
                            expected_len = None
                            mark("frame_found")

                            set_stage("reader", "cv2.imdecode")
                            nparr = np.frombuffer(jpg, np.uint8)
                            frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                            if frame is not None:
                                mark("decoded")
                                set_stage("reader", "raw_queue.put")
                                if raw_queue.full():
                                    try:
                                        raw_queue.get_nowait()
                                    except queue.Empty:
                                        pass
                                raw_queue.put(frame)
                                mark("queued")
                            progressed = True

                if len(buffer) > 2_000_000:
                    buffer = b""
                    expected_len = None
            set_stage("reader", "parado (stop_event)")
        except (OSError, TimeoutError, ConnectionError) as e:
            set_stage("reader", f"reconectando ({e!r})")
            print(f"[reader] error de conexión: {e!r}, reconectando en 1s...")
            time.sleep(1.0)
            continue
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


def process_loop():
    global latest_annotated_jpeg, frame_seq
    fps_window: list[float] = []

    while not stop_event.is_set():
        set_stage("process", "raw_queue.get")
        try:
            frame = raw_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        mark("dequeued")

        now = time.time()
        fps_window.append(now)
        while fps_window and now - fps_window[0] > 1.0:
            fps_window.pop(0)
        fps = (len(fps_window) - 1) / max(fps_window[-1] - fps_window[0], 1e-6) if len(fps_window) >= 2 else 0.0

        set_stage("process", "cv2.putText")
        cv2.putText(frame, f"{fps:.1f} fps  {time.strftime('%H:%M:%S')}", (10, 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

        set_stage("process", "cv2.imencode")
        ok, buf = cv2.imencode(".jpg", frame)
        if not ok:
            continue
        mark("encoded")

        set_stage("process", "notificando al cliente")
        with cond:
            latest_annotated_jpeg = buf.tobytes()
            frame_seq += 1
            cond.notify_all()
        mark("published")


latest_annotated_jpeg: "bytes | None" = None
frame_seq = 0
cond = threading.Condition()

reader_thread = threading.Thread(target=reader_loop, daemon=True)
processing_thread = threading.Thread(target=process_loop, daemon=True)
heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)


@app.on_event("startup")
def startup():
    reader_thread.start()
    processing_thread.start()
    heartbeat_thread.start()


def mjpeg_generator():
    last_seq_seen = -1
    while True:
        with cond:
            cond.wait_for(lambda: frame_seq != last_seq_seen, timeout=5.0)
            last_seq_seen = frame_seq
            jpg = latest_annotated_jpeg
        if jpg is None:
            continue
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n"
            + jpg + b"\r\n"
        )


@app.get("/stream")
def stream():
    return StreamingResponse(
        mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )