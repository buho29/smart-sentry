from fastapi import FastAPI, UploadFile, File, Form, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from ultralytics import YOLO
import threading
import queue
import requests
import numpy as np
import cv2
import time
import io
import torch
# torch.backends.cudnn.benchmark = False
torch.backends.cudnn.enabled = False


app = FastAPI()

# Cache de modelos cargados, para poder comparar varios sin recargar cada vez
loaded_models = {}

def get_model(name: str, device: str = "cuda"):
    key = f"{name}_{device}"
    if key not in loaded_models:
        m = YOLO(f"{name}.pt")
        m.to(device)
        loaded_models[key] = m
    return loaded_models[key]

# Precarga y warm-up del modelo por defecto (ajusta la llamada de warm-up igual)
default_device = "cuda" if torch.cuda.is_available() else "cpu"
default_model = get_model("yolo11m", default_device)
dummy = np.zeros((640, 640, 3), dtype=np.uint8)
default_model.predict(dummy, verbose=False)
print(f"Modelo precalentado en {default_device} y listo")

class ImageRequest(BaseModel):
    image_url: str
    confidence: float = 0.5


# --- KEEP-ALIVE DE GPU ---
# Diagnostico confirmado (31/08/2026): la GTX 1080 produce inferencias corruptas
# (confianzas fuera de rango 0-1, p.ej. 4.796) cuando el driver baja el estado de
# energia (P5) entre frames y vuelve a subir para la siguiente inferencia. Se
# confirmo con nvidia-smi que las anomalias coinciden al segundo exacto con
# transiciones de P-state. "Preferir maximo rendimiento" en el panel de NVIDIA
# no bastó, asi que mantenemos la GPU ocupada activamente con inferencias
# minimas cuando no ha llegado un frame real, en vez de dejarla bloqueada
# esperando en frame_queue.get() sin limite de tiempo (lo que le daba tiempo
# de sobra para caer a P5).
KEEPALIVE_IMG = np.zeros((640, 640, 3), dtype=np.uint8)
KEEPALIVE_INTERVAL_SEC = 0.05  # frecuencia maxima a la que se permite caer a reposo 


def frame_reader(stream_url, out_queue, stop_event):
    """Hilo dedicado solo a leer del ESP32 y quedarse con el frame más reciente."""
    while not stop_event.is_set():
        r = None
        try:
            r = requests.get(stream_url, stream=True, timeout=(5, 15))
            buffer = b""
            for chunk in r.iter_content(chunk_size=4096):
                if stop_event.is_set():
                    break
                buffer += chunk
                start = buffer.find(b'\xff\xd8')
                end = buffer.find(b'\xff\xd9')
                if start != -1 and end != -1 and end > start:
                    jpg = buffer[start:end + 2]
                    buffer = buffer[end + 2:]
                    nparr = np.frombuffer(jpg, np.uint8)
                    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                    if frame is not None:
                        if out_queue.full():
                            try:
                                out_queue.get_nowait()
                            except queue.Empty:
                                pass
                        out_queue.put(frame)
                if len(buffer) > 1_000_000:
                    buffer = b""
        except requests.exceptions.RequestException as e:
            print(f"Stream del ESP32 interrumpido ({e!r}), reconectando en 1s...")
            time.sleep(1)
            continue
        finally:
            if r is not None:
                r.close()

