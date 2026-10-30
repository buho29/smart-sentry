# Add-in de Fusion 360: "Sinfín y rueda" (SÓLIDO > CREAR).
# Diálogo de parámetros -> wormgear.build crea el sinfín y la rueda lisa; wormgear.check comprueba el engrane.
import importlib
import json
import os
import sys
import traceback

import adsk.core
import adsk.fusion

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import wormgear.geometry  # noqa: E402
import wormgear.build  # noqa: E402
import wormgear.check  # noqa: E402
for _m in (wormgear.geometry, wormgear.build, wormgear.check):   # recargar al reiniciar el add-in
    importlib.reload(_m)
from wormgear import geometry as G, build as B, check as C  # noqa: E402

CMD_ID = 'cupulaWormGear'
# SÓLIDO > CREAR (diseños de pieza e híbridos) y ENSAMBLAJE > CREAR (diseños de ensamblaje)
PANEL_IDS = ('SolidCreatePanel', 'CreatePanel')
SETTINGS = os.path.join(HERE, 'settings.json')

_app = adsk.core.Application.get()
_ui = _app.userInterface
_handlers = []
_state = {}   # comando abierto y última validación (útil para depurar)

# (id del grupo, título, [(parámetro, etiqueta, tipo, mín, máx, paso)])
#   tipos: len = mm, ang = grados, int = entero, float = número sin unidades
FIELDS = [
    ('g_gear', 'Engrane', [
        ('module', 'Módulo', 'len'),
        ('starts', 'Entradas del sinfín', 'int', 1, 4, 1),
        ('teeth', 'Dientes de la rueda', 'int', 3, 300, 1),
        ('worm_pitch_d', 'Ø primitivo del sinfín', 'len'),
        ('flank_deg', 'Ángulo de flanco', 'ang'),
        ('addendum_f', 'Cabeza (× módulo)', 'float', 0.1, 1.5, 0.05),
        ('dedendum_f', 'Pie (× módulo)', 'float', 0.1, 1.5, 0.05),
        ('clearance', 'Holgura radial', 'len'),
        ('backlash', 'Holgura entre flancos', 'len'),
    ]),
    ('g_worm', 'Sinfín', [
        ('worm_length', 'Longitud', 'len'),
        ('worm_bore', 'Ø taladro (0 = sin taladro)', 'len'),
    ]),
    ('g_wheel', 'Rueda', [
        ('wheel_width', 'Ancho', 'len'),
        ('wheel_bore', 'Ø taladro (0 = sin taladro)', 'len'),
        ('rim_over_tip', 'Borde sobre la cabeza', 'len'),
        ('sections', 'Perfiles de la solevación', 'int', 5, 80, 2),
    ]),
]
WHAT = [('both', 'Sinfín y rueda'), ('worm', 'Solo el sinfín'), ('wheel', 'Solo la rueda')]


def _load_settings():
    s = dict(G.DEFAULTS, what='both', check=True)
    try:
        with open(SETTINGS, encoding='utf-8') as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    return s


