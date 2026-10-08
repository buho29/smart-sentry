"""Seguimiento con servos: mueve una torreta pan/tilt para centrar un objetivo.

Es el primer consumidor de detecciones (ver `detections.py`): recibe las cajas
de cada frame desde el hilo de proceso de la cámara y traduce "el objetivo está
descentrado" en órdenes de servo hacia la placa.

El contrato con el firmware es un servicio de la API nativa de ESPHome llamado
`set_servo_position` con dos variables float, `pan` y `tilt`, **en el rango
-1.0 a 1.0** (lo que espera `servo.write` de ESPHome). No se manejan grados en
ninguna capa: no sabemos ni la geometría del montaje ni el campo de visión de la
lente, así que hablar de ángulos absolutos sería inventarse una precisión que no
existe.

Por ese mismo motivo el control es proporcional y relativo: en vez de calcular
"el objetivo está a 12 grados, apunta ahí", se corrige un poco en la dirección
del error en cada frame y se deja que el lazo cerrado converja. Sale estable con
ganancia baja y no necesita calibración.
"""

from __future__ import annotations

import time
from typing import Optional

from pydantic import BaseModel

from detections import Detection


class ServoConfig(BaseModel):
    """Ajustes del seguimiento, persistidos en cameras_config.json.

    Una cámara sin servos simplemente no trae esta sección (`servo: None`), y
    entonces la sesión no crea el consumidor.
    """

    enabled: bool = True
    # Cómo se llama en el YAML de esta placa el servicio que mueve la torreta.
    # Va en la config y no cableado en el código para que una variante pueda
    # nombrarlo distinto sin tocar nada.
    service: str = "set_servo_position"
    # Servicio que anula el auto-detach de la placa mientras el seguimiento
    # está activo. Con el PWM cortado el servo obedece a picos espurios de la
    # señal y el pan daba giros solos de ~30°; con detección intermitente
    # (de noche) soltarlo al perder el objetivo lo dejaba expuesto. Un
    # firmware sin el servicio simplemente suelta como siempre.
    hold_service: str = "set_servo_hold"
    # Fracción del error que se corrige en cada envío. Más alto = más rápido
    # pero con riesgo de sobrepasar el objetivo y oscilar. Medido en la
    # torreta del servo (pan, imagen girada 480 de ancho): una unidad de servo
    # desplaza la caja ~900 px, así que corregir el error entero sería ~0.27;
    # 0.15 corrige ~55% por vez. Con 0.3 (~110%) se pasaba siempre y oscilaba.
    gain: float = 0.15
    # Error normalizado por debajo del cual un eje se considera centrado y NO
    # se mueve. Sin zona muerta el servo tiembla sin parar persiguiendo el
    # ruido de la caja, que en una persona quieta baila más de un 6% entre
    # frames.
    deadzone: float = 0.08
    # Tiempo mínimo entre envíos. Tiene que dar tiempo a que el servo llegue y
    # a que la imagen lo refleje (0.2-0.42 s medidos hasta el primer cambio):
    # si no, se corrige otra vez el mismo error y la torreta se pasa. Con la
    # placa interpolando (number "Servo transition") cada paso es una rampa
    # suave, no un golpe.
    min_interval_sec: float = 0.6
    # Según cómo quede montado cada servo, corregir "a la derecha" puede ser
    # sumar o restar. Se ajusta aquí en vez de recompilando el firmware.
    invert_pan: bool = False
    invert_tilt: bool = False
    # Relación de engranajes de cada eje: vueltas del servo por cada vuelta de
    # la cámara, o sea dientes del engranaje de la cámara entre dientes del del
    # servo. Un tilt con piñón de 21 en el servo y corona de 63 en la cámara es
    # 63/21 = 3: la cámara gira un tercio de lo que gira el servo, así que el
    # paso se multiplica por 3 para que ese eje centre igual de rápido que el
    # otro. Solo afecta al seguimiento; move_to y home_* van en unidades de
    # servo, que es lo que recibe servo.write. Ojo: dos engranajes que engranan
    # directamente giran en sentidos opuestos, así que al añadir una reducción
    # a un eje suele haber que dar la vuelta también a su invert_*.
    pan_gear_ratio: float = 1.0
    tilt_gear_ratio: float = 1.0
    # Recorrido máximo de cada eje, simétrico, en unidades de servo: ninguna
    # orden (ni del seguimiento ni manual) sale de [-limit, +limit]. Los
    # extremos del PWM suelen coincidir con el tope mecánico, y un servo
    # pequeño forzado ahí consume mucha corriente y puede romper engranajes o
    # quemarse.
    pan_limit: float = 0.9
    tilt_limit: float = 0.9
    # Tiempo sin ver el objetivo antes de soltarlo y poder enganchar otro.
    lost_target_sec: float = 1.5
    # Posición de reposo, usada al arrancar y al apagar.
    home_pan: float = 0.0
    home_tilt: float = 0.0
    # Al perder el objetivo, ¿volver a reposo? Por defecto no: en una torreta
    # suele interesar más quedarse mirando donde se perdió, que es por donde
    # probablemente reaparezca.
    return_home_on_lost: bool = False
    # Anticipación horizontal, en segundos: se apunta a donde estará el
    # objetivo dentro de este tiempo según su velocidad en la imagen, en vez
    # de a donde está. La torreta va por delante y queda más aire en la
    # dirección en la que se mueve. 0 = sin anticipar, el de por defecto: con
    # una persona cerca y sentada, su balanceo natural se colaba como
    # velocidad (decenas de px/s) y la anticipación lo amplificaba en una
    # oscilación. Útil solo para objetivos que cruzan la imagen andando. Solo
    # actúa si el objetivo ya está fuera de la zona muerta: uno centrado no
    # mueve la torreta aunque ande.
    lead_sec: float = 0.0


