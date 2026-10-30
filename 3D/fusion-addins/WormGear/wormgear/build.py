# Crea el sinfín y la rueda lisa en el diseño activo de Fusion.
# - Sinfín: perfil axial trapezoidal barrido por una hélice (con una segunda hélice de raíl para que no se tuerza),
#   unido al núcleo, recortado con chaflanes de 45° y taladrado (cuerpo de un base feature).
# - Rueda: disco en bruto con garganta, N perfiles calculados del hueco (geometry.gap_profile), solevación,
#   patrón circular y corte. Todo queda en un grupo de la línea de tiempo.
# En un diseño de ensamblaje cada pieza va en su componente (con junta de giro); en uno de pieza, como cuerpos.
import math
import adsk.core
import adsk.fusion

from . import geometry as G

MM = 0.1   # Fusion trabaja en cm


def P(x, y, z):
    return adsk.core.Point3D.create(x * MM, y * MM, z * MM)


def V(x, y, z):
    return adsk.core.Vector3D.create(x, y, z)


def _tbm():
    return adsk.fusion.TemporaryBRepManager.get()


def _cyl(p1, r1, p2, r2):
    return _tbm().createCylinderOrCone(P(*p1), r1 * MM, P(*p2), r2 * MM)


def worm_axis_rotation(q, phi):
    """Giro phi (rad) alrededor del eje del sinfín (paralelo a X por (0, A, 0))."""
    m = adsk.core.Matrix3D.create()
    m.setToRotation(phi, V(1, 0, 0), P(0, q['A'], 0))
    return m


def wheel_rotation(theta):
    m = adsk.core.Matrix3D.create()
    m.setToRotation(theta, V(0, 0, 1), P(0, 0, 0))
    return m


# ---------------------------------------------------------------- sinfín

