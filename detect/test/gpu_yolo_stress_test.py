"""
Test de estres aislado para reproducir el fallo de "misaligned address" / Xid 13
sin camara ni red de por medio. Solo YOLO + GPU en bucle sobre imagenes aleatorias.

Uso:
    python gpu_yolo_stress_test.py

Mientras corre, en otra terminal puedes loguear el estado de la GPU con:
    nvidia-smi --query-gpu=timestamp,pstate,clocks.sm,utilization.gpu,memory.used --format=csv -l 1 > gpu_log.csv

y luego cruzar los timestamps de "ANOMALIA" que imprime este script con ese CSV,
para ver si coincide con un cambio de P-state.
"""

import time
import numpy as np
import torch
from ultralytics import YOLO

# Deja esto igual que en tu servicio real para que el test sea representativo
torch.backends.cudnn.enabled = False

MODEL_NAME = "yolo11m"
DEVICE = "cuda"
IMGSZ = 640
CONFIDENCE = 0.3
N_ITERS = 5000  # ~5000 inferencias deberia bastar para ver si aparece la anomalia

# NUEVO: pausa entre inferencias para imitar el ritmo real de tu camara (3-5 fps),
# forzando a la GPU a bajar de estado (P8) y volver a subir en cada frame,
# en vez de mantenerse siempre activa como en el bucle continuo original.
SLEEP_BETWEEN_FRAMES_SEC = 0.25

print(f"Version de torch: {torch.__version__} | CUDA: {torch.version.cuda}")
print(f"Capacidad de computo de la GPU: {torch.cuda.get_device_capability()}")
print(f"Nombre de la GPU: {torch.cuda.get_device_name(0)}")

model = YOLO(f"{MODEL_NAME}.pt")
model.to(DEVICE)

# Warm-up
dummy = np.zeros((IMGSZ, IMGSZ, 3), dtype=np.uint8)
model.predict(dummy, verbose=False)
print("Modelo precalentado, empezando el bucle de estres...\n")

n_anomalias = 0
n_sin_cajas = 0
t_inicio = time.time()

for i in range(N_ITERS):
    # Imagen aleatoria en vez de ceros: estresa mas la memoria que una imagen fija
    frame = np.random.randint(0, 255, (IMGSZ, IMGSZ, 3), dtype=np.uint8)

    try:
        results = model.predict(frame, conf=CONFIDENCE, imgsz=IMGSZ, verbose=False)[0]

        if len(results.boxes) > 0:
            confs = results.boxes.conf
            max_conf = float(confs.max())
            min_conf = float(confs.min())

            # Una confianza fuera de [0, 1] es matematicamente imposible: GPU corrupta
            if max_conf > 1.0 or min_conf < 0.0:
                n_anomalias += 1
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{ts}] ANOMALIA en iter {i}: confianza fuera de rango "
                      f"(max={max_conf:.3f}, min={min_conf:.3f})")
        else:
            n_sin_cajas += 1

    except RuntimeError as e:
        n_anomalias += 1
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] ANOMALIA en iter {i}: excepcion CUDA -> {e!r}")

    time.sleep(SLEEP_BETWEEN_FRAMES_SEC)

    if (i + 1) % 500 == 0:
        elapsed = time.time() - t_inicio
        print(f"...{i + 1}/{N_ITERS} iteraciones, {elapsed:.0f}s, "
              f"{n_anomalias} anomalias, {n_sin_cajas} frames sin ninguna caja")

print("\n=== RESUMEN ===")
print(f"Iteraciones totales: {N_ITERS}")
print(f"Anomalias (confianza imposible o excepcion CUDA): {n_anomalias}")
print(f"Frames sin ninguna caja detectada: {n_sin_cajas}")

if n_anomalias > 0:
    print("\n-> Se reprodujo el fallo SIN camara ni red de por medio.")
    print("   Esto confirma que el problema esta en GPU+driver+CUDA/PyTorch,")
    print("   no en tu pipeline de streaming ni en tu logica de deteccion.")
else:
    print("\n-> No se reprodujo el fallo en este test.")
    print("   Podria depender de la duracion de ejecucion continua, de la")
    print("   temperatura acumulada, o de algo especifico del stream real.")