# A cuántos px del borde de la imagen se considera que la caja está cortada:
# el objeto se sale por ese lado. No es exactamente 0 porque YOLO rara vez
# pega la caja al píxel.
_EDGE_PX = 2

# Error con el que se mueve un eje cuando la caja está cortada por un lado. No
# se puede medir cuánto falta (con y1=0 da igual un pelo que media cabeza), así
# que se avanza a paso fijo y se deja que el lazo lo repita si hace falta. Con
# 1.0 (paso máximo) el tilt, multiplicado por la reducción, daba saltos de 0.6
# de servo, se pasaba al extremo contrario y ByteTrack perdía el ID.
_CLIPPED_EDGE_ERROR = 0.4

# object_id de los number de ajuste que publica el firmware de la torreta
# (esphome/esp32-s3-cam-servo.yaml): velocidad máxima, suavizado y auto-detach.
_BOARD_TRANSITION = "servo_transition"
_BOARD_SMOOTHING = "servo_smoothing"
_BOARD_AUTO_DETACH = "servo_auto_detach"

# Máximo que se aleja una orden del seguimiento de la anterior, por eje y en
# giro de cámara (se multiplica por el gear_ratio del eje). Red de seguridad
# contra una ganancia alta o un error grande: ningún frame puede mandar un
# salto que deje al objetivo fuera de la imagen.
_MAX_STEP = 0.15

# Saltos a partir de los cuales se deja constancia en el log, para poder
# explicar después un giro brusco (en unidades de servo, -1..1).
_LOG_JUMP = 0.15

# Cada cuánto se repite el hold con el seguimiento activo. El firmware lo da por
# caducado a los 10 s sin refresco (por si este servicio cae); repetirlo
# además lo recupera si la placa se reinicia o se pierde un mensaje.
_HOLD_REFRESH_SEC = 3.0


# Franja superior, en fracción del alto, dentro de la cual el centrado ya no
# puede bajar más la cámara. Sin ella, con alguien más alto que media imagen el
# centro de la caja queda abajo: centrar bajaba paso a paso hasta cortar la
# cabeza, el borde de arriba subía de golpe, y vuelta a empezar. Tiene que
# ser ancha: con el retardo del stream un paso de centrado se pasaba de una
# franja del 10% y cortaba la cabeza igual.
_TOP_GUARD_FRACTION = 0.25