def generate_annotated_stream(stream_url, model_name="yolo11m", confidence=0.3, imgsz=640,
                               device="cuda"):
    """
    Stream anotado del ESP32-S3-CAM con detección YOLO.

    HISTORIAL DE DEPURACIÓN (para no repetir pasos ya descartados):
    - WiFi: descartado. RSSI de la cámara excelente (-19/-21 dBm) durante las lagunas.
    - Cámara ESP32 en sí: descartada. Accediendo directo al stream (sin este script
      de por medio) va fluido, 3-5fps, sin cortes.
    - Lectura de red bloqueando el hilo principal: mitigado. La lectura del stream
      corre en un hilo aparte (frame_reader) con una cola de tamaño 1, para que la
      inferencia no tenga que esperar directamente a la red.
    - GPU entrando en estados de energía bajos (P2/P5): CONFIRMADO como causa raíz
      (31/08/2026). Se demostró con un stress test aislado (sin cámara/red): 15000
      inferencias seguidas sin pausa -> 0 anomalías; el mismo test con una pausa de
      0.25s entre inferencias (imitando el ritmo real de la cámara) -> anomalías ya
      en la iteración 27, coincidiendo al segundo exacto con una transición de
      P-state en el log de nvidia-smi. "Preferir máximo rendimiento" en el panel de
      NVIDIA no fue suficiente por sí solo. Solución aplicada: keep-alive activo de
      GPU (ver más abajo) en vez de esperar bloqueado en la cola sin límite.
    - Umbral de confianza / ByteTrack: descartado. Con conf=0.01 (prácticamente sin
      filtro) siguen apareciendo frames con "NINGUNA CAJA", tanto con model.track()
      como con model.predict() (se probó explícitamente para descartar que fuera
      el filtrado interno de ByteTrack) — mismo resultado en ambos casos.
    """
    model = get_model(model_name, device)

    TEST_CLASSES = [0, 15, 56, 59]   # person, cat, chair, bed (clases de testeo, sin pájaros a mano)

    frame_queue = queue.Queue(maxsize=1)
    stop_event = threading.Event()
    reader_thread = threading.Thread(target=frame_reader, args=(stream_url, frame_queue, stop_event), daemon=True)
    reader_thread.start()

    last_yield_time = [time.time()]

    # Evaluamos si el dispositivo destino utiliza CUDA para aplicar keep-alive o reposo pasivo
    is_cuda = device.startswith("cuda")

    try:
        while True:
            frame = None

            if is_cuda:
                # Antes: frame = frame_queue.get() bloqueaba sin límite, dando tiempo
                # de sobra a que la GPU cayera a P5 entre frames reales. Ahora
                # esperamos como máximo KEEPALIVE_INTERVAL_SEC y, si no llega frame
                # real, disparamos una inferencia mínima para mantener la GPU activa
                # antes de volver a intentarlo.
                try:
                    frame = frame_queue.get(timeout=KEEPALIVE_INTERVAL_SEC)
                except queue.Empty:
                    try:
                        model.predict(KEEPALIVE_IMG, imgsz=640, verbose=False)
                    except Exception as e:
                        print("keep-alive de GPU falló:", repr(e))
                    continue
            else:
                # En CPU no hay degradación por P-states; un keep-alive sintético 
                # saturaría los núcleos al 100%. Se usa espera pasiva limpia.
                try:
                    frame = frame_queue.get(block=True, timeout=1.0)
                except queue.Empty:
                    if stop_event.is_set():
                        break
                    continue

            if frame is None:
                continue

            try:
                t0 = time.time()
                results = model.track(
                    frame,
                    persist=True,
                    conf=confidence,
                    imgsz=imgsz,
                    verbose=False,
                    tracker="bytetrack.yaml",
                    classes=TEST_CLASSES,
                )[0]
                inference_ms = (time.time() - t0) * 1000
                if inference_ms > 80:
                    print(f"INFERENCIA lenta: {inference_ms:.0f} ms")

                # --- diagnóstico: mejor confianza del frame, aunque no llegue al umbral ---
                # (nota: aquí SÍ se aplica el conf= de arriba antes de llegar a results.boxes;
                # para el diagnóstico con conf=0.01 real, hay que bajar el parámetro `confidence`
                # al llamar a este generador, no cambiarlo solo en este print)
                if len(results.boxes) > 0:
                    best_conf = float(results.boxes.conf.max())
                    print(f"mejor confianza del frame: {best_conf:.3f}")
                else:
                    print("mejor confianza del frame: NINGUNA CAJA (ni con el umbral actual)")

                # --- dibujo de cajas crudas, sin suavizado ni tracking por ahora ---
                for box in results.boxes:
                    x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
                    label = model.names[int(box.cls)]
                    conf_val = float(box.conf[0])
                    tid = int(box.id) if box.id is not None else -1

                    cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), (0, 0, 255), 1)
                    cv2.putText(frame, f"{label} {conf_val:.2f} #{tid}", (int(x1), int(y1) - 6),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 255), 1)

                # --- overlay: ms de inferencia + fps efectivo del pipeline completo ---
                now = time.time()
                pipeline_fps = 1.0 / max(now - last_yield_time[0], 1e-6)
                last_yield_time[0] = now
                cv2.putText(frame, f"{inference_ms:.0f} ms ({device}) | {pipeline_fps:.1f} fps", (10, 20),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)

                annotated = frame

            except Exception as e:
                print("ERROR en track/dibujo:", repr(e))
                annotated = frame

            ok, buf = cv2.imencode('.jpg', annotated)
            if not ok:
                continue

            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + buf.tobytes() + b'\r\n')

    finally:
        # se ejecuta al refrescar/cerrar la página (GeneratorExit) o si algo revienta arriba
        stop_event.set()
        reader_thread.join(timeout=2)


