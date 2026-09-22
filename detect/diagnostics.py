"""Mirar DENTRO del proceso que falla, que es lo único que no se puede hacer desde fuera.

Existe por un fallo muy concreto: el servicio devolvía cero detecciones sobre
frames en los que el mismo modelo, con los mismos ajustes y en un proceso
aparte, encontraba a una persona al 0,83. Comparar desde fuera ya no aportaba
nada; había que correr los dos caminos dentro del proceso enfermo y sobre el
mismo frame.

El `selftest` corre cuatro caminos sobre la imagen actual de la cámara y los
compara:

    track()          el camino real del servicio
    predict()        el mismo modelo sin el tracker
    forward crudo    la red a pelo, sin postproceso: ¿saca algo antes del NMS?
    modelo fresh    una instancia recién cargada, en este mismo proceso

Cada combinación de resultados apunta a un culpable distinto, y `verdict`
lo dice en una frase para no tener que interpretar la tabla.
"""

from __future__ import annotations

import time
from typing import Optional

import numpy as np
import torch

from log import print


def gpu_report() -> dict:
    """Estado del hardware de la GPU: relojes, P-state, temperatura y memoria.

    Vive aquí y no en el `/status` de cada cámara porque es de la máquina, no de
    la cámara: repetir los mismos relojes en cada una era ruido. Lo que sí se
    queda en `/status` es `gpu_clock_pct`, que resume todo esto en un número.

    **No lista los procesos que usan la GPU**, aunque sería el dato ideal para
    detectar a otro programa compitiendo por la tarjeta (el caso real fue un
    Fusion 360 abierto). En Windows con WDDM, NVML no informa de la memoria por
    proceso y encima mete medio escritorio en la lista de cómputo, así que solo
    se podía devolver una lista vacía o ruido disfrazado de información.

    La vía que sí funciona es `memory_used_mb`: si es bastante mayor que lo que
    ocupa el servicio (del orden de 1 GB con un modelo cargado), hay algo más
    dentro. Para saber qué, `nvidia-smi`.
    """
    # Se importa el MÓDULO, no los nombres: `_sm_clock_max_seen` es un int que
    # camera.py reasigna, y un `from camera import _sm_clock_max_seen` se
    # quedaría con el valor que tuviera en el import (0) para siempre.
    import camera

    out: dict = {"clock_pct": camera.gpu_clock_pct(),
                 "sm_clock_max_seen_mhz": camera._sm_clock_max_seen or None,
                 **camera._gpu_state()}
    try:
        out["name"] = torch.cuda.get_device_name(0)
        out["capability"] = list(torch.cuda.get_device_capability(0))
        out["device_count"] = torch.cuda.device_count()
    except Exception:
        out["name"] = None
    # cuDNN lo decide DETECT_CUDNN, no una detección de arquitectura. Se expone
    # porque una GTX 10xx (Pascal) con cuDNN activo da "CUDA misaligned
    # address" de forma intermitente, y esto es lo primero que hay que mirar.
    out["cudnn_enabled"] = torch.backends.cudnn.enabled

    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        out["temperature_c"] = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
        out["utilization_pct"] = pynvml.nvmlDeviceGetUtilizationRates(h).gpu
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        out["memory_used_mb"] = round(mem.used / 1024 ** 2)
        out["memory_total_mb"] = round(mem.total / 1024 ** 2)
    except Exception:
        pass
    return out


def _to_tensor(frame: np.ndarray, imgsz: int, ref_param) -> torch.Tensor:
    """Frame BGR -> tensor listo para el forward crudo.

    Se redimensiona a cuadrado en vez de hacer letterbox como ultralytics: para
    lo que se quiere saber aquí (¿la red saca números finitos y puntuaciones
    altas?) la geometría exacta da igual, y así no se depende de sus internos.
    """
    import cv2

    img = cv2.resize(frame, (imgsz, imgsz))
    img = img[:, :, ::-1].transpose(2, 0, 1)          # BGR->RGB, HWC->CHW
    t = torch.from_numpy(np.ascontiguousarray(img)).to(ref_param.device)
    return (t.to(ref_param.dtype) / 255.0).unsqueeze(0)


def _max_score(output) -> Optional[float]:
    """Puntuación máxima de la red ANTES del NMS.

    Si esto es alto y aun así no salen cajas, el problema está en el
    postproceso (NMS, `conf`, `classes`) y no en la red. Si es bajo, es que la
    red de verdad no está viendo nada.
    """
    try:
        t = output[0] if isinstance(output, (list, tuple)) else output
        if not torch.is_tensor(t):
            return None
        # Cabeza estilo YOLO: (1, 4+nc, anchors). Las 4 primeras filas son la
        # caja; las demás, puntuaciones por clase.
        if t.ndim == 3 and t.shape[1] > 4:
            return float(t[:, 4:, :].max())
        return float(t.max())
    except Exception:
        return None