# Tiempo desde la última orden a partir del cual se da la cámara por quieta y
# los frames sirven para medir la velocidad del objetivo. Antes, lo que se
# mueve en la imagen es sobre todo la propia torreta (más el retardo del
# stream, que entrega frames de cuando aún giraba). Medido: el primer cambio
# llega a 0.2-0.42 s de la orden y la imagen estable hacia los 0.6 s; con 0.25
# se medía el propio giro como velocidad del objetivo. Con órdenes continuas
# (min_interval_sec corto) solo se mide cuando la torreta descansa en la zona
# muerta: la anticipación ayuda sobre todo al arrancar a moverse el objetivo.
_SETTLE_SEC = 0.6

# Suavizado de la velocidad medida (media exponencial): peso de la muestra
# nueva. La caja baila unos px entre frames y sin suavizar la anticipación
# temblaría con ella.
_VELOCITY_ALPHA = 0.5

# Tope de la anticipación, en fracción del ancho: por mucho que corra el
# objetivo, no apuntar más allá de un cuarto de imagen por delante.
_MAX_LEAD_FRACTION = 0.25

# Por debajo de esta velocidad (px/s) no se anticipa: es el temblor de la caja
# con el objetivo quieto, y anticiparlo solo lo amplificaría.
_MIN_LEAD_SPEED = 40.0

# La velocidad medida caduca pasado este tiempo sin medida nueva. Con órdenes
# continuas la cámara casi nunca está quieta para medir, y sin caducidad la
# última velocidad se quedaba congelada: un sesgo fijo de decenas de px
# empujando hacia un lado con el objetivo quieto.
_LEAD_STALE_SEC = 1.0

# En horizontal, los bordes solo se persiguen si la caja ocupa menos de esta
# fracción del ancho. Una persona a 1 m llena casi todo el cuadro y toca los
# lados casi siempre: perseguirlos daba golpes de un lado a otro.
_SIDE_CHASE_MAX_FRACTION = 0.5


# Solape mínimo (IoU) con la última caja del objetivo para dar por hecho que un
# ID nuevo es la misma persona. ByteTrack cambia el ID de alguien cercano con
# solo mover un brazo; 0.3 deja pasar ese cambio de postura y no engancha a
# otra persona al lado.
_RELOCK_IOU = 0.3


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """Intersección sobre unión de dos cajas (x1, y1, x2, y2)."""
    iw = min(a[2], b[2]) - max(a[0], b[0])
    ih = min(a[3], b[3]) - max(a[1], b[1])
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _axis_error(lo: float, hi: float, size: int, center_err: float,
                chase_lo: bool, chase_hi: bool) -> tuple[float, Optional[str]]:
    """Error de un eje: centrar, salvo que la caja esté cortada por un lado
    que se persigue.

    `lo`/`hi` son los bordes de la caja en ese eje (x1/x2 o y1/y2) y `size` el
    ancho o alto del frame. Un error positivo acerca la caja al lado `lo` de la
    imagen (izquierda o arriba), uno negativo la acerca a `hi`.

    Si un lado perseguido está cortado, se va hacia él como mínimo a paso
    máximo, porque el objeto se está saliendo por ahí. `lo` gana si están
    cortados los dos: en vertical es la cabeza.

    Devuelve el error y qué lado cortado lo decidió ("lo", "hi" o None), solo
    para el log.
    """
    if chase_lo and lo <= _EDGE_PX:
        return min(center_err, -_CLIPPED_EDGE_ERROR), "lo"
    if chase_hi and hi >= size - _EDGE_PX:
        return max(center_err, _CLIPPED_EDGE_ERROR), "hi"
    return center_err, None


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


