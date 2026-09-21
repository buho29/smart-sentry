"""Convertir una ristra de JPEG en un MP4 que Home Assistant sepa reproducir.

El detalle que manda en todo este módulo: **el códec tiene que ser H.264**. Los
navegadores (y los WebView de la app de HA) reproducen H.264, H.265, VP9 y AV1,
y nada más. Un `.mp4` con `mp4v` —que es MPEG-4 Part 2, lo que sale por defecto
de `cv2.VideoWriter` en Windows— lo abre VLC tan contento y en el navegador se ve
un reproductor en negro, sin ningún mensaje de error. Es el fallo más caro de
diagnosticar de toda la grabación, así que aquí se evita por construcción y, si
no queda más remedio que caer en `mp4v`, se marca el clip como no reproducible en
vez de dejar que el usuario lo descubra días después.

El OpenCV del proyecto trae un FFmpeg LGPL **sin libx264**, así que
`VideoWriter` con fourcc `avc1` no puede codificar H.264: devuelve
`isOpened() == False` sin lanzar excepción. Por eso el camino preferente es un
`ffmpeg.exe` aparte (el de `imageio-ffmpeg`, que sí trae libx264), alimentado por
stdin. Tiene además una ventaja gorda: los JPEG se le pasan tal cual con
`-f image2pipe`, así que **en Python no se decodifica ni un frame**.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Protocol

from log import print


class VideoEncoder(Protocol):
    """Lo que el hilo escritor necesita de un codificador.

    Deliberadamente mínimo para que los tests puedan inyectar un doble que solo
    apunta los bytes recibidos, sin ffmpeg ni disco.
    """

    name: str
    # Si el fichero resultante se puede reproducir en un navegador. False
    # significa "se ha grabado, pero en HA se verá en negro".
    playable_in_browser: bool

    def open(self, path: Path, width: int, height: int, fps: float) -> None: ...

    def write(self, jpeg: bytes) -> None: ...

    def close(self, timeout: float = 5.0) -> None: ...


class FfmpegEncoder:
    """Tubería a un ffmpeg externo: JPEG por stdin, H.264 por la salida.

    `-pix_fmt yuv420p` NO es opcional. El MJPEG del ESP32 es 4:2:2, y sin
    forzar el pixel format x264 produce un H.264 yuv422p perfectamente válido
    que ningún navegador decodifica: mismo síntoma que el `mp4v` (vídeo en
    negro) y todavía más difícil de achacar.
    """

    def __init__(self, exe: str, crf: int = 23, preset: str = "veryfast",
                 faststart: bool = True):
        self.exe = exe
        self.crf = crf
        self.preset = preset
        self.faststart = faststart
        self.name = "ffmpeg/libx264"
        self.playable_in_browser = True
        self._proc: Optional[subprocess.Popen] = None
        self._path: Optional[Path] = None

    def open(self, path: Path, width: int, height: int, fps: float) -> None:
        # +faststart deja el índice al principio del fichero, que es lo que
        # permite empezar a ver el clip sin descargarlo entero. El precio es que
        # exige un cierre limpio: un proceso matado a lo bruto deja el .part
        # inservible. Se asume porque el cierre sucio ya está cubierto (el .part
        # se rescata al arrancar) y porque el caso normal —ver el clip desde HA,
        # por la red— es el que se quiere rápido.
        movflags = "+faststart" if self.faststart else "+frag_keyframe+empty_moov"
        cmd = [
            self.exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "image2pipe", "-vcodec", "mjpeg", "-framerate", f"{fps:.3f}", "-i", "-",
            "-c:v", "libx264", "-preset", self.preset, "-crf", str(self.crf),
            "-pix_fmt", "yuv420p", "-movflags", movflags,
            str(path),
        ]
        self._path = path
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            # Sin esto, en Windows cada clip abre una ventana de consola.
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )

    def write(self, jpeg: bytes) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write(jpeg)
        except (BrokenPipeError, OSError) as e:
            # ffmpeg se ha muerto (disco lleno, JPEG corrupto...). Se cierra la
            # tubería para que el hilo escritor deje de alimentar un proceso
            # zombi y el clip se dé por terminado.
            self._proc.stdin = None
            raise RuntimeError(f"ffmpeg dejó de aceptar frames: {e!r}") from e

    def close(self, timeout: float = 5.0) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Se acabó el presupuesto (típicamente en el apagado del servicio).
            # El fichero queda a medias, pero sigue siendo un .part y se rescata
            # en el próximo arranque.
            print(f"ffmpeg no cerró en {timeout}s, se mata ({self._path})")
            proc.kill()
            proc.wait(timeout=2)
        if proc.returncode not in (0, None):
            err = b""
            try:
                err = proc.stderr.read() if proc.stderr else b""
            except OSError:
                pass
            print(f"ffmpeg terminó con código {proc.returncode}: "
                  f"{err.decode('utf-8', 'replace').strip()[:300]}")


class OpenCvEncoder:
    """Respaldo sin dependencias externas, con el aviso por delante.

    Solo se usa cuando no hay ningún ffmpeg a mano. Intenta `avc1` (H.264) y,
    si OpenCV no puede —que es lo normal con el FFmpeg LGPL que trae el
    paquete—, cae a `mp4v` y **se marca como no reproducible en navegador**.
    Decodifica cada JPEG con `imdecode`, así que además cuesta CPU.
    """

    def __init__(self, fourcc: str = "avc1"):
        self.fourcc = fourcc
        self.name = f"opencv/{fourcc}"
        self.playable_in_browser = fourcc.lower() in ("avc1", "h264", "x264")
        self._writer = None

    def open(self, path: Path, width: int, height: int, fps: float) -> None:
        import cv2

        def _try(fourcc: str):
            w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*fourcc),
                                fps, (width, height))
            return w if w.isOpened() else None

        writer = _try(self.fourcc)
        if writer is None and self.fourcc.lower() != "mp4v":
            # isOpened() False es el modo de fallo habitual: OpenCV escribe un
            # aviso por stderr y devuelve un writer muerto, sin excepción.
            print(f"OpenCV no puede codificar '{self.fourcc}'; se graba en mp4v. "
                  f"ESTOS CLIPS NO SE VEN EN EL NAVEGADOR NI EN HOME ASSISTANT: "
                  f"instala imageio-ffmpeg para tener H.264.")
            writer = _try("mp4v")
            self.fourcc = "mp4v"
            self.name = "opencv/mp4v"
            self.playable_in_browser = False
        if writer is None:
            raise RuntimeError(f"OpenCV no pudo abrir {path} para escribir vídeo")
        self._writer = writer

    def write(self, jpeg: bytes) -> None:
        import cv2
        import numpy as np

        if self._writer is None:
            return
        frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return  # JPEG corrupto: se salta el frame, no se rompe el clip
        self._writer.write(frame)

    def close(self, timeout: float = 5.0) -> None:
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.release()


def find_ffmpeg(explicit: Optional[str] = None) -> Optional[str]:
    """Localiza un ffmpeg utilizable, en orden de preferencia.

    El de `imageio-ffmpeg` va el último de los que sirven pero es el que se
    instala con el servicio, así que en la práctica es el que se usa. Se le
    pregunta dentro de un try porque el paquete es opcional.
    """
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)
        print(f"ffmpeg_path apunta a algo que no existe: {explicit}")
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def resolve_encoder(prefer: str = "auto", ffmpeg_path: Optional[str] = None,
                    fourcc: str = "avc1", crf: int = 23,
                    preset: str = "veryfast") -> tuple[VideoEncoder, str]:
    """Construye el encoder que toca y explica por qué ese.

    Devuelve (encoder, motivo). El motivo se guarda en el sidecar del clip y
    sale en /status: cuando alguien pregunte "¿por qué no veo el vídeo?", la
    respuesta tiene que estar ahí y no en un log de hace tres días.
    """
    if prefer in ("auto", "ffmpeg"):
        exe = find_ffmpeg(ffmpeg_path)
        if exe:
            return FfmpegEncoder(exe, crf=crf, preset=preset), f"ffmpeg en {exe}"
        if prefer == "ffmpeg":
            raise RuntimeError(
                "encoder='ffmpeg' pero no hay ningún ffmpeg: instala imageio-ffmpeg "
                "o pon ffmpeg_path")
        print("No se ha encontrado ffmpeg; se graba con OpenCV. "
              "Instala imageio-ffmpeg si los clips deben verse en Home Assistant.")
    return OpenCvEncoder(fourcc), f"OpenCV con fourcc '{fourcc}'"


def encoder_capabilities() -> dict:
    """Qué se puede codificar en esta máquina. Para /recordings/capabilities."""
    exe = find_ffmpeg()
    caps = {"ffmpeg": exe, "h264": bool(exe), "encoder": None, "reason": None}
    try:
        enc, reason = resolve_encoder("auto")
        caps["encoder"] = enc.name
        caps["reason"] = reason
        caps["h264"] = enc.playable_in_browser
    except Exception as e:
        caps["reason"] = repr(e)
    if not caps["h264"]:
        caps["warning"] = ("Sin H.264 los clips no se reproducen en el navegador "
                           "ni en Home Assistant. Instala imageio-ffmpeg.")
    return caps


def jpeg_size(data: bytes) -> Optional[tuple[int, int]]:
    """(ancho, alto) de un JPEG leyendo solo su cabecera.

    Hace falta para abrir el encoder y para detectar que la placa ha cambiado de
    resolución a media grabación. Se lee la cabecera en vez de llamar a
    `cv2.imdecode` porque esto corre en el hilo de la cámara: son unos cuantos
    saltos sobre un bytes, del orden de microsegundos, frente a descomprimir la
    imagen entera.
    """
    # Marcadores SOF (inicio de frame) que llevan las dimensiones. Se excluyen
    # C4 (tablas Huffman), C8 (reservado) y CC (aritmética), que comparten rango
    # pero no son SOF.
    sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
           0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    i, n = 2, len(data)
    if n < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return None
    while i + 3 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = (data[i + 2] << 8) | data[i + 3]
        if marker in sof:
            if i + 9 > n:
                return None
            h = (data[i + 5] << 8) | data[i + 6]
            w = (data[i + 7] << 8) | data[i + 8]
            return (w, h) if w and h else None
        i += 2 + seg_len
    return None


def frames_for_gap(dt: float, fps: float, max_repeat: int = 8) -> int:
    """Cuántas veces escribir este frame para que el clip dure lo que el evento.

    El MP4 se graba a fps constante, pero el pipeline no lo es: la misma cámara
    da 15 fps de cerca y 4 fps con mala señal. Si se escribiera un frame por
    frame recibido, veinte segundos de evento a 4 fps saldrían como un clip de
    cinco segundos acelerado.

    La corrección es repetir el frame cuando el hueco real es mayor que el
    periodo del fichero. A x264 un frame idéntico al anterior le sale casi
    gratis. `max_repeat` acota el caso feo —la placa se durmió a media
    grabación— para que un corte de dos minutos no genere mil quinientos frames
    de la misma imagen.
    """
    if fps <= 0:
        return 1
    n = int(round(dt * fps))
    return max(1, min(n, max_repeat))