def selftest(session, cached_model, pt_path: str) -> dict:
    """Los cuatro caminos sobre el frame actual. No modifica configuración."""
    import cv2

    cfg = session.cfg
    frame = session.latest_frame()
    if frame is None:
        jpg = session.snapshot(False) or session.snapshot(True)
        if jpg is None:
            return {"error": "no hay ningún frame todavía; ¿está la cámara arrancada?"}
        frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return {"error": "no se pudo decodificar el frame actual"}

    kw = dict(conf=cfg.confidence, imgsz=cfg.imgsz,
              classes=cfg.classes, verbose=False)
    out: dict = {
        "camera_id": cfg.camera_id,
        "modelo": cfg.model_name,
        "device": cfg.device,
        "conf": cfg.confidence,
        "imgsz": cfg.imgsz,
        "classes": cfg.classes,
        "frame": f"{frame.shape[1]}x{frame.shape[0]}",
        "mean_brightness": round(float(frame.mean()), 1),
    }

    def _tracker_frame_id():
        try:
            return cached_model.predictor.trackers[0].frame_id
        except Exception:
            return None

    out["tracker_frame_id_before"] = _tracker_frame_id()

    # 1. El camino REAL del servicio.
    try:
        t0 = time.perf_counter()
        r = cached_model.track(frame, persist=True, tracker="bytetrack.yaml", **kw)[0]
        out["track_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out["track_boxes"] = len(r.boxes)
        out["track_labels"] = _labels(cached_model, r)
    except Exception as e:
        out["track_error"] = repr(e)
        out["track_boxes"] = None

    # 2. El mismo modelo sin tracker.
    try:
        t0 = time.perf_counter()
        r = cached_model.predict(frame, **kw)[0]
        out["predict_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out["predict_boxes"] = len(r.boxes)
        out["predict_labels"] = _labels(cached_model, r)
    except Exception as e:
        out["predict_error"] = repr(e)
        out["predict_boxes"] = None

    out["tracker_frame_id_after"] = _tracker_frame_id()

    # 3. La red a pelo, sin postproceso. Es lo que distingue "la red no ve
    #    nada" de "el postproceso se lo come".
    try:
        ref = next(cached_model.model.parameters())
        tensor = _to_tensor(frame, cfg.imgsz, ref)
        t0 = time.perf_counter()
        with torch.no_grad():
            output = cached_model.model(tensor)
        out["raw_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out["raw_max_score"] = _max_score(output)
        t = output[0] if isinstance(output, (list, tuple)) else output
        out["raw_output_finite"] = bool(torch.isfinite(t).all()) if torch.is_tensor(t) else None
    except Exception as e:
        out["raw_error"] = repr(e)

    # 4. Pesos del modelo cacheado.
    try:
        out["weights_finite"] = all(
            bool(torch.isfinite(p).all()) for p in cached_model.model.parameters())
    except Exception as e:
        out["weights_error"] = repr(e)

    # 5. Una instancia nueva, en ESTE mismo proceso: separa "modelo degradado"
    #    de "contexto CUDA tocado".
    try:
        from ultralytics import YOLO
        fresh = YOLO(pt_path)
        fresh.to(cfg.device)
        t0 = time.perf_counter()
        r = fresh.predict(frame, **kw)[0]
        out["fresh_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out["fresh_boxes"] = len(r.boxes)
        out["fresh_labels"] = _labels(fresh, r)
        del fresh
    except Exception as e:
        out["fresh_error"] = repr(e)
        out["fresh_boxes"] = None

    out["verdict"] = _verdict(out)
    print(f"[{cfg.camera_id}] selftest: {out['verdict']}")
    return out


def _labels(model, result) -> list:
    try:
        return [(model.names[int(c)], round(float(f), 2))
                for c, f in zip(result.boxes.cls.tolist(),
                                result.boxes.conf.tolist())][:10]
    except Exception:
        return []


def _verdict(o: dict) -> str:
    """Traduce la tabla a una frase y a la acción que toca."""
    trk, pre, fre = o.get("track_boxes"), o.get("predict_boxes"), o.get("fresh_boxes")
    score = o.get("raw_max_score")

    if o.get("weights_finite") is False:
        return ("PESOS CORRUPTOS: el modelo cacheado tiene NaN/inf. "
                "Hay que recargarlo (reiniciar el servicio).")
    if o.get("raw_output_finite") is False:
        return ("LA RED SACA NaN: el contexto CUDA del proceso está tocado. "
                "Reinicia el servicio.")
    if trk is None and pre is None:
        return "La inferencia lanza excepción; mira track_error / predict_error."

    if (trk or 0) > 0:
        return ("TODO CORRECTO ahora mismo: el camino del servicio detecta. "
                "Si el stream no pinta cajas, el problema no es la inferencia.")

    if (pre or 0) > 0 and (trk or 0) == 0:
        return ("ES EL TRACKER: predict() detecta y track() no. "
                "POST /cameras/{id}/tracker/reset debería arreglarlo en caliente.")

    if (fre or 0) > 0 and (pre or 0) == 0:
        return ("MODELO CACHEADO DEGRADADO: una instancia nueva detecta en este "
                "mismo proceso y la cacheada no. Hay que recargar el modelo.")

    if score is not None and score >= 0.4:
        return (f"ES EL POSTPROCESO: la red saca una puntuación máxima de "
                f"{score:.2f}, por encima del umbral, pero no sobrevive ninguna "
                f"caja. Revisa conf/classes/NMS.")

    return (f"LA RED NO VE NADA en este frame (puntuación máxima "
            f"{score if score is None else round(score, 3)}). "
            f"Si hay alguien delante, es la imagen: luz, enfoque o encuadre.")