def _thread_body(comp, q, extra, tip, length):
    """Barre un filete y devuelve una copia temporal (borra los bocetos y el barrido)."""
    lead = q['lead']
    # hélice con el centro del filete en x = lead·(t-π)/2π; empieza en t = 2π·k0 (perfil en el plano z = 0)
    k0 = math.floor(-length / 2 / lead + 0.5)
    t0 = 2 * math.pi * k0
    t1 = 2 * math.pi * (length / 2 / lead + 0.5) + 0.2
    n = int((t1 - t0) / (2 * math.pi) * 32) + 2
    sk = comp.sketches.add(comp.xYConstructionPlane)
    sk.isComputeDeferred = True

    def helix(r):
        pts = adsk.core.ObjectCollection.create()
        for i in range(n + 1):
            t = t0 + (t1 - t0) * i / n
            pts.add(P(lead * (t - math.pi) / (2 * math.pi), q['A'] + r * math.cos(t), r * math.sin(t)))
        return sk.sketchCurves.sketchFittedSplines.add(pts)

    path_curve = helix(q['R1'])
    rail_curve = helix(tip)          # guía por el centro de la cresta
    sk.isComputeDeferred = False
    # perfil axial: trapecio de flancos rectos, recto por debajo del fondo para meterse en el núcleo sin cerrarse
    xc = lead * (t0 - math.pi) / (2 * math.pi)
    r_in = q['r_root'] - 0.4
    w_root = G.hw(q, q['r_root'], extra)
    w_tip = G.hw(q, tip, extra)
    corners = [(xc - w_root, r_in), (xc + w_root, r_in), (xc + w_root, q['r_root']), (xc + w_tip, tip),
               (xc - w_tip, tip), (xc - w_root, q['r_root'])]
    sk2 = comp.sketches.add(comp.xYConstructionPlane)
    lines = sk2.sketchCurves.sketchLines
    for i in range(len(corners)):
        x0, r0 = corners[i]
        x1, r1 = corners[(i + 1) % len(corners)]
        lines.addByTwoPoints(P(x0, q['A'] + r0, 0), P(x1, q['A'] + r1, 0))
    si = comp.features.sweepFeatures.createInput(sk2.profiles.item(0), comp.features.createPath(path_curve, False),
                                                 adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    si.guideRail = comp.features.createPath(rail_curve, False)
    si.profileScaling = adsk.fusion.SweepProfileScalingOptions.SweepProfileNoScalingOption
    sw = comp.features.sweepFeatures.add(si)
    tb = _tbm().copy(sw.bodies.item(0))
    sw.deleteMe()
    sk2.deleteMe()
    sk.deleteMe()
    return tb


def worm_solid(comp, q, extra=0.0, tip=None, length=None, finished=True):
    """Sinfín como cuerpo temporal. finished=False: sin chaflanes ni taladro (para usarlo de cortador)."""
    tbm = _tbm()
    tip = q['r_tip'] if tip is None else tip
    length = q['worm_length'] if length is None else length
    L = length / 2
    thread = _thread_body(comp, q, extra, tip, length + 2 * q['lead'])
    for s in range(1, q['starts']):
        other = tbm.copy(thread)
        tbm.transform(other, worm_axis_rotation(q, 2 * math.pi * s / q['starts']))
        tbm.booleanOperation(thread, other, adsk.fusion.BooleanTypes.UnionBooleanType)
    A = q['A']
    core = _cyl((-L, A, 0), q['r_root'], (L, A, 0), q['r_root'])
    tbm.booleanOperation(thread, core, adsk.fusion.BooleanTypes.UnionBooleanType)
    if finished:
        ch = tip - q['r_root']   # chaflán de 45° en las puntas
        env = _cyl((-L + ch, A, 0), tip, (L - ch, A, 0), tip)
        tbm.booleanOperation(env, _cyl((-L, A, 0), q['r_root'], (-L + ch, A, 0), tip), adsk.fusion.BooleanTypes.UnionBooleanType)
        tbm.booleanOperation(env, _cyl((L - ch, A, 0), tip, (L, A, 0), q['r_root']), adsk.fusion.BooleanTypes.UnionBooleanType)
    else:
        env = _cyl((-L, A, 0), tip + 1, (L, A, 0), tip + 1)
    tbm.booleanOperation(thread, env, adsk.fusion.BooleanTypes.IntersectionBooleanType)
    if finished and q['worm_bore'] > 0:
        tbm.booleanOperation(thread, _cyl((-L - 1, A, 0), q['worm_bore'] / 2, (L + 1, A, 0), q['worm_bore'] / 2),
                             adsk.fusion.BooleanTypes.DifferenceBooleanType)
    return thread


def build_worm(comp, q, log):
    tb = worm_solid(comp, q)
    bf = comp.features.baseFeatures.add()
    bf.name = 'Sinfín'
    bf.startEdit()
    comp.bRepBodies.add(tb, bf)
    bf.finishEdit()
    body = bf.bodies.item(0)
    body.name = 'Sinfín'
    log.append('Sinfín: %d caras, %.0f mm³' % (body.faces.count, body.volume * 1000))
    return body


# ---------------------------------------------------------------- rueda

def build_wheel(comp, q, log):
    tbm = _tbm()
    W = q['wheel_width'] / 2
    blank = _cyl((0, 0, -W), q['rim'], (0, 0, W), q['rim'])
    tbm.booleanOperation(blank, tbm.createTorus(P(0, 0, 0), V(0, 0, 1), q['A'] * MM, q['throat'] * MM),
                         adsk.fusion.BooleanTypes.DifferenceBooleanType)
    if q['chamfer_z'] is not None:
        # chaflán de 45° en las caras: entre ±chamfer_z se deja todo; fuera, un cono que baja 45° hacia la cara
        zc, rc = q['chamfer_z'], q['chamfer_r']
        big = q['rim'] + 5
        keep = _cyl((0, 0, -zc), big, (0, 0, zc), big)
        for sgn in (1, -1):
            cone = _cyl((0, 0, sgn * zc), rc, (0, 0, sgn * (W + 1)), rc - (W + 1 - zc))
            tbm.booleanOperation(keep, cone, adsk.fusion.BooleanTypes.UnionBooleanType)
        tbm.booleanOperation(blank, keep, adsk.fusion.BooleanTypes.IntersectionBooleanType)
    if q['wheel_bore'] > 0:
        tbm.booleanOperation(blank, _cyl((0, 0, -W - 1), q['wheel_bore'] / 2, (0, 0, W + 1), q['wheel_bore'] / 2),
                             adsk.fusion.BooleanTypes.DifferenceBooleanType)
    bf = comp.features.baseFeatures.add()
    bf.name = 'Rueda en bruto'
    bf.startEdit()
    comp.bRepBodies.add(blank, bf)
    bf.finishEdit()
    blank_body = bf.bodies.item(0)

    # un boceto por sección con el perfil cerrado del hueco de un diente (centrado en +Y)
    sketches = []
    for c in q['section_z']:
        pin = comp.constructionPlanes.createInput()
        pin.setByOffset(comp.xYConstructionPlane, adsk.core.ValueInput.createByReal(c * MM))
        pl = comp.constructionPlanes.add(pin)
        pl.isLightBulbOn = False
        sk = comp.sketches.add(pl)
        sk.name = 'Hueco z=%+.2f' % c
        sk.isComputeDeferred = True
        left, right, rr, ro = G.gap_profile(q, c)

        def sp(pt):
            return sk.modelToSketchSpace(P(pt[0], pt[1], c))

        def spline(pts):
            oc = adsk.core.ObjectCollection.create()
            for pt in pts:
                oc.add(sp(pt))
            return sk.sketchCurves.sketchFittedSplines.add(oc)

        sl = spline(left)                    # fondo -> fuera
        sr = spline(list(reversed(right)))   # fuera -> fondo
        a_out = (G.angle_from_y(left[-1]) + G.angle_from_y(right[-1])) / 2
        a_root = (G.angle_from_y(left[0]) + G.angle_from_y(right[0])) / 2
        arcs = sk.sketchCurves.sketchArcs
        arcs.addByThreePoints(sl.endSketchPoint, sp((ro * math.sin(a_out), ro * math.cos(a_out))), sr.startSketchPoint)
        arcs.addByThreePoints(sr.endSketchPoint, sp((rr * math.sin(a_root), rr * math.cos(a_root))), sl.startSketchPoint)
        sk.isComputeDeferred = False
        sk.isVisible = False
        if sk.profiles.count != 1:
            raise RuntimeError('El perfil del hueco en z=%.2f no ha salido cerrado (%d perfiles).' % (c, sk.profiles.count))
        sketches.append(sk)

    li = comp.features.loftFeatures.createInput(adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    for sk in sketches:
        li.loftSections.add(sk.profiles.item(0))
    li.isSolid = True
    loft = comp.features.loftFeatures.add(li)
    loft.name = 'Hueco del diente'
    cutter = loft.bodies.item(0)

    oc = adsk.core.ObjectCollection.create()
    oc.add(cutter)
    pin = comp.features.circularPatternFeatures.createInput(oc, comp.zConstructionAxis)
    pin.quantity = adsk.core.ValueInput.createByReal(q['teeth'])
    pin.totalAngle = adsk.core.ValueInput.createByString('360 deg')
    pat = comp.features.circularPatternFeatures.add(pin)
    pat.name = 'Huecos'
    tools = adsk.core.ObjectCollection.create()
    tools.add(cutter)
    for b in pat.bodies:
        if b != cutter:
            tools.add(b)
    ci = comp.features.combineFeatures.createInput(blank_body, tools)
    ci.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
    ci.isKeepToolBodies = False
    cut = comp.features.combineFeatures.add(ci)
    cut.name = 'Tallar rueda'
    body = cut.bodies.item(0)
    body.name = 'Rueda'
    log.append('Rueda: %d caras (%.1f por diente), %.0f mm³' % (body.faces.count, body.faces.count / q['teeth'], body.volume * 1000))
    return body


# ---------------------------------------------------------------- juntas (ensamblaje e híbrido)

def _axis_line(comp, which, q):
    """Línea de boceto sobre el eje de la rueda ('wheel', +Z) o del sinfín ('worm', +X por y = A)."""
    sk = comp.sketches.add(comp.xYConstructionPlane)
    sk.name = 'Eje %s' % ('rueda' if which == 'wheel' else 'sinfín')
    if which == 'wheel':
        h = q['wheel_width'] / 2
        ln = sk.sketchCurves.sketchLines.addByTwoPoints(P(0, 0, -h), P(0, 0, h))
    else:
        h = q['worm_length'] / 2
        ln = sk.sketchCurves.sketchLines.addByTwoPoints(P(-h, q['A'], 0), P(h, q['A'], 0))
    ln.isConstruction = True
    sk.isVisible = False
    return ln


def _joints(root, q, tag, parts, log):
    """Junta de giro de cada pieza respecto a un componente fijo "Ejes" (con las líneas de los dos ejes),
    y enlace de movimiento con la relación si están las dos. parts = {'worm': (occ, comp), 'wheel': (...)}."""
    occ_axes = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    occ_axes.component.name = 'Ejes %s' % tag
    occ_axes.isGrounded = True
    joints = {}
    for which, (occ, comp) in parts.items():
        name = 'rueda' if which == 'wheel' else 'sinfín'
        # en los diseños híbridos/de ensamblaje un componente nuevo puede quedar "fijado al principal":
        # entonces la junta no lo mueve
        for attr, value in (('isGroundToParent', False), ('isGrounded', False)):
            try:
                setattr(occ, attr, value)
            except Exception:
                pass
        try:
            fixed = _axis_line(occ_axes.component, which, q).createForAssemblyContext(occ_axes)
            moving = _axis_line(comp, which, q).createForAssemblyContext(occ)
            ji = root.joints.createInput(
                adsk.fusion.JointGeometry.createByCurve(moving, adsk.fusion.JointKeyPointTypes.MiddleKeyPoint),
                adsk.fusion.JointGeometry.createByCurve(fixed, adsk.fusion.JointKeyPointTypes.MiddleKeyPoint))
            # eje de giro explícito = la línea del eje (con X/Y/Z de la junta Fusion gira alrededor de otro eje)
            ji.setAsRevoluteJointMotion(adsk.fusion.JointDirections.CustomJointDirection, fixed)
            j = root.joints.add(ji)
            j.name = 'Giro %s' % name
            joints[which] = j
        except Exception as e:
            log.append('%s: no se pudo crear la junta de giro (%s).' % (name, str(e).splitlines()[0]))
    if len(joints) == 2:
        try:
            # una vuelta del sinfín (giro + alrededor de +X) = entradas/dientes de vuelta de la rueda (+ alrededor de +Z)
            mi = root.motionLinks.createInput(joints['worm'], joints['wheel'])
            mi.valueOne = adsk.core.ValueInput.createByString('360 deg')
            mi.valueTwo = adsk.core.ValueInput.createByString('%.6f deg' % (360.0 * q['starts'] / q['teeth']))
            mi.isReversed = False
            ml = root.motionLinks.add(mi)
            ml.name = 'Sinfín %s' % tag
            log.append('Juntas de giro y enlace de movimiento creados: arrastra el sinfín y gira la rueda.')
        except Exception as e:
            log.append('No se pudo crear el enlace de movimiento (%s): créalo a mano, 360° del sinfín = %.4f° de la rueda.'
                       % (str(e).splitlines()[0], 360.0 * q['starts'] / q['teeth']))
    return joints


# ---------------------------------------------------------------- todo

def build(design, prm, what='both'):
    """Genera lo pedido (both / worm / wheel). Devuelve (q, cuerpo sinfín, cuerpo rueda, log)."""
    q, errors, warns, summary = G.check_params(prm)
    if errors:
        raise ValueError('\n'.join(errors))
    log = []
    root = design.rootComponent
    tl = design.timeline
    start = tl.count
    tag = 'm%g %d:%d' % (q['module'], q['teeth'], q['starts'])

    # los diseños "de pieza" no admiten componentes: ahí van como cuerpos del raíz.
    # Se mira antes de intentarlo, porque un addNewComponent fallido deja el error pendiente en la API.
    occ_worm = occ_wheel = None
    comp_worm = comp_wheel = root
    intent = getattr(design, 'designIntent', None)
    if intent == getattr(adsk.fusion.DesignIntentTypes, 'PartDesignIntentType', -1):
        log.append('Diseño de pieza: sinfín y rueda van como cuerpos (sin componentes ni juntas).')
    else:
        if what in ('both', 'worm'):
            occ_worm = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
            comp_worm = occ_worm.component
            comp_worm.name = 'Sinfín %s' % tag
        if what in ('both', 'wheel'):
            occ_wheel = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
            comp_wheel = occ_wheel.component
            comp_wheel.name = 'Rueda %s' % tag

    worm = build_worm(comp_worm, q, log) if what in ('both', 'worm') else None
    wheel = build_wheel(comp_wheel, q, log) if what in ('both', 'wheel') else None

    parts = {}
    if occ_worm is not None:
        parts['worm'] = (occ_worm, comp_worm)
    if occ_wheel is not None:
        parts['wheel'] = (occ_wheel, comp_wheel)
    joints = _joints(root, q, tag, parts, log) if parts else {}
    try:
        if tl.count - 1 > start:
            grp = tl.timelineGroups.add(start, tl.count - 1)
            grp.name = 'Sinfín y rueda %s' % tag
    except Exception as e:
        log.append('No se pudo agrupar la línea de tiempo (%s).' % str(e).splitlines()[0])
    build.last_joints = joints
    return q, worm, wheel, log
