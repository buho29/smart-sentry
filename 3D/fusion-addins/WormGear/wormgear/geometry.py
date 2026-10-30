# Matemática del sinfín y su rueda, sin dependencias de Fusion (se puede probar con Python normal).
# Unidades: mm y radianes.
#
# Convenios (marco del mundo):
#   - rueda: eje Z, centro en el origen, plano medio z = 0;
#   - sinfín: eje paralelo a X, por (0, A, 0); hélice a derechas con el centro del filete en
#     x = lead·(t - π)/2π, siendo t el ángulo alrededor del eje desde +Y hacia +Z.
#     Con esta fase, en el plano medio el filete queda centrado en x = 0 justo debajo, frente a la rueda.
#
# Rueda lisa: en cada plano z = c, el flanco del cortador (sinfín engordado) es
#   x(u) = lead·atan2(-c, -u)/2π + σ·hw(r),   y = A + u,   r = √(u² + c²)
# Girar el sinfín equivale a desplazar esa curva en x (es un helicoide), y la rueda gira alrededor de Z,
# así que el plano no se mueve: la envolvente 2D de cada plano es la del engrane 3D real.
# Movimiento en el marco de la rueda (rodadura sobre el primitivo):  Q = Rot(-θ)·(x - R2·θ, y)
# Ecuación de engrane (la normal pasa por el punto primitivo):  θ = (x + (y - R2)/x') / R2

import math

DEFAULTS = dict(
    module=1.5,          # módulo (mm)
    starts=1,            # entradas del sinfín
    teeth=30,            # dientes de la rueda
    worm_pitch_d=16.0,   # diámetro primitivo del sinfín (mm)
    flank_deg=45.0,      # ángulo de flanco axial (45 = sin voladizos con el sinfín de pie)
    addendum_f=0.5,      # cabeza (× módulo)
    dedendum_f=0.6,      # pie (× módulo)
    clearance=0.15,      # holgura radial (mm)
    backlash=0.1,        # holgura entre flancos, axial (mm)
    worm_length=30.0,
    worm_bore=5.1,
    wheel_width=10.0,
    wheel_bore=8.1,
    rim_over_tip=0.63,   # borde exterior de la rueda por encima de la cabeza de la garganta (mm)
    sections=23,         # perfiles de la solevación
)


def derive(prm):
    """Añade las medidas derivadas a una copia de los parámetros."""
    q = dict(DEFAULTS)
    q.update(prm)
    m = q['module']
    q['p'] = math.pi * m                       # paso axial
    q['lead'] = q['starts'] * q['p']           # avance
    q['R1'] = q['worm_pitch_d'] / 2
    q['R2'] = m * q['teeth'] / 2
    q['A'] = q['R1'] + q['R2']                 # distancia entre ejes
    q['ha'] = q['addendum_f'] * m
    q['hf'] = q['dedendum_f'] * m
    q['alpha'] = math.radians(q['flank_deg'])
    q['r_tip'] = q['R1'] + q['ha']
    q['r_root'] = q['R1'] - q['hf']
    q['hob_tip'] = q['r_tip'] + q['clearance']
    q['r_tip2'] = q['A'] - q['r_root'] - q['clearance']   # cabeza de la rueda en el plano medio
    q['throat'] = q['r_root'] + q['clearance']            # radio de la garganta
    q['rim'] = q['r_tip2'] + q['rim_over_tip']
    q['R_OUT'] = q['rim'] + 1.0
    q['lead_angle_deg'] = math.degrees(math.atan(q['lead'] / (math.pi * q['worm_pitch_d'])))
    # hasta dónde (en z) llega el cortador a la rueda: el fondo del hueco, A - √(cabeza² - z²), sube con |z|
    # y deja de tocar el disco cuando pasa del borde (c_touch). Las secciones llegan medio mm más allá de
    # las caras para que el corte salga limpio, pero no más allá de donde el hueco aún existe (c_lim).
    q['c_touch'] = math.sqrt(max(q['hob_tip'] ** 2 - (q['A'] - q['rim']) ** 2, 0.0))
    q['c_lim'] = math.sqrt(max(q['hob_tip'] ** 2 - (q['A'] - q['R_OUT'] + 0.2) ** 2, 0.0))
    w = min(q['wheel_width'] / 2 + 0.5, q['c_lim'])
    q['section_z'] = [-w + 2 * w * i / (q['sections'] - 1) for i in range(q['sections'])]
    # chaflán de 45° en las caras: empieza donde el diente baja de la mitad de su altura del centro, para
    # quitar las puntas bajas y débiles de los bordes. chamfer_z = None si los dientes llegan bien a las caras.
    q['chamfer_z'] = q['chamfer_r'] = None
    d0 = tooth_depth(q, 0.0)
    W = q['wheel_width'] / 2
    n = 400
    for i in range(n + 1):
        z = W * i / n
        if tooth_depth(q, z) < 0.5 * d0:
            if W - z >= 0.3:   # un chaflán de menos de 0.3 mm no aporta y solo añade caras
                q['chamfer_z'] = z
                q['chamfer_r'] = _outer_radius(q, z)
            break
    return q


