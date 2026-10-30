# Dibuja los diagramas de "Cómo se calcula la rueda" del README con la misma matemática del add-in.
# Uso:  python img/diagramas.py   (desde la carpeta WormGear; necesita matplotlib y Pillow)
import math
import os
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, Polygon

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from wormgear import geometry as G   # noqa: E402

q = G.derive({})          # valores por defecto: m1.5, 30 dientes, 1 entrada, flanco 45°
WORM = '#E08C19'
WHEEL = '#3A63AA'
ENV = '#C62828'
GREY = '#888888'
C_OFF = 3.5               # plano fuera del centro que se usa de ejemplo
plt.rcParams.update({'font.size': 11, 'axes.titlesize': 12, 'savefig.dpi': 110})


def cutter_outline(c, shift=0.0, ks=range(-2, 3)):
    """Contorno del cortador (sinfín engordado) en el plano z=c, de izquierda a derecha, en el marco fijo."""
    u_tip = -math.sqrt(q['hob_tip'] ** 2 - c * c)
    u_root = -math.sqrt(q['r_root'] ** 2 - c * c)
    us = [u_root + (u_tip - u_root) * i / 30 for i in range(31)]

    def x(u, sigma, k):
        r = math.hypot(u, c)
        return q['lead'] * math.atan2(-c, -u) / (2 * math.pi) + sigma * G.hw(q, r, q['backlash'] / 2) + k * q['p'] + shift
    pts = []
    for k in ks:
        pts += [(x(u, -1, k), q['A'] + u) for u in us]               # flanco izquierdo, del fondo a la cabeza
        pts += [(x(u, +1, k), q['A'] + u) for u in reversed(us)]     # flanco derecho, de la cabeza al fondo
    return pts


def to_wheel(pts, theta):
    """Del marco fijo al de la rueda cuando la rueda ha girado theta (y el cortador se ha desplazado -R2·theta)."""
    return [G._rot(-theta, x - q['R2'] * theta, y) for x, y in pts]


def gap_polygon(c):
    left, right, rr, ro = G.gap_profile(q, c, 60)
    a_l0, a_r0 = G.angle_from_y(left[0]), G.angle_from_y(right[0])
    a_l1, a_r1 = G.angle_from_y(left[-1]), G.angle_from_y(right[-1])
    bottom = [(rr * math.sin(a_r0 + (a_l0 - a_r0) * i / 10), rr * math.cos(a_r0 + (a_l0 - a_r0) * i / 10)) for i in range(11)]
    top = [(ro * math.sin(a_l1 + (a_r1 - a_l1) * i / 10), ro * math.cos(a_l1 + (a_r1 - a_l1) * i / 10)) for i in range(11)]
    return left + top + list(reversed(right)) + bottom