def _save_settings(s):
    try:
        with open(SETTINGS, 'w', encoding='utf-8') as f:
            json.dump(s, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


def _read(inputs):
    """Parámetros del diálogo (mm y grados). Devuelve (dict, etiquetas de los campos que no se entienden)."""
    prm, bad = {}, []
    for _, _, fields in FIELDS:
        for f in fields:
            key, label, kind = f[0], f[1], f[2]
            inp = inputs.itemById(key)
            if kind in ('len', 'ang'):
                if not inp.isValidExpression:
                    bad.append(label)
                    continue
                prm[key] = inp.value * 10.0 if kind == 'len' else inp.value * 180.0 / 3.141592653589793
            else:
                prm[key] = inp.value
    prm['starts'] = int(prm.get('starts', 1))
    prm['teeth'] = int(prm.get('teeth', 30))
    prm['sections'] = int(prm.get('sections', 23))
    return prm, bad


def _evaluate(inputs):
    """(html del resumen, ¿se puede generar?). El resumen explica siempre por qué no se puede."""
    prm, bad = _read(inputs)
    if bad:
        return ('<b><font color="#d33">✖ No entiendo el valor de: %s.</font></b><br>'
                'Escribe un número con punto decimal y, si quieres, la unidad (p. ej. <i>1.5 mm</i>, <i>45 deg</i>).'
                % ', '.join(bad)), False
    try:
        q, errors, warns, summary = G.check_params(prm)
    except Exception as e:   # combinación que la matemática no sabe tratar
        return '<b><font color="#d33">✖ Con estos valores no sale geometría: %s</font></b>' % e, False
    html = summary.replace('\n', '<br>')
    for e in errors:
        html += '<br><b><font color="#d33">✖ %s</font></b>' % e
    for w in warns:
        html += '<br><font color="#c80">⚠ %s</font>' % w
    return html, not errors


class _CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd = args.command
            _state['cmd'] = cmd
            cmd.okButtonText = 'Generar'
            cmd.setDialogMinimumSize(420, 600)
            inputs = cmd.commandInputs
            s = _load_settings()
            for gid, title, fields in FIELDS:
                grp = inputs.addGroupCommandInput(gid, title)
                grp.isExpanded = True
                gi = grp.children
                for f in fields:
                    key, label, kind = f[0], f[1], f[2]
                    v = s.get(key, G.DEFAULTS[key])
                    if kind == 'len':
                        gi.addValueInput(key, label, 'mm', adsk.core.ValueInput.createByString('%g mm' % v))
                    elif kind == 'ang':
                        gi.addValueInput(key, label, 'deg', adsk.core.ValueInput.createByString('%g deg' % v))
                    elif kind == 'int':
                        gi.addIntegerSpinnerCommandInput(key, label, f[3], f[4], f[5], int(v))
                    else:
                        gi.addFloatSpinnerCommandInput(key, label, '', f[3], f[4], f[5], float(v))
            opt = inputs.addGroupCommandInput('g_opt', 'Opciones').children
            dd = opt.addDropDownCommandInput('what', 'Generar', adsk.core.DropDownStyles.TextListDropDownStyle)
            for key, label in WHAT:
                dd.listItems.add(label, key == s.get('what', 'both'))
            opt.addBoolValueInput('check', 'Comprobar engrane al terminar', True, '', bool(s.get('check', True)))
            inputs.addTextBoxCommandInput('info', 'Resumen', _evaluate(inputs)[0], 12, True)

            for ev, h in ((cmd.execute, _ExecuteHandler()), (cmd.inputChanged, _ChangedHandler()),
                          (cmd.validateInputs, _ValidateHandler())):
                ev.add(h)
                _handlers.append(h)
        except Exception:
            _ui.messageBox('Sinfín y rueda: error al abrir el diálogo\n' + traceback.format_exc())


def _refresh(inputs):
    """Actualiza el resumen y devuelve si se puede generar. Si algo falla, lo dice en el resumen
    (así el botón Generar nunca se queda en gris sin explicación)."""
    try:
        html, valid = _evaluate(inputs)
    except Exception:
        html, valid = ('<b><font color="#d33">✖ Error interno del complemento:</font></b><br>%s'
                       % traceback.format_exc().replace('\n', '<br>')), False
    info = inputs.itemById('info')
    if info and info.formattedText != html:
        info.formattedText = html
    return valid


class _ChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        if args.input.id == 'info':
            return
        # args.inputs es solo el grupo del campo cambiado: hacen falta todos los del comando
        _refresh(args.firingEvent.sender.commandInputs)


class _ValidateHandler(adsk.core.ValidateInputsEventHandler):
    def notify(self, args):
        args.areInputsValid = _state['valid'] = _refresh(args.firingEvent.sender.commandInputs)


class _ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        inputs = args.command.commandInputs
        try:
            design = adsk.fusion.Design.cast(_app.activeProduct)
            if not design:
                args.executeFailed = True
                args.executeFailedMessage = 'Abre un diseño (espacio de trabajo DISEÑO) antes de usar Sinfín y rueda.'
                return
            prm, _ = _read(inputs)
            what = WHAT[inputs.itemById('what').selectedItem.index][0]
            do_check = inputs.itemById('check').value
            _save_settings(dict(prm, what=what, check=do_check))
            q, worm, wheel, log = B.build(design, prm, what)
            msg = list(log)
            if do_check and worm is not None and wheel is not None:
                res = C.check(q, worm, wheel)
                _, txt = C.report(res, q)
                msg.append('')
                msg.append(txt)
            _, _, warns, summary = G.check_params(prm)
            _ui.messageBox('\n'.join([summary, ''] + msg + ([''] + ['⚠ ' + w for w in warns] if warns else [])),
                           'Sinfín y rueda')
        except ValueError as e:   # parámetros que no dan geometría: mensaje claro, sin traza
            args.executeFailed = True
            args.executeFailedMessage = 'No se pudo generar: %s' % e
            _ui.messageBox('No se pudo generar:\n%s' % e, 'Sinfín y rueda')
        except Exception as e:
            args.executeFailed = True
            args.executeFailedMessage = 'No se pudo generar: %s' % e
            _ui.messageBox('Sinfín y rueda: error\n' + traceback.format_exc())


def run(context):
    try:
        cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()
        cmd_def = _ui.commandDefinitions.addButtonDefinition(
            CMD_ID, 'Sinfín y rueda',
            'Sinfín y su rueda dentada, paramétricos e imprimibles. La rueda se genera con perfiles '
            'de envolvente calculados y una solevación (dientes lisos, sin interferencias).',
            os.path.join(HERE, 'resources'))
        h = _CreatedHandler()
        cmd_def.commandCreated.add(h)
        _handlers.append(h)
        for pid in PANEL_IDS:
            panel = _ui.allToolbarPanels.itemById(pid)
            if panel and not panel.controls.itemById(CMD_ID):
                panel.controls.addCommand(cmd_def)
    except Exception:
        _ui.messageBox('Sinfín y rueda: error al arrancar\n' + traceback.format_exc())


def stop(context):
    try:
        for pid in PANEL_IDS:
            panel = _ui.allToolbarPanels.itemById(pid)
            ctrl = panel.controls.itemById(CMD_ID) if panel else None
            if ctrl:
                ctrl.deleteMe()
        cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()
        _handlers.clear()
    except Exception:
        _ui.messageBox('Sinfín y rueda: error al parar\n' + traceback.format_exc())
