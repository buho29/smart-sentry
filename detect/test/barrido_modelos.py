"""Mide qué modelo, imgsz y confianza hacen falta para detectar de verdad en ESTA cámara.

El problema que motiva esto: `yolo26m` detecta personas en la imagen de la
ESP32-CAM y `yolo26n` no, pero se cambió al nano precisamente para gastar menos
GPU. Así que la pregunta no es "cuál detecta mejor" —eso ya se sabe— sino
**cuál es la combinación más barata que detecta de forma fiable**.

Dos ideas gobiernan el diseño:

1. **Se mide sobre frames guardados, no sobre la escena en vivo.** Comparar
   modelos contra una imagen que cambia entre ejecuciones no mide nada: todas
   las combinaciones tienen que ver exactamente los mismos píxeles. Por eso
   `capturar` guarda a disco y `barrer` trabaja sobre esa carpeta.

2. **Hacen falta frames CON persona y SIN nadie.** Si solo se mide "¿encuentra
   a la persona?", la respuesta siempre es bajar el umbral hasta que sí, y lo
   que se consigue es el otro síntoma del problema original: cajas absurdas.
   Los frames sin nadie son los que ponen precio a esa trampa.

Uso:

    cd detect
    venv\\Scripts\\python.exe test\\barrido_modelos.py capturar --camera huerta --frames 20 --etiqueta con-persona
    venv\\Scripts\\python.exe test\\barrido_modelos.py capturar --camera huerta --frames 20 --etiqueta sin-nadie
    venv\\Scripts\\python.exe test\\barrido_modelos.py barrer --con-persona barridos\\con-persona-... --sin-nadie barridos\\sin-nadie-...
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import requests  # noqa: E402
import torch  # noqa: E402

from camera import get_model  # noqa: E402

# Espejo de CUDNN_ENABLED en main.py: **ponlo igual que allí**. Este script no
# pasa por main.py y es el único de test/ que hace inferencia sostenida, que es
# donde el fallo de Pascal asoma. Y además hay que medir en las MISMAS
# condiciones en las que corre el servicio, o los milisegundos no valen nada.
CUDNN_ENABLED = True
torch.backends.cudnn.enabled = CUDNN_ENABLED


RAIZ = Path(__file__).resolve().parent.parent / "barridos"

# Cuántas inferencias se tiran antes de empezar a cronometrar. La primera de
# cada modelo incluye carga de kernels y subida de pesos, y la GTX 1080 puede
# venir de P8: cronometrar eso mide el arranque, no el modelo.
CALENTAMIENTO = 4


# ---------------------------------------------------------------------------
# GPU
# ---------------------------------------------------------------------------

class SondaGPU:
    """Muestrea la GPU en un hilo mientras se infiere.

    Se mide el % de utilización durante el bloque cronometrado porque el motivo
    de todo esto es el consumo: una recomendación sin ese número no sirve para
    decidir nada. Si pynvml no está, todo devuelve None y el barrido sigue.
    """

    def __init__(self, intervalo: float = 0.02):
        self._intervalo = intervalo
        self._muestras: list[int] = []
        self._stop = threading.Event()
        self._hilo: threading.Thread | None = None
        self._h = None
        try:
            import pynvml
            pynvml.nvmlInit()
            self._nvml = pynvml
            self._h = pynvml.nvmlDeviceGetHandleByIndex(0)
        except Exception:
            self._nvml = None

    @property
    def disponible(self) -> bool:
        return self._h is not None

    def pstate(self):
        if not self.disponible:
            return None
        try:
            return f"P{self._nvml.nvmlDeviceGetPerformanceState(self._h)}"
        except Exception:
            return None

    def _muestrear(self):
        try:
            self._muestras.append(
                self._nvml.nvmlDeviceGetUtilizationRates(self._h).gpu)
        except Exception:
            pass

    def _loop(self):
        # Muestrear ANTES de esperar: con pocos frames a imgsz pequeño el bloque
        # entero dura menos que un intervalo, y esperando primero el barrido
        # acababa sin una sola muestra y con la columna de GPU vacía.
        while True:
            self._muestrear()
            if self._stop.wait(self._intervalo):
                return

    def __enter__(self):
        self._muestras = []
        if self.disponible:
            self._stop.clear()
            self._hilo = threading.Thread(target=self._loop, daemon=True)
            self._hilo.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._hilo is not None:
            self._hilo.join(timeout=1.0)
            self._hilo = None

    def resumen(self) -> dict:
        if not self._muestras:
            return {"gpu_medio": None, "gpu_max": None}
        return {
            "gpu_medio": round(statistics.mean(self._muestras), 1),
            "gpu_max": max(self._muestras),
        }


def gpu_memory_mb() -> Optional[tuple[int, int]]:
    """(usada, total) de VRAM, o None sin pynvml.

    Se mira antes de medir porque otro programa dentro de la GPU falsea los
    milisegundos de todo el barrido. Antes esto listaba los procesos por nombre,
    que sería mucho mejor, pero en Windows con WDDM NVML no da la memoria por
    proceso y devuelve medio escritorio: o salía vacío o salía ruido. La memoria
    total sí es fiable, y basta para saber que hay alguien más.
    """
    try:
        import pynvml
        pynvml.nvmlInit()
        mem = pynvml.nvmlDeviceGetMemoryInfo(pynvml.nvmlDeviceGetHandleByIndex(0))
        return round(mem.used / 1024 ** 2), round(mem.total / 1024 ** 2)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Fase 1: capturar
# ---------------------------------------------------------------------------

def capturar(args) -> int:
    destino = RAIZ / f"{args.etiqueta}-{time.strftime('%Y%m%d-%H%M%S')}"
    destino.mkdir(parents=True, exist_ok=True)
    url = f"{args.host}/cameras/{args.camera}/snapshot?infer=false"

    # El anotado va a una SUBCARPETA a propósito: el barrido recorre los *.jpg
    # de la carpeta y estos no son frames de entrada, son la prueba de lo que el
    # servicio estaba dibujando en ese momento. Comparar "lo que el servicio
    # pintó" con "lo que el modelo encuentra en esos mismos píxeles después" es
    # lo que distingue un modelo que no ve de un servicio que falla.
    dir_anotado = destino / "anotado_por_el_servicio"
    if args.anotado:
        dir_anotado.mkdir(exist_ok=True)

    print(f"Capturando {args.frames} frames de {args.camera} en {destino}")
    print("(si da 503, la cámara está parada: POST /cameras/"
          f"{args.camera}/start)\n")

    guardados = 0
    for i in range(args.frames):
        try:
            r = requests.get(url, timeout=8)
        except Exception as e:
            print(f"  frame {i}: sin respuesta ({e!r})")
            break
        if r.status_code != 200:
            print(f"  frame {i}: HTTP {r.status_code} {r.text[:120]}")
            if r.status_code == 503:
                break
            continue
        p = destino / f"{i:03d}.jpg"
        p.write_bytes(r.content)
        guardados += 1
        img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
        forma = f"{img.shape[1]}x{img.shape[0]}" if img is not None else "ilegible"

        # El frame anotado es casi simultáneo, no el mismo exacto: /snapshot
        # espera a uno recién codificado. Da igual, lo que se quiere es la
        # prueba de qué estaba pintando el servicio en ese instante.
        nota = ""
        if args.anotado:
            try:
                ra = requests.get(
                    f"{args.host}/cameras/{args.camera}/snapshot?infer=true",
                    timeout=8)
                if ra.status_code == 200:
                    (dir_anotado / f"{i:03d}.jpg").write_bytes(ra.content)
                    nota = " (+anotado)"
            except Exception:
                pass

        print(f"  frame {i}: {len(r.content)} bytes, {forma}{nota}")
        time.sleep(args.intervalo)

    if not guardados:
        print("\nNo se ha capturado nada.")
        return 1
    print(f"\n{guardados} frames en {destino}")
    if args.anotado and dir_anotado.is_dir():
        print(f"Lo que el servicio dibujaba, en {dir_anotado.name}\\ "
              f"(no entra en el barrido).")
        print("  Si ahí no hay cajas pero el barrido SÍ las encuentra sobre los")
        print("  mismos frames, el fallo está en el servicio y no en el modelo.")

    # El comando listo para copiar, con los nombres REALES. Escribirlo a mano a
    # partir de un ejemplo con puntos suspensivos es justo donde se falla.
    otras = sorted(d.name for d in RAIZ.iterdir()
                   if d.is_dir() and not d.name.startswith("barrido-")
                   and d.name != destino.name)
    con = destino.name if args.etiqueta.startswith("con") else None
    sin = destino.name if not args.etiqueta.startswith("con") else None
    for n in otras:
        if con is None and n.startswith("con"):
            con = n
        if sin is None and n.startswith("sin"):
            sin = n

    if con and sin:
        print("\nYa tienes las dos tandas. Para el barrido:\n")
        print(f"  python test\\barrido_modelos.py barrer "
              f"--con-persona {con} --sin-nadie {sin}\n")
        print("  (desde detect\\; si lanzas desde detect\\test\\, quita el 'test\\'.")
        print("   Basta con el nombre de la carpeta, sin ruta.)")
    else:
        falta = "sin-nadie" if con else "con-persona"
        print(f"\nFalta la otra tanda. Repite con --etiqueta {falta}:\n")
        print(f"  python test\\barrido_modelos.py capturar --camera {args.camera} "
              f"--frames {args.frames} --etiqueta {falta}")
    return 0


# ---------------------------------------------------------------------------
# Fase 2: inferir
# ---------------------------------------------------------------------------

def resolver_carpeta(valor: str) -> Path:
    """Acepta ruta absoluta, relativa al directorio actual, o solo el nombre.

    El script se lanza indistintamente desde `detect\\` o desde `detect\\test\\`,
    y las capturas siempre viven en `detect/barridos/`. Obligar a acertar con la
    ruta relativa no aporta nada, así que si no existe tal cual se busca dentro
    de barridos/, y si tampoco, se listan las que sí hay.
    """
    p = Path(valor)
    if p.is_dir():
        return p
    candidata = RAIZ / p.name
    if candidata.is_dir():
        return candidata

    disponibles = sorted(d.name for d in RAIZ.iterdir()
                         if d.is_dir() and not d.name.startswith("barrido-")) \
        if RAIZ.is_dir() else []
    msg = [f"No existe la carpeta {valor!r}."]
    if disponibles:
        msg.append(f"\nCapturas disponibles en {RAIZ}:")
        msg += [f"    {n}" for n in disponibles]
        msg.append("\nPuedes pasar solo el nombre, sin la ruta.")
    else:
        msg.append(f"\nNo hay ninguna captura en {RAIZ}. "
                   f"Lánzalas antes con el subcomando 'capturar'.")
    raise SystemExit("\n".join(msg))


def cargar_frames(carpeta: Path) -> list[tuple[str, np.ndarray]]:
    out = []
    for p in sorted(carpeta.glob("*.jpg")):
        img = cv2.imread(str(p))
        if img is not None:
            out.append((p.name, img))
    if not out:
        raise SystemExit(f"No hay JPEG legibles en {carpeta}")
    return out


def inferir_lote(model, frames, imgsz: float, conf_min: float, sonda: SondaGPU):
    """Infiere todos los frames UNA vez, al umbral más bajo del barrido.

    Se corre una sola vez por (modelo, imgsz) y después se filtra por cada
    confianza, en vez de repetir la inferencia por umbral. Es equivalente: el
    NMS conserva siempre la caja de mayor puntuación y suprime las que se
    solapan por debajo, así que una caja de confianza baja nunca puede tapar a
    una alta. Y de paso el cronometraje sale limpio, porque el coste de
    inferir no depende del umbral.
    """
    # Calentar: la primera inferencia incluye carga de kernels, y la GPU puede
    # venir de P8 por el bug de P-state de la 1080.
    for _ in range(CALENTAMIENTO):
        model.predict(frames[0][1], imgsz=imgsz, conf=conf_min, verbose=False)

    resultados, tiempos = [], []
    with sonda:
        for nombre, img in frames:
            t0 = time.perf_counter()
            r = model.predict(img, imgsz=imgsz, conf=conf_min, verbose=False)[0]
            tiempos.append((time.perf_counter() - t0) * 1000)
            cajas = [
                (int(c), float(f), [float(v) for v in xy])
                for c, f, xy in zip(r.boxes.cls.tolist(),
                                    r.boxes.conf.tolist(),
                                    r.boxes.xyxy.tolist())
            ]
            resultados.append((nombre, img, cajas))
    return resultados, tiempos


def metricas(resultados, conf: float, clase: int) -> dict:
    """Cuántos frames tienen la clase buscada por encima del umbral."""
    con_clase, confianzas, cajas_clase, cajas_otras = 0, [], 0, 0
    for _nombre, _img, cajas in resultados:
        mejor = 0.0
        for c, f, _xy in cajas:
            if f < conf:
                continue
            if c == clase:
                cajas_clase += 1
                mejor = max(mejor, f)
            else:
                cajas_otras += 1
        if mejor > 0:
            con_clase += 1
            confianzas.append(mejor)
    n = len(resultados)
    return {
        "frames": n,
        "frames_con_clase": con_clase,
        "pct": round(100 * con_clase / n, 1) if n else 0.0,
        "cajas_clase": cajas_clase,
        "cajas_otras": cajas_otras,
        "conf_media": round(statistics.mean(confianzas), 3) if confianzas else None,
        "conf_min": round(min(confianzas), 3) if confianzas else None,
    }


def anotar(img, cajas, nombres, conf: float, titulo: str):
    """Misma idea de dibujo que POST /detect-file: cajas + overlay legible."""
    out = img.copy()
    h, w = out.shape[:2]
    lw = max(round((h + w) / 2 * 0.003), 2)
    escala, grosor = lw / 3, max(lw - 1, 1)
    n = 0
    for c, f, (x1, y1, x2, y2) in cajas:
        if f < conf:
            continue
        n += 1
        cv2.rectangle(out, (int(x1), int(y1)), (int(x2), int(y2)), (0, 255, 0), lw)
        cv2.putText(out, f"{nombres[c]} {f:.2f}", (int(x1), max(0, int(y1) - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, escala, (0, 255, 0), grosor, cv2.LINE_AA)
    texto = f"{titulo} | {n} det"
    (tw, th), base = cv2.getTextSize(texto, cv2.FONT_HERSHEY_SIMPLEX, escala, grosor)
    cv2.rectangle(out, (0, 0), (tw + 2 * lw, th + base + 2 * lw), (0, 0, 0), -1)
    cv2.putText(out, texto, (lw, th + lw), cv2.FONT_HERSHEY_SIMPLEX,
                escala, (255, 255, 0), grosor, cv2.LINE_AA)
    return out


# ---------------------------------------------------------------------------
# Fase 3: barrer
# ---------------------------------------------------------------------------

def barrer(args) -> int:
    modelos = [m.strip() for m in args.models.split(",") if m.strip()]
    tamanos = [int(s) for s in args.imgsz.split(",") if s.strip()]
    confianzas = sorted(float(c) for c in args.conf.split(",") if c.strip())
    conf_min = confianzas[0]

    con_persona = cargar_frames(resolver_carpeta(args.con_persona))
    sin_nadie = cargar_frames(resolver_carpeta(args.sin_nadie)) if args.sin_nadie else []

    if not sin_nadie:
        print("AVISO: sin tanda 'sin nadie' no se pueden contar falsos positivos,\n"
              "       y entonces 'hacer que el nano funcione' es solo bajar el umbral.\n")

    print("Cierra cualquier otro programa que use la GPU antes de medir "
          "(un Fusion 360 abierto falsea los milisegundos de todo el barrido).")
    mem = gpu_memory_mb()
    if mem:
        usada, total = mem
        print(f"  VRAM en uso ahora: {usada} de {total} MB"
              f"{'  <- parece que hay alguien más dentro' if usada > 2000 else ''}")
    print()

    salida = RAIZ / f"barrido-{time.strftime('%Y%m%d-%H%M%S')}"
    salida.mkdir(parents=True, exist_ok=True)

    sonda = SondaGPU()
    filas = []

    for nombre_modelo in modelos:
        # Con el nombre pelado, ultralytics busca el .pt relativo al directorio
        # ACTUAL: lanzando esto desde detect\test\ no lo encontraba y se bajaba
        # 47 MB otra vez, dejando copias sueltas por ahí. Se le da la ruta
        # absoluta cuando el fichero ya está en detect\.
        # (get_model ya le añade el .pt, así que se le pasa sin extensión)
        pt = RAIZ.parent / f"{nombre_modelo}.pt"
        model = get_model(str(pt.with_suffix("")) if pt.is_file() else nombre_modelo,
                          args.device)
        nombres = model.names
        for imgsz in tamanos:
            print(f"-- {nombre_modelo} @ {imgsz} "
                  f"({len(con_persona)}+{len(sin_nadie)} frames, "
                  f"pstate {sonda.pstate()})")

            res_con, tiempos = inferir_lote(model, con_persona, imgsz, conf_min, sonda)
            gpu = sonda.resumen()
            ms = round(statistics.median(tiempos), 1)

            res_sin = []
            if sin_nadie:
                res_sin, _ = inferir_lote(model, sin_nadie, imgsz, conf_min, sonda)

            for conf in confianzas:
                m_con = metricas(res_con, conf, args.clase)
                m_sin = metricas(res_sin, conf, args.clase) if res_sin else None
                filas.append({
                    "modelo": nombre_modelo, "imgsz": imgsz, "conf": conf,
                    "recall_pct": m_con["pct"],
                    "conf_media": m_con["conf_media"], "conf_min": m_con["conf_min"],
                    "fp_frames_pct": m_sin["pct"] if m_sin else None,
                    "fp_cajas": m_sin["cajas_clase"] if m_sin else None,
                    "otras_cajas": m_con["cajas_otras"] + (m_sin["cajas_otras"] if m_sin else 0),
                    "ms": ms,
                    # El % de GPU de arriba se mide infiriendo sin pausa, que NO
                    # es como corre el servicio: ahí se infiere un frame y se
                    # espera al siguiente. La carga real es la fracción del
                    # tiempo ocupada, o sea ms por los fps de la cámara. Es el
                    # número que contesta "¿cuánta GPU me va a costar?".
                    "carga_pct": round(min(100.0, ms * args.fps / 10), 1),
                    **gpu,
                })

                # Anotadas solo de los primeros frames: el objetivo es poder
                # mirarlas, y una imagen por frame y combinación son cientos.
                if args.anotar:
                    carpeta = salida / f"{nombre_modelo}_{imgsz}_conf{conf}"
                    carpeta.mkdir(exist_ok=True)
                    for etiqueta, res in (("con", res_con), ("sin", res_sin)):
                        for nombre, img, cajas in res[:args.anotar]:
                            cv2.imwrite(
                                str(carpeta / f"{etiqueta}_{nombre}"),
                                anotar(img, cajas, nombres, conf,
                                       f"{nombre_modelo} {imgsz} c{conf}"))

    informe(filas, salida, args)
    return 0


def informe(filas, salida: Path, args):
    (salida / "resultados.json").write_text(
        json.dumps(filas, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n{'modelo':<10} {'imgsz':>5} {'conf':>5} {'recall':>7} {'conf.med':>8} "
          f"{'conf.min':>8} {'FP%':>6} {'FPcaj':>6} {'ms':>7} "
          f"{'carga@' + str(args.fps) + 'fps':>12}")
    print("-" * 84)
    for f in sorted(filas, key=lambda r: (r["ms"], -r["recall_pct"])):
        print(f"{f['modelo']:<10} {f['imgsz']:>5} {f['conf']:>5} "
              f"{f['recall_pct']:>6.1f}% "
              f"{_n(f['conf_media']):>8} {_n(f['conf_min']):>8} "
              f"{_n(f['fp_frames_pct'], '%'):>6} {_n(f['fp_cajas']):>6} "
              f"{f['ms']:>6.1f} {f['carga_pct']:>11.0f}%")
    print(f"\n(carga@{args.fps}fps = ms x fps: la fracción de GPU que ocupará EN MARCHA. "
          f"El % medido\n durante el barrido es más alto porque ahí se infiere sin "
          f"pausa, que no es el caso real.)")

    # Línea base: lo que se sabe que funciona hoy.
    base = next((f for f in filas if f["modelo"] == args.base_modelo
                 and f["imgsz"] == args.base_imgsz), None)

    validas = [f for f in filas
               if f["recall_pct"] >= args.recall_min
               and (f["fp_cajas"] in (0, None))]
    print()
    if not validas:
        # Las dos causas piden cosas distintas, así que conviene distinguirlas:
        # con recall alto y falsos positivos el problema es el umbral; sin
        # recall en ningún sitio, el problema es la imagen o el modelo.
        con_recall = [f for f in filas if f["recall_pct"] >= args.recall_min]
        sin_fp = [f for f in filas if f["fp_cajas"] in (0, None)]
        print(f"Ninguna combinación llega a {args.recall_min}% de recall sin falsos "
              f"positivos.")
        if con_recall and not sin_fp:
            print("  Detectan, pero TODAS dan falsos positivos en la tanda sin nadie.")
            print("  Revisa que esa tanda no tenga de verdad a alguien en el encuadre,")
            print("  y mira las anotadas: si las cajas falsas son de otra clase, filtra")
            print("  por clase en vez de subir el umbral.")
        elif not con_recall:
            print(f"  Ninguna pasa del {max((f['recall_pct'] for f in filas), default=0)}% "
                  f"de recall, ni siquiera el modelo grande al umbral más bajo.")
            print("  Eso ya no es el modelo: mira las anotadas y la imagen de la cámara")
            print("  (luz, enfoque, resolución, compresión del ESP32).")
        else:
            print("  Las que detectan no son las que evitan falsos positivos.")
    else:
        # Más barata primero; a igualdad de coste, la de umbral MÁS ALTO: si dos
        # configuraciones cuestan lo mismo y detectan igual, la de umbral alto
        # aguanta mejor una escena que no estaba en la captura.
        mejor = min(validas, key=lambda f: (f["ms"], -f["conf"]))
        print(f"RECOMENDACIÓN: {mejor['modelo']} imgsz={mejor['imgsz']} "
              f"conf={mejor['conf']}")
        print(f"  recall {mejor['recall_pct']}%, confianza mínima "
              f"{_n(mejor['conf_min'])}, {mejor['ms']} ms "
              f"-> {mejor['carga_pct']}% de GPU a {args.fps} fps")
        if mejor["conf_min"] is not None and mejor["conf_min"] - mejor["conf"] < 0.1:
            print(f"  AVISO: la confianza mínima ({mejor['conf_min']}) casi roza el "
                  f"umbral ({mejor['conf']}). Poco margen: se romperá sola en cuanto "
                  f"cambie la luz.")
        if base and base["ms"]:
            delta = 100 * (1 - mejor["ms"] / base["ms"])
            verbo = "ahorra" if delta >= 0 else "cuesta"
            print(f"  frente a {base['modelo']}@{base['imgsz']} "
                  f"({base['ms']} ms, {base['carga_pct']}% de GPU): "
                  f"{verbo} un {abs(delta):.0f}%, "
                  f"{base['carga_pct']}% -> {mejor['carga_pct']}% de GPU")

    print(f"\nImágenes anotadas y resultados.json en {salida}")
    print("Míralas antes de decidir: la tabla no distingue 'encontró a la persona' "
          "de 'encontró una sombra con forma de persona'.")


def _n(v, suf=""):
    return "-" if v is None else f"{v}{suf}"


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capturar", help="guarda frames reales de una cámara")
    c.add_argument("--camera", required=True)
    c.add_argument("--etiqueta", required=True,
                   help="con-persona / sin-nadie: hacen falta las dos tandas")
    c.add_argument("--frames", type=int, default=20)
    c.add_argument("--intervalo", type=float, default=0.5)
    c.add_argument("--sin-anotado", dest="anotado", action="store_false",
                   help="no guardar además lo que el servicio está dibujando")
    c.add_argument("--host", default="http://127.0.0.1:8080")
    c.set_defaults(func=capturar)

    b = sub.add_parser("barrer", help="barre modelos x imgsz x conf sobre lo capturado")
    b.add_argument("--con-persona", required=True, dest="con_persona")
    b.add_argument("--sin-nadie", default=None, dest="sin_nadie")
    b.add_argument("--models", default="yolo26n,yolo26m")
    b.add_argument("--imgsz", default="640,960,1280")
    b.add_argument("--conf", default="0.1,0.2,0.3,0.4")
    b.add_argument("--clase", type=int, default=0, help="clase COCO a buscar (0=persona)")
    b.add_argument("--device", default="cuda")
    b.add_argument("--anotar", type=int, default=3,
                   help="cuántos frames de cada tanda guardar anotados por combinación")
    b.add_argument("--fps", type=float, default=15.0,
                   help="fps reales de la cámara, para estimar la carga de GPU "
                        "en marcha (mira pipeline_fps en /status)")
    b.add_argument("--recall-min", type=float, default=90.0, dest="recall_min")
    b.add_argument("--base-modelo", default="yolo26m", dest="base_modelo")
    b.add_argument("--base-imgsz", type=int, default=640, dest="base_imgsz")
    b.set_defaults(func=barrer)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