def _outer_radius(q, z):
    """Borde del disco sin chaflán: garganta toroidal limitada por el borde."""
    t = q['throat']
    if abs(z) < t:
        return min(q['rim'], q['A'] - math.sqrt(t * t - z * z))
    return q['rim']


def tooth_depth(q, z):
    """Altura del diente de la rueda en z: del fondo que deja el cortador al borde del disco (0 si no llega)."""
    if abs(z) >= q['hob_tip']:
        return 0.0
    return max(0.0, _outer_radius(q, z) - root_radius(q, z))


def hw(q, r, extra=0.0):
    """Semiancho axial del filete a radio r (extra = engorde de cada flanco)."""
    return q['p'] / 4 + extra - (r - q['R1']) * math.tan(q['alpha'])


def rim_radius(q, z):
    """Radio exterior de la rueda a la altura z: garganta toroidal, limitada por el borde y por el chaflán."""
    r = _outer_radius(q, z)
    if q['chamfer_z'] is not None and abs(z) > q['chamfer_z']:
        r = min(r, q['chamfer_r'] - (abs(z) - q['chamfer_z']))
    return r


def _rot(a, x, y):
    ca, sa = math.cos(a), math.sin(a)
    return (ca * x - sa * y, sa * x + ca * y)


def envelope_point(q, c, u, sigma):
    """Punto de la envolvente generado por el punto u del flanco sigma (-1 izquierdo, +1 derecho) del cortador."""
    ta = math.tan(q['alpha'])
    r = math.hypot(u, c)
    x = q['lead'] * math.atan2(-c, -u) / (2 * math.pi) + sigma * hw(q, r, q['backlash'] / 2)
    y = q['A'] + u
    dx = -q['lead'] * c / (2 * math.pi * r * r) - sigma * ta * u / r
    theta = (x + (y - q['R2']) / dx) / q['R2']
    return _rot(-theta, x - q['R2'] * theta, y)


def root_radius(q, c):
    """Fondo del hueco en el plano z=c: lo marca la cabeza del cortador."""
    return q['A'] - math.sqrt(q['hob_tip'] ** 2 - c * c)