class ServoTracker:
    """Consumidor de detecciones que centra el objetivo con dos servos.

    Elegir objetivo se hace por **bloqueo de track ID**: se engancha uno y se
    sigue mientras siga visible, en vez de recalcular el "mejor" cada frame.
    Con dos objetos en escena, elegir cada frame haría que la torreta saltara
    entre ellos en cuanto uno se acercara un píxel más al centro.
    """

    def __init__(self, cfg: ServoConfig, esphome, camera_id: str = ""):
        # `esphome` es un EsphomeController, pero aquí solo se le piden
        # call_service() y has_service(): cualquier doble sirve para los
        # tests.
        self.cfg = cfg
        self._esphome = esphome
        self._camera_id = camera_id

        self.pan = cfg.home_pan
        self.tilt = cfg.home_tilt
        self.target_id: Optional[int] = None
        # Clase y última caja vista del objetivo, para reengancharlo si
        # ByteTrack le cambia el ID.
        self._target_label: Optional[str] = None
        self._last_box: Optional[tuple[float, float, float, float]] = None
        self._last_seen: float = 0.0
        self._last_send: float = 0.0
        self._sends = 0
        # Último objetivo sobre el que se calculó una corrección, para /status.
        self.last_target: Optional[dict] = None
        # Velocidad horizontal del objetivo en px/s, medida solo con la cámara
        # quieta, y la última muestra quieta (instante, track_id, x1, x2).
        self._vx: float = 0.0
        self._vx_at: float = 0.0  # instante de la última medida de velocidad
        self._still_sample: Optional[tuple[float, int, float, float]] = None
        # Último hold pedido a la placa y cuándo, para refrescarlo.
        self._hold = False
        self._hold_sent_at: float = 0.0

    # -- elección de objetivo ------------------------------------------------

    def _pick_target(self, dets: list[Detection], width: int, height: int) -> Optional[Detection]:
        """Mantiene el objetivo bloqueado si sigue ahí; si no, engancha otro.

        Solo se consideran cajas con track_id: sin ID confirmado, ByteTrack no
        garantiza que la caja de este frame sea el mismo objeto que la del
        anterior, y bloquearse a eso sería bloquearse a nada.
        """
        with_id = [d for d in dets if d.track_id is not None]

        if self.target_id is not None:
            current = next((d for d in with_id if d.track_id == self.target_id), None)
            if current is None and self._last_box is not None:
                current = self._relock(with_id)
            if current is not None:
                self._last_seen = time.monotonic()
                self._last_box = (current.x1, current.y1, current.x2, current.y2)
                return current
            # Perdido, pero puede ser una oclusión de un par de frames: se le da
            # margen antes de soltarlo, para no cambiar de objetivo por un
            # parpadeo del detector.
            if time.monotonic() - self._last_seen < self.cfg.lost_target_sec:
                return None
            self._release_target()

        if not with_id:
            return None

        # Objetivo nuevo: el más cercano al centro, que es el que menos hay que
        # mover la torreta para atender.
        cx0, cy0 = width / 2, height / 2
        new_target = min(with_id, key=lambda d: (d.cx - cx0) ** 2 + (d.cy - cy0) ** 2)
        self.target_id = new_target.track_id
        self._target_label = new_target.label
        self._last_seen = time.monotonic()
        self._last_box = (new_target.x1, new_target.y1, new_target.x2, new_target.y2)
        self._vx = 0.0  # la velocidad era la del objetivo anterior
        self._still_sample = None
        print(f"[{self._camera_id}] servo: objetivo #{self.target_id} "
              f"({new_target.label} {new_target.conf:.2f}) en "
              f"({new_target.cx:.0f}, {new_target.cy:.0f}) de {width}x{height}")
        return new_target

    def _relock(self, with_id: list[Detection]) -> Optional[Detection]:
        """El ID bloqueado ha desaparecido: ¿hay un ID nuevo que sea la misma
        persona? Se busca la caja de la misma clase que más solape con la
        última vista. Si pasa de `_RELOCK_IOU` se cambia de ID en el acto, sin
        esperar `lost_target_sec` y conservando la velocidad medida.
        """
        candidates = [d for d in with_id if d.label == self._target_label]
        if not candidates:
            return None
        best = max(candidates, key=lambda d: _iou(self._last_box, (d.x1, d.y1, d.x2, d.y2)))
        iou = _iou(self._last_box, (best.x1, best.y1, best.x2, best.y2))
        if iou < _RELOCK_IOU:
            return None
        print(f"[{self._camera_id}] servo: reenganche #{self.target_id} -> "
              f"#{best.track_id} (IoU {iou:.2f})")
        self.target_id = best.track_id
        # La muestra de velocidad era del ID viejo: se descarta, pero no la
        # velocidad, que es de la misma persona.
        self._still_sample = None
        return best

    def _release_target(self):
        if self.target_id is None:
            return
        print(f"[{self._camera_id}] servo: objetivo #{self.target_id} perdido")
        self.target_id = None
        self._last_box = None
        if self.cfg.return_home_on_lost:
            self._send(self.cfg.home_pan, self.cfg.home_tilt, force=True,
                       reason="objetivo perdido, a reposo")

    # -- envío ---------------------------------------------------------------

    def _set_hold(self, hold: bool) -> None:
        """Pide a la placa que no suelte los servos (hold) o que vuelva a su
        auto-detach. Sin conexión o sin el servicio, call_service no hace nada."""
        self._hold = hold
        self._hold_sent_at = time.monotonic()
        self._esphome.call_service(self.cfg.hold_service, hold=hold)

    def _sync_hold(self) -> None:
        """Hold = seguimiento activo, haya objetivo o no. Se manda al cambiar
        y, activo, se repite cada _HOLD_REFRESH_SEC."""
        want = self.cfg.enabled
        if want != self._hold or \
                (want and time.monotonic() - self._hold_sent_at >= _HOLD_REFRESH_SEC):
            self._set_hold(want)

    def set_config(self, cfg: ServoConfig) -> None:
        """Cambia la config en caliente. Apagar el seguimiento suelta el hold
        en el acto: con enabled=False no hay inferencia ni on_detections, y
        solo quedaría la caducidad de 10 s del firmware."""
        self.cfg = cfg
        self._sync_hold()

    def _send(self, pan: float, tilt: float, force: bool = False,
              reason: str = "manual") -> bool:
        now = time.monotonic()
        if not force and (now - self._last_send) < self.cfg.min_interval_sec:
            return False
        pan = _clamp(pan, -self.cfg.pan_limit, self.cfg.pan_limit)
        tilt = _clamp(tilt, -self.cfg.tilt_limit, self.cfg.tilt_limit)
        if abs(pan - self.pan) >= _LOG_JUMP or abs(tilt - self.tilt) >= _LOG_JUMP:
            print(f"[{self._camera_id}] servo: salto pan {self.pan:+.2f}->{pan:+.2f} "
                  f"tilt {self.tilt:+.2f}->{tilt:+.2f} ({reason})")
        self.pan = pan
        self.tilt = tilt
        self._last_send = now
        # La cámara se va a mover: las posiciones de antes y de después no son
        # comparables para medir velocidad.
        self._still_sample = None
        self._sends += 1
        self._esphome.call_service(self.cfg.service, pan=self.pan, tilt=self.tilt)
        return True

    def move_to(self, pan: float, tilt: float):
        """Control manual (endpoint /servo): salta el rate limit a propósito.

        Quien mueve la torreta a mano quiere que se mueva ya, y además así se
        puede verificar el hardware sin esperar a que haya detecciones.
        """
        self._send(pan, tilt, force=True)

    def _board_setting(self, object_id: str) -> Optional[float]:
        """Valor de un number de ajuste de la placa, o None si no lo publica
        (otra variante de firmware, o todavía sin conectar)."""
        get_state = getattr(self._esphome, "get_state", None)
        value = get_state(object_id) if get_state else None
        return float(value) if isinstance(value, (int, float)) else None

    def _transition_sec(self) -> tuple[float, str]:
        """Tiempo de recorrido completo de la placa y de dónde sale, para
        /status. Solo vive en la placa (number "Servo transition"); si no lo
        publica se supone 0, que es lo que hace un servo sin ese ajuste:
        saltar a cada orden."""
        board = self._board_setting(_BOARD_TRANSITION)
        if board is not None:
            return board, "placa"
        return 0.0, "sin dato"

    def _measure_velocity(self, target: Detection) -> None:
        """Actualiza la velocidad horizontal del objetivo con este frame.

        Solo cuenta si la cámara lleva quieta `_SETTLE_SEC` y la muestra
        anterior es del mismo objetivo en la misma racha quieta (`_send` la
        borra). Así lo que se mide es el objetivo y no el giro de la torreta.

        Se mira cada borde por separado y no el centro: estirar un brazo mueve
        un solo borde y desplaza el centro sin que el cuerpo se haya movido.
        Solo cuenta como desplazamiento si los dos bordes van en el mismo
        sentido, y entonces vale lo que el más lento.
        """
        now = time.monotonic()
        if now - self._last_send < _SETTLE_SEC:
            return
        prev = self._still_sample
        if prev is not None and prev[1] == target.track_id and now - prev[0] > 0.02:
            dt = now - prev[0]
            v1 = (target.x1 - prev[2]) / dt
            v2 = (target.x2 - prev[3]) / dt
            v = min(v1, v2, key=abs) if v1 * v2 > 0 else 0.0
            self._vx = _VELOCITY_ALPHA * v + (1 - _VELOCITY_ALPHA) * self._vx
            self._vx_at = now
        self._still_sample = (now, target.track_id, target.x1, target.x2)

    # -- interfaz DetectionConsumer -----------------------------------------

    def on_detections(self, dets: list[Detection], width: int, height: int) -> None:
        self._sync_hold()
        if not self.cfg.enabled or width <= 0 or height <= 0:
            return

        target = self._pick_target(dets, width, height)
        if target is None:
            return

        # Anticipación: apuntar a donde estará el objetivo, no a donde está.
        self._measure_velocity(target)
        if time.monotonic() - self._vx_at > _LEAD_STALE_SEC:
            self._vx = 0.0  # sin medida reciente no se anticipa
        lead_px = self._vx * self.cfg.lead_sec if abs(self._vx) >= _MIN_LEAD_SPEED else 0.0
        max_lead = width * _MAX_LEAD_FRACTION
        lead_px = max(-max_lead, min(max_lead, lead_px))

        # Error normalizado a [-1, 1]: independiente de la resolución, así que
        # cambiar imgsz o la resolución de la cámara no descalibra la ganancia.
        ex_now = (target.cx - width / 2) / (width / 2)
        ex = ex_now + lead_px / (width / 2)
        ey =(target.cy - height / 2) / (height / 2)

        # Centrar, pero ir a por el borde por el que se sale el objetivo: el de
        # arriba siempre (la cabeza); los laterales solo si la caja es
        # estrecha. El de abajo nunca: el cuerpo de una persona cerca sale
        # siempre por abajo, y perseguirlo bajaba la cámara de golpe.
        narrow = (target.x2 - target.x1) < width * _SIDE_CHASE_MAX_FRACTION
        ex, cut_x = _axis_error(target.x1, target.x2, width, ex, narrow, narrow)
        ey, cut_y = _axis_error(target.y1, target.y2, height, ey, True, False)
        if cut_y is None and ey > 0:
            # Bajar la cámara sube la caja en la imagen: no más allá de dejar
            # la cabeza al borde de la franja superior.
            guard = height * _TOP_GUARD_FRACTION
            ey = min(ey, max(0.0, (target.y1 - guard) / (height / 2)))
        self.last_target = {
            "track_id": target.track_id, "label": target.label,
            "conf": round(target.conf, 2),
            "box": [round(v) for v in (target.x1, target.y1, target.x2, target.y2)],
            "frame": [width, height],
            "vx_px_s": round(self._vx),
            "lead_px": round(lead_px),
        }

        # Zona muerta por eje: un eje ya centrado no se mueve aunque el otro
        # tenga que corregir, o perseguiría el ruido de la caja. En el pan se
        # mira la posición de AHORA, sin anticipación: con alguien cerca el
        # balanceo pasa de _MIN_LEAD_SPEED y la anticipación sacaba de la zona
        # muerta a una persona centrada. Fuera de ella sí se anticipa.
        if abs(ex) < self.cfg.deadzone or \
                (cut_x is None and abs(ex_now) < self.cfg.deadzone):
            ex = 0.0
        if abs(ey) < self.cfg.deadzone:
            ey = 0.0
        if ex == 0.0 and ey == 0.0:
            return  # ya está centrado: no gastar movimiento ni ancho de banda

        step_pan = self.cfg.gain * ex * self.cfg.pan_gear_ratio
        step_tilt = self.cfg.gain * ey * self.cfg.tilt_gear_ratio
        if self.cfg.invert_pan:
            step_pan = -step_pan
        if self.cfg.invert_tilt:
            step_tilt = -step_tilt

        # Se corrige desde la orden actual, no desde donde va la torreta: con
        # la placa interpolando, la posición real va a medio camino, y partir
        # de ahí desharía parte de la última orden. Un eje sin error se queda
        # donde está.
        # El signo por defecto asume una torreta que mira hacia delante: si el
        # objetivo aparece a la derecha de la imagen, la cámara tiene que girar
        # hacia ese lado, lo que en el montaje de referencia es restar pan.
        new_pan = self.pan - step_pan if ex else self.pan
        new_tilt = self.tilt + step_tilt if ey else self.tilt
        # El tope va en giro de CÁMARA: con reducción, el servo tiene que girar
        # más para lo mismo.
        max_pan = _MAX_STEP * self.cfg.pan_gear_ratio
        max_tilt = _MAX_STEP * self.cfg.tilt_gear_ratio
        new_pan = _clamp(new_pan, self.pan - max_pan, self.pan + max_pan)
        new_tilt = _clamp(new_tilt, self.tilt - max_tilt, self.tilt + max_tilt)
        edges = {"lo": ("izq", "arriba"), "hi": ("der", "abajo")}
        cuts = [f"borde {edges[c][i]}" for i, c in enumerate((cut_x, cut_y)) if c]
        reason = (f"#{target.track_id} {target.label} ex={ex:+.2f} ey={ey:+.2f} "
                  + (", ".join(cuts) or "centro")
                  + (f" lead={lead_px:+.0f}px" if lead_px else ""))
        self._send(new_pan, new_tilt, reason=reason)

    def on_idle(self) -> None:
        # Sin frames no hay detecciones, y el objetivo caduca igual que si la
        # cámara siguiera dando imagen sin él: si no, al volver la señal la
        # torreta seguiría enganchada a un ID que ByteTrack ya no reconoce.
        if self.target_id is not None and \
                time.monotonic() - self._last_seen >= self.cfg.lost_target_sec:
            self._release_target()
        self._sync_hold()

    def wants_inference(self) -> bool:
        return self.cfg.enabled

    def status(self) -> dict:
        return {
            "enabled": self.cfg.enabled,
            "pan": round(self.pan, 3),
            "tilt": round(self.tilt, 3),
            "target_id": self.target_id,
            "last_target": self.last_target,
            "transition_sec": self._transition_sec()[0],
            "transition_source": self._transition_sec()[1],
            "smoothing_sec": self._board_setting(_BOARD_SMOOTHING),
            "auto_detach_sec": self._board_setting(_BOARD_AUTO_DETACH),
            # Con el seguimiento activo la placa no aplica el auto-detach
            # (ver _sync_hold).
            "hold": self._hold,
            "sends": self._sends,
            # Lo que de verdad se quiere saber cuando "no se mueve": si la placa
            # llegó a publicar el servicio. Sin esto la llamada falla en
            # silencio y no hay forma de distinguirlo de un error de puntería.
            "service": self.cfg.service,
            "servo_service": self._esphome.has_service(self.cfg.service),
        }

    def shutdown(self) -> None:
        # A reposo antes de que se cierre la conexión: si no, el servo se queda
        # donde estuviera apuntando hasta el próximo arranque.
        try:
            self._send(self.cfg.home_pan, self.cfg.home_tilt, force=True, reason="apagado")
            self._set_hold(False)
        except Exception as e:
            print(f"[{self._camera_id}] servo: fallo al volver a reposo:", repr(e))