@app.get("/detect/stream")
async def detect_stream(
    stream_url: str,
    model_name: str = "yolo11m",
    confidence: float = 0.5,
    imgsz: int = 640,
    device: str = "cuda",
):
    return StreamingResponse(
        generate_annotated_stream(stream_url, model_name, confidence, imgsz, device),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/detect")
async def detect(req: ImageRequest):
    response = requests.get(req.image_url, timeout=5)
    nparr = np.frombuffer(response.content, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return {"error": "No se pudo decodificar la imagen"}

    start = time.time()
    results = default_model.predict(img, verbose=False)[0]
    inference_ms = round((time.time() - start) * 1000, 1)

    detections = []
    for box in results.boxes:
        if float(box.conf) >= req.confidence:
            x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
            detections.append({
                "label": default_model.names[int(box.cls)],
                "confidence": round(float(box.conf), 3),
                "box": {"x1": round(x1, 1), "y1": round(y1, 1), "x2": round(x2, 1), "y2": round(y2, 1)},
                "center": {"x": round((x1 + x2) / 2, 1), "y": round((y1 + y2) / 2, 1)},
            })

    return {"detections": detections, "inference_ms": inference_ms}


@app.post("/detect-file")
async def detect_file(
    image: UploadFile = File(...),
    model_name: str = Form("yolo11n"),
    confidence: float = Form(0.5),
    imgsz: int = Form(640),
):
    """Prueba con imagen local + elige modelo/confianza/resolución"""
    contents = await image.read()
    nparr = np.frombuffer(contents, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return {"error": "No se pudo decodificar la imagen"}

    model = get_model(model_name)
    start = time.time()
    results = model.predict(img, conf=confidence, imgsz=imgsz, verbose=False)[0]
    inference_ms = round((time.time() - start) * 1000, 1)

    detections = [
        {"label": model.names[int(box.cls)], "confidence": round(float(box.conf), 3)}
        for box in results.boxes
    ]
    return {"model": model_name, "detections": detections, "inference_ms": inference_ms}

@app.post("/detect-file/annotated")
async def detect_file_annotated(
    image: UploadFile = File(...),
    model_name: str = Form("yolo11n"),
    confidence: float = Form(0.5),
    imgsz: int = Form(640),
):
    """Igual que arriba pero devuelve la imagen con las cajas dibujadas, para verlo directamente"""
    contents = await image.read()
    nparr = np.frombuffer(contents, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return {"error": "No se pudo decodificar la imagen"}

    model = get_model(model_name)
    results = model.predict(img, conf=confidence, imgsz=imgsz, verbose=False)[0]
    annotated = results.plot()  # numpy array con las cajas ya pintadas

    ok, buf = cv2.imencode(".jpg", annotated)
    return StreamingResponse(io.BytesIO(buf.tobytes()), media_type="image/jpeg")