def flank(q, c, sigma, n=40):
    """Puntos de un flanco del hueco, del círculo de fondo a R_OUT (radio creciente), remuestreados a n+1.
    El flanco del cortador se prolonga por debajo de la cabeza y por encima del fondo (virtual): lo que sobra
    se recorta con el círculo de fondo, y por fuera del borde de la rueda no hay material que cortar."""
    rr = root_radius(q, c)
    u0 = -math.sqrt(q['hob_tip'] ** 2 - c * c) - 1.0
    u1 = -0.2
    raw = []
    steps = 400
    for i in range(steps + 1):
        u = u0 + (u1 - u0) * i / steps
        try:
            e = envelope_point(q, c, u, sigma)
        except ZeroDivisionError:   # flanco paralelo a la rodadura: ese punto no engendra nada
            continue
        raw.append((math.hypot(*e), e))
    out = []
    for (ra, ea), (rb, eb) in zip(raw, raw[1:]):
        if rb <= ra:
            continue
        for rt in (rr, q['R_OUT']):
            if ra < rt <= rb:
                t = (rt - ra) / (rb - ra)
                out.append((rt, (ea[0] + t * (eb[0] - ea[0]), ea[1] + t * (eb[1] - ea[1]))))
        if rr < rb < q['R_OUT']:
            out.append((rb, eb))
    out.sort(key=lambda v: v[0])
    pts = [v[1] for v in out]
    if len(pts) < 2:
        raise ValueError('no se pudo calcular el flanco en z=%.2f' % c)
    L = [0.0]
    for a, b in zip(pts, pts[1:]):
        L.append(L[-1] + math.dist(a, b))
    res = []
    j = 0
    for i in range(n + 1):
        s = L[-1] * i / n
        while j < len(L) - 2 and L[j + 1] < s:
            j += 1
        t = 0 if L[j + 1] == L[j] else (s - L[j]) / (L[j + 1] - L[j])
        res.append((pts[j][0] + t * (pts[j + 1][0] - pts[j][0]), pts[j][1] + t * (pts[j + 1][1] - pts[j][1])))
    return res


def gap_profile(q, c, n=40):
    """Hueco de un diente en z=c: (flanco izquierdo, flanco derecho, radio de fondo, radio exterior).
    Los flancos van del fondo hacia fuera. El hueco está centrado en +Y."""
    return flank(q, c, -1, n), flank(q, c, +1, n), root_radius(q, c), q['R_OUT']


def angle_from_y(pt):
    return math.atan2(pt[0], pt[1])


