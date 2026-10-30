# Comprobación del engrane con cuerpos temporales (no toca el diseño):
# - interferencia sinfín/rueda en varias fases de un paso de diente, en todo el ancho de la rueda;
# - juego: cuánto gira el sinfín, con la rueda quieta, hasta tocar por cada lado.
# Se usan los cuerpos tal como se crearon (sinfín en su sitio, rueda en el origen).
import math
import adsk.core
import adsk.fusion

from .build import MM, P, V, worm_axis_rotation, wheel_rotation

TOL = 1e-4   # mm³: por debajo se considera que no tocan


def _vol(b):
    """Volumen en mm³; None si Fusion no lo sabe calcular (pasa con astillas casi tangentes)."""
    try:
        return b.volume * 1000.0
    except RuntimeError:
        pass
    try:
        return sum(l.volume for l in b.lumps) * 1000.0
    except RuntimeError:
        return None


def check(q, worm_body, wheel_body, phases=8):
    tbm = adsk.fusion.TemporaryBRepManager.get()
    I = adsk.fusion.BooleanTypes.IntersectionBooleanType
    worm = tbm.copy(worm_body)
    # solo la zona de la rueda que puede tocar el sinfín, para que sea rápido
    y0 = q['A'] - q['hob_tip'] - 1.5
    y1 = q['rim'] + 1.0
    zone = tbm.createBox(adsk.core.OrientedBoundingBox3D.create(
        P(0.013, (y0 + y1) / 2, 0.011), V(1, 0, 0), V(0, 1, 0),
        (q['worm_length'] + 4.03) * MM, (y1 - y0) * MM, (q['wheel_width'] + 2.07) * MM))
    tbm.booleanOperation(zone, tbm.copy(wheel_body), I)

    def overlap(theta, dphi=0.0):
        # si Fusion no sabe cortar o medir (contacto casi tangente), se repite con un giro mínimo
        for eps in (0.0, 1e-6, -1e-6, 1e-5, -1e-5):
            w = tbm.copy(worm)
            tbm.transform(w, worm_axis_rotation(q, (theta + eps) * q['teeth'] / q['starts'] + dphi))
            tbm.transform(w, wheel_rotation(-(theta + eps)))
            z = tbm.copy(zone)
            try:
                tbm.booleanOperation(z, w, I)
            except RuntimeError:
                continue
            v = _vol(z)
            if v is not None:
                return v
        return float('inf')   # no se puede medir: se cuenta como contacto

    pitch = 2 * math.pi / q['teeth']
    vols = [overlap(pitch * k / phases) for k in range(phases)]

    def first_contact(sign):
        hi = math.radians(2 * 360 * q['backlash'] / q['lead'] + 2)
        if overlap(0, sign * hi) <= TOL:
            return None
        lo = 0.0
        for _ in range(12):
            mid = (lo + hi) / 2
            if overlap(0, sign * mid) > TOL:
                hi = mid
            else:
                lo = mid
        return math.degrees((lo + hi) / 2)

    pos, neg = first_contact(+1), first_contact(-1)
    play_mm = None if pos is None or neg is None else (pos + neg) / 360 * q['lead']
    return dict(overlaps=vols, max_overlap=max(vols), play_pos_deg=pos, play_neg_deg=neg, play_mm=play_mm)


def report(res, q):
    ok = res['max_overlap'] <= TOL
    lines = ['Interferencia en %d fases de un diente: %s (máx. %.4f mm³)'
             % (len(res['overlaps']), 'ninguna' if ok else 'SÍ', res['max_overlap'])]
    if res['play_mm'] is None:
        lines.append('Juego: no he encontrado contacto girando el sinfín (¿demasiada holgura?).')
    else:
        lines.append('Juego: el sinfín gira +%.2f° / -%.2f° hasta tocar = %.3f mm axiales (pedido %.3f mm)'
                     % (res['play_pos_deg'], res['play_neg_deg'], res['play_mm'], q['backlash']))
    return ok, '\n'.join(lines)