def save(fig, name):
    fig.savefig(os.path.join(HERE, name), bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print('ok', name)


# ------------------------------------------------ 1. planos paralelos al plano medio
def fig_planos():
    fig, ax = plt.subplots(figsize=(8, 5))
    W = q['wheel_width'] / 2
    zs = [-W + 2 * W * i / 200 for i in range(201)]
    y0 = q['R2'] - 9
    teeth = [(G.rim_radius(q, z), z) for z in zs]
    roots = [(G.root_radius(q, z), z) for z in zs]
    ax.add_patch(Polygon([(y0, -W)] + roots + [(y0, W)], closed=True, fc=WHEEL, ec='none', alpha=0.8))
    ax.add_patch(Polygon(roots + list(reversed(teeth)), closed=True, fc=WHEEL, ec='none', alpha=0.35))
    ax.plot(*zip(*teeth), color=WHEEL, lw=1.5)
    ax.plot(*zip(*roots), color=WHEEL, lw=1, ls='--')
    ax.add_patch(Circle((q['A'], 0), q['r_tip'], fc=WORM, ec=WORM, lw=1.5, alpha=0.18))
    ax.add_patch(Circle((q['A'], 0), q['r_tip'], fill=False, ec=WORM, lw=1.5))
    ax.add_patch(Circle((q['A'], 0), q['r_root'], fc=WORM, ec=WORM, alpha=0.45))
    for c in q['section_z']:
        ax.plot([y0, q['A'] + q['r_tip'] + 1], [c, c], color=GREY, lw=0.5, alpha=0.7)
    box = dict(fc='white', ec='none', alpha=0.85, pad=1)
    for c, lab in ((0.0, 'z = 0 (plano medio)'), (C_OFF, 'z = %.1f (fuera del centro)' % C_OFF)):
        ax.plot([y0, q['A'] + q['r_tip'] + 1], [c, c], color=ENV, lw=1.8)
        ax.text(y0 + 0.3, c + 0.3, lab, color=ENV, fontsize=10, bbox=box)
    ax.annotate('fondo de los huecos\n(lo marca la cabeza del sinfín)', xy=(G.root_radius(q, -2.5), -2.5),
                xytext=(y0 + 0.3, -W - 2.6), fontsize=9, arrowprops=dict(arrowstyle='->'))
    ax.annotate('borde de la rueda', xy=(G.rim_radius(q, -4.5), -4.5), xytext=(q['R2'] - 1, -W - 3.8),
                fontsize=9, arrowprops=dict(arrowstyle='->'))
    if q['chamfer_z'] is not None:
        ax.annotate('chaflán de 45°', xy=(G.rim_radius(q, W - 0.3), W - 0.3), xytext=(q['R2'] - 7, W + 2.2),
                    arrowprops=dict(arrowstyle='->', color='k'), fontsize=10)
    ax.text(y0 + 0.3, W + 1.5, 'rueda (cortada por un plano que pasa por su eje)', color=WHEEL, fontsize=10, weight='bold')
    ax.text(q['A'] - 4.2, q['r_tip'] + 0.5, 'sinfín (su eje sale\nhacia ti)', color=WORM, fontsize=10, weight='bold')
    ax.set_xlim(y0, q['A'] + q['r_tip'] + 1)
    ax.set_ylim(-q['r_tip'] - 1.5, q['r_tip'] + 3)
    ax.set_aspect('equal')
    ax.set_xlabel('distancia al eje de la rueda (mm)')
    ax.set_ylabel('z (mm)')
    ax.set_title('1. Se corta todo con planos paralelos al plano medio de la rueda\n'
                 '(líneas grises: los %d planos; en rojo, los dos de ejemplo)' % q['sections'])
    save(fig, 'calc_1_planos.png')


# ------------------------------------------------ 2. el cortador en un plano: una cremallera que se desplaza
def fig_seccion():
    fig, axs = plt.subplots(1, 2, figsize=(10, 3.3), sharey=True)
    for ax, c in zip(axs, (0.0, C_OFF)):
        pts = cutter_outline(c)
        y_core = q['A'] - math.sqrt(q['r_root'] ** 2 - c * c)
        poly = [(pts[0][0], q['A'] + 2)] + pts + [(pts[-1][0], q['A'] + 2)]
        ax.add_patch(Polygon(poly, closed=True, fc=WORM, ec=WORM, alpha=0.8))
        ghost = cutter_outline(c, shift=q['p'] * 0.35)
        ax.plot(*zip(*ghost), color=WORM, ls='--', lw=1)
        ax.annotate('', xy=(q['p'] * 0.35 + 1.2, y_core + 0.6), xytext=(1.2, y_core + 0.6),
                    arrowprops=dict(arrowstyle='->', color='k', lw=1.5))
        ax.axhline(q['R2'], color=WHEEL, ls='-.', lw=1)
        ax.text(-2 * q['p'] + 0.2, q['R2'] - 0.9, 'línea de rodadura (y = R2)', color=WHEEL, fontsize=9)
        ax.set_title('plano z = %.1f' % c)
        ax.set_xlim(-2 * q['p'], 2 * q['p'])
        ax.set_ylim(q['R2'] - 2.5, q['A'] - 4)
        ax.set_aspect('equal')
        ax.set_xlabel('x, a lo largo del eje del sinfín (mm)')
    axs[0].set_ylabel('y (mm)')
    fig.suptitle('2. En cada plano, el sinfín cortado es como una cremallera. Al girarlo, esa sección no cambia de forma:\n'
                 'solo se desliza en X (en discontinua, la misma sección con el sinfín algo girado)', fontsize=11)
    save(fig, 'calc_2_seccion.png')


# ------------------------------------------------ 3. envolvente: la rueda quieta y el cortador rodando alrededor
def _gap_axes(ax):
    pitch = 2 * math.pi / q['teeth']
    for r, st, lab in ((q['R2'], '-.', 'primitivo'), (q['rim'], '-', None)):
        a = [math.pi / 2 + pitch * 1.6 * (i / 50 - 0.5) * 2 for i in range(51)]
        ax.plot([r * math.cos(t) for t in a], [r * math.sin(t) for t in a], color=WHEEL, ls=st, lw=1)
    ax.set_xlim(-4.2, 4.2)
    ax.set_ylim(q['R2'] - 3.2, q['rim'] + 1.6)
    ax.set_aspect('equal')
    ax.set_xticks([])
    ax.set_yticks([])


def thetas(n):
    tm = 2.2 * 2 * math.pi / q['teeth']
    return [-tm + 2 * tm * i / (n - 1) for i in range(n)]


def fig_envolvente():
    fig, axs = plt.subplots(1, 2, figsize=(10, 4.3))
    for ax, c in zip(axs, (0.0, C_OFF)):
        for th in thetas(45):
            ax.plot(*zip(*to_wheel(cutter_outline(c, ks=range(-3, 4)), th)), color=WORM, lw=0.6, alpha=0.45)
        ax.add_patch(Polygon(gap_polygon(c), closed=True, fill=False, ec=ENV, lw=2.2))
        _gap_axes(ax)
        ax.set_title('plano z = %.1f' % c)
    from matplotlib.lines import Line2D
    fig.legend([Line2D([], [], color=WORM), Line2D([], [], color=ENV, lw=2.2),
                Line2D([], [], color=WHEEL), Line2D([], [], color=WHEEL, ls='-.')],
               ['el sinfín en 45 posiciones, visto desde la rueda', 'hueco del diente (la envolvente; sale 1 mm del borde para cortar limpio)',
                'borde de la rueda', 'primitivo de la rueda'], loc='lower center', ncol=2, frameon=False)
    fig.suptitle('3. Lo que barre el sinfín al engranar es el hueco del diente de la rueda', y=0.93)
    fig.subplots_adjust(bottom=0.16)
    save(fig, 'calc_3_envolvente.png')


def gif_envolvente():
    from PIL import Image
    import io
    frames = []
    ths = thetas(36)
    for n in range(len(ths) + 6):
        fig, ax = plt.subplots(figsize=(5, 4.6))
        for th in ths[:min(n, len(ths))]:
            ax.plot(*zip(*to_wheel(cutter_outline(0.0, ks=range(-3, 4)), th)), color=WORM, lw=0.6, alpha=0.4)
        if n < len(ths):
            pts = to_wheel(cutter_outline(0.0, ks=range(-3, 4)), ths[n])
            ax.add_patch(Polygon(pts + [(pts[-1][0], 99), (pts[0][0], 99)], closed=True, fc=WORM, ec=WORM, alpha=0.85))
        else:
            ax.add_patch(Polygon(gap_polygon(0.0), closed=True, fill=False, ec=ENV, lw=2.5))
        _gap_axes(ax)
        ax.set_title('el sinfín "talla" el hueco (visto desde la rueda)', fontsize=10)
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=80, facecolor='white')
        plt.close(fig)
        frames.append(Image.open(buf).convert('RGB'))
    # paleta común sacada de un fotograma con el sinfín relleno y del último (con la envolvente roja)
    mid, last = frames[len(ths) // 2], frames[-1]
    both = Image.new('RGB', (mid.width * 2, mid.height))
    both.paste(mid, (0, 0))
    both.paste(last, (mid.width, 0))
    pal = both.quantize(colors=64)
    qf = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    qf[0].save(os.path.join(HERE, 'calc_envolvente.gif'), save_all=True, append_images=qf[1:],
               duration=[90] * len(ths) + [400] * 5 + [1500], loop=0, optimize=True)
    print('ok calc_envolvente.gif')


# ------------------------------------------------ 4. la ecuación: la normal pasa por el punto primitivo
def fig_normal():
    fig, ax = plt.subplots(figsize=(8, 5.6))
    # un punto del flanco derecho del filete central, cerca de la cabeza del sinfín (plano medio, c = 0)
    y = q['R2'] - 0.6
    u = y - q['A']
    x = G.hw(q, abs(u), q['backlash'] / 2)
    dx = math.tan(q['alpha'])                      # x' = dx/dy del flanco
    th = (x + (y - q['R2']) / dx) / q['R2']        # la ecuación de engrane
    cx = x - q['R2'] * th                          # el sinfín, desplazado lo que toca para ese giro de la rueda
    pts = [(px - q['R2'] * th, py) for px, py in cutter_outline(0.0, ks=range(-2, 3))]
    ax.add_patch(Polygon(pts + [(pts[-1][0], 99), (pts[0][0], 99)], closed=True, fc=WORM, ec=WORM, alpha=0.55))
    gap = [G._rot(th, gx, gy) for gx, gy in gap_polygon(0.0)]   # la rueda, girada theta
    ax.add_patch(Polygon(gap, closed=True, fill=False, ec=WHEEL, lw=1.8))
    a = [math.pi / 2 + 0.3 * (i / 50 - 0.5) for i in range(51)]
    ax.plot([q['R2'] * math.cos(t) for t in a], [q['R2'] * math.sin(t) for t in a], color=WHEEL, ls='-.', lw=1.2)
    ax.axhline(q['R2'], color=GREY, ls=':', lw=1.2)
    ax.plot([cx, 0], [y, q['R2']], color=ENV, lw=2)
    ax.plot([cx], [y], 'o', color=ENV, ms=8, zorder=5)
    ax.plot([0], [q['R2']], 'o', color='k', ms=7, zorder=5)
    ax.annotate('punto de contacto', xy=(cx, y), xytext=(cx + 0.6, y - 1.0), fontsize=10, color=ENV,
                arrowprops=dict(arrowstyle='->', color=ENV))
    ax.annotate('P: punto primitivo', xy=(0, q['R2']), xytext=(-3.6, q['R2'] + 0.9), fontsize=10,
                arrowprops=dict(arrowstyle='->'))
    ax.text(cx + 0.15, (y + q['R2']) / 2 - 0.05, 'normal al flanco', color=ENV, fontsize=10)
    ax.text(-3.9, q['R2'] - 0.45, 'primitivo de la rueda', color=WHEEL, fontsize=9)
    ax.text(1.9, q['R2'] + 0.08, 'línea de rodadura del sinfín', color=GREY, fontsize=9)
    ax.text(-0.5, q['A'] - 6.45, 'sinfín', color=WORM, fontsize=11, weight='bold')
    rr = G.root_radius(q, 0.0)
    ax.annotate('hueco de un diente de la rueda\n(el filete del sinfín entra en él)',
                xy=(-rr * math.sin(th), rr * math.cos(th)), xytext=(-3.8, q['R2'] - 2.5), fontsize=10, color=WHEEL,
                arrowprops=dict(arrowstyle='->', color=WHEEL))
    ax.set_xlim(-4, 4)
    ax.set_ylim(q['R2'] - 3, q['A'] - 6)
    ax.set_aspect('equal')
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title('4. Con la rueda girada θ, el sinfín toca en el punto de su flanco cuya normal pasa por P.\n'
                 'Despejando: θ = (x + (y − R2)/x′) / R2, una fórmula directa por cada punto del flanco', fontsize=11)
    save(fig, 'calc_4_normal.png')


# ------------------------------------------------ 5. perfiles apilados → solevación
def fig_perfiles():
    fig = plt.figure(figsize=(8, 6))
    ax = fig.add_subplot(projection='3d')
    cmap = plt.get_cmap('viridis')
    zs = q['section_z']
    for i, c in enumerate(zs):
        pts = gap_polygon(c)
        pts.append(pts[0])
        ax.plot([p[0] for p in pts], [p[1] for p in pts], [c] * len(pts), color=cmap(i / (len(zs) - 1)),
                lw=2.0 if i in (0, len(zs) // 2, len(zs) - 1) else 0.9)
    ax.set_xlabel('x (mm)')
    ax.set_ylabel('y (mm)')
    ax.set_zlabel('z (mm)')
    ax.set_box_aspect((1, 0.8, 1.6))
    ax.locator_params(nbins=4)
    ax.view_init(elev=18, azim=-60)
    ax.set_title('5. Los %d perfiles del hueco, uno por plano: una solevación los une en un sólido,\n'
                 'que se repite %d veces (patrón) y se resta del disco' % (len(zs), q['teeth']))
    save(fig, 'calc_5_perfiles.png')


if __name__ == '__main__':
    fig_planos()
    fig_seccion()
    fig_envolvente()
    gif_envolvente()
    fig_normal()
    fig_perfiles()
