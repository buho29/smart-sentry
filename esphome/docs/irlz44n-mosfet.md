# MOSFET de conmutación IRLZ44N

Ficha de referencia del MOSFET usado como interruptor de bajo lado para
cortar la alimentación de los [servos](servos-pan-tilt.md) y disparar el
motor de la [pistola de agua ZC005](bambulab-zc005-water-gun.md). El
esquema completo de ese circuito (resistencia de gate, pull-down, diodo
flyback) está en [`servos-pan-tilt.md`](servos-pan-tilt.md#cortar-la-alimentación-de-los-servos-ahorro-en-reposo)
y como archivo de KiCad en
[`diagrams/mosfet-load-switch/`](diagrams/mosfet-load-switch/); este
documento se centra solo en el componente.

## Identificación

- **Fabricante:** International Rectifier (hoy Infineon).
- **Tipo:** MOSFET de canal N, **logic-level** (la "L" del nombre) — se
  satura completamente con tensiones de puerta bajas, a diferencia de un
  MOSFET de potencia "normal" que necesita ~10V en la puerta.
- **Encapsulado:** TO-220 (3 patillas: Gate, Drain, Source; con lengüeta
  metálica trasera unida al Drain, para disipador si hiciera falta).

## Specs clave

| Parámetro | Valor | Nota |
| --- | --- | --- |
| V_DS máxima | 55V | Muy por encima de los 5V de este proyecto |
| I_D continua | 47A (a 25°C, con disipador) | Sobredimensionado para este uso |
| V_GS(th) (umbral) | 1.0–2.0V | Ya conduce por encima de este valor |
| R_DS(on) | ~0.022Ω a V_GS=10V / ~0.028Ω a V_GS=5V | Sube algo más a 3.3V (ver abajo) |
| Carga de puerta (Qg) | ~48nC | Modesta; un GPIO la carga sin problema a estas frecuencias de conmutación tan bajas |
| Disipación máxima | ~110W (con disipador adecuado) | Muy por encima de lo que disipará aquí |

## Por qué encaja en este proyecto

- **Nivel lógico real:** aunque está pensado para 5V/10V de puerta, su
  umbral (1.0–2.0V) está por debajo de los **3.3V** que da un GPIO de la
  ESP32-S3 directamente — se satura sin necesitar un driver de puerta
  aparte. A 3.3V la R_DS(on) es algo mayor que la de la hoja de datos
  (que se mide a 5V/10V), pero para las corrientes de este proyecto
  (0.9A del motor de la pistola, hasta ~1-1.5A de los servos según
  [servos-pan-tilt.md](servos-pan-tilt.md)) esa diferencia no genera
  calentamiento apreciable.
- **Muy sobredimensionado en corriente:** 47A de capacidad frente a <2A
  reales de carga — no hace falta disipador para este uso; el margen
  térmico es enorme.
- **Ya en la mano del usuario:** no es una elección de catálogo, es la
  pieza que ya estaba disponible (reaprovechada de un proyecto de tira de
  LED), de ahí que este documento parta del componente real en vez de
  recomendar uno nuevo.

## Notas de la implementación en este proyecto

- El circuito completo (resistencia de gate ~220Ω, pull-down ~10kΩ en la
  puerta, diodo flyback en la carga) está documentado y verificado con
  `kicad-cli` en [`servos-pan-tilt.md`](servos-pan-tilt.md#cortar-la-alimentación-de-los-servos-ahorro-en-reposo) —
  no se repite aquí para no duplicar.
- **Drain vs. Source:** el Drain va hacia la carga (servo o motor de la
  pistola), el Source va directo a GND — no hay ninguna resistencia entre
  Source y GND; el pull-down de 10kΩ solo está entre la puerta y GND, para
  evitar que el MOSFET conduzca sin querer si el GPIO queda flotando
  durante el arranque de la ESP32.
- Al ser TO-220, el pin central (con la lengüeta metálica) suele ser el
  Drain — comprobar la serigrafía del componente físico concreto antes de
  soldar, ya que el pinout G-D-S por posición puede variar entre fabricantes
  de clones.

---

*Documento de referencia de hardware. Componente aún no soldado en ningún
YAML/placa activa del proyecto.*