def _angle_at_radius(pts, r):
    for a, b in zip(pts, pts[1:]):
        ra, rb = math.hypot(*a), math.hypot(*b)
        if ra <= r <= rb and rb > ra:
            t = (r - ra) / (rb - ra)
            return angle_from_y((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
    return None


def tip_land(q):
    """Grosor mínimo (mm) de la cabeza del diente de la rueda en su borde exterior, mirando todas las secciones."""
    pitch = 2 * math.pi / q['teeth']
    worst = None
    for c in q['section_z']:
        if abs(c) > q['wheel_width'] / 2:
            continue
        left, right, _, _ = gap_profile(q, c, 60)
        rr = rim_radius(q, c)
        al, ar = _angle_at_radius(left, rr), _angle_at_radius(right, rr)
        if al is None or ar is None:
            continue
        land = (pitch - (ar - al)) * rr
        worst = land if worst is None else min(worst, land)
    return worst


def check_params(prm):
    """Devuelve (q, errores, avisos, resumen) para el diálogo."""
    q = derive(prm)
    errors, warns = [], []
    if q['starts'] < 1 or q['teeth'] < 3:
        errors.append('Entradas ≥ 1 y dientes ≥ 3.')
    if q['r_root'] <= q['worm_bore'] / 2 + 0.8:
        errors.append('El taladro del sinfín no cabe: pared de %.2f mm bajo el fondo del filete.' % (q['r_root'] - q['worm_bore'] / 2))
    if q['wheel_width'] / 2 + 0.5 >= q['hob_tip']:
        errors.append('La rueda es demasiado ancha para este sinfín (ancho/2 tiene que ser < %.1f mm).' % (q['hob_tip'] - 0.5))
    crest = 2 * hw(q, q['r_tip'])
    gap_root = q['p'] - 2 * hw(q, q['r_root'], q['backlash'] / 2)
    if crest <= 0:
        errors.append('El filete del sinfín acaba en punta (cresta %.2f mm): baja la cabeza o el flanco.' % crest)
    elif crest < 0.4:
        warns.append('Cresta del sinfín de %.2f mm (< 0.4).' % crest)
    if gap_root <= 0:
        errors.append('El hueco del sinfín se cierra en el fondo: baja el pie o el flanco.')
    elif gap_root < 0.3:
        warns.append('Fondo del hueco del sinfín de %.2f mm.' % gap_root)
    if q['flank_deg'] < 45:
        warns.append('Con el sinfín de pie, la cara de abajo del filete queda a %.0f° de voladizo.' % (90 - q['flank_deg']))
    if q['wheel_width'] / 2 > q['c_touch']:
        warns.append('Los dientes no llegan a las caras: el sinfín solo toca la rueda en %.1f mm de ancho '
                     '(estrecha la rueda o agranda el sinfín).' % (2 * q['c_touch']))
    elif q['wheel_width'] / 2 > q['throat'] * math.sin(math.radians(45)):
        warns.append('La garganta de la rueda pasa de 45° en las caras: estrecha la rueda o agranda el sinfín.')
    if q['ha'] + q['clearance'] > q['R2'] * math.sin(q['alpha']) ** 2:
        warns.append('Puede haber socavado en el pie de los dientes de la rueda (pocos dientes para este flanco).')
    hub = q['A'] - q['hob_tip'] - q['wheel_bore'] / 2
    if hub < 2:
        warns.append('Queda poco cubo entre el taladro y el fondo de los dientes (%.1f mm).' % hub)
    land = None
    if not errors:
        try:
            land = tip_land(q)
        except ValueError as e:
            errors.append(str(e))
    if land is not None and land < 0.3:
        warns.append('Los dientes de la rueda quedan en punta en el borde (%.2f mm): baja "borde sobre la cabeza".' % land)
    locking = 'autobloqueante' if q['lead_angle_deg'] < 5.5 else ('casi autobloqueante' if q['lead_angle_deg'] < 8 else 'reversible')
    summary = ('Relación %g:1 (%d dientes / %d entradas) · entre ejes %.2f mm · avance %.3f mm · hélice %.2f° (%s)\n'
               'Sinfín Ø%.2f / fondo Ø%.2f · rueda Ø%.2f primitivo, borde Ø%.2f · cresta del sinfín %.2f mm%s'
               % (round(q['teeth'] / q['starts'], 3), q['teeth'], q['starts'], q['A'], q['lead'], q['lead_angle_deg'], locking,
                  2 * q['r_tip'], 2 * q['r_root'], 2 * q['R2'], 2 * q['rim'], crest,
                  '' if land is None else ' · cabeza del diente de la rueda %.2f mm' % land))
    if q['chamfer_z'] is not None:
        summary += ('\nChaflán de 45° en las caras desde z = ±%.2f mm (ahí el diente baja de la mitad de su altura)'
                    % q['chamfer_z'])
    return q, errors, warns, summary


if __name__ == '__main__':
    for prm in ({}, dict(module=2, teeth=20, starts=2, flank_deg=30, addendum_f=0.8, dedendum_f=1.0,
                         worm_pitch_d=20, wheel_width=12, worm_length=36, rim_over_tip=0.8)):
        q, errs, warns, summ = check_params(prm)
        print(summ)
        print('errores:', errs)
        print('avisos:', warns)
        for c in (q['section_z'][0], 0.0, q['section_z'][-1]):
            l, r, rr, ro = gap_profile(q, c)
            print('  c=%+.2f fondo %.3f  izq %.2f°→%.2f°  der %.2f°→%.2f°' % (
                c, rr, math.degrees(angle_from_y(l[0])), math.degrees(angle_from_y(l[-1])),
                math.degrees(angle_from_y(r[0])), math.degrees(angle_from_y(r[-1]))))
