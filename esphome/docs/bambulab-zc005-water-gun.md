# Mecanismo de disparo: Bambu Lab Electric Water Spray Kit 02 (ZC005)

Ficha de referencia del kit que hará de "pistola de agua eléctrica" del nodo
final del proyecto (ver README, sección "water-tower-defense"). No es una
pieza genérica de bazar sino un **kit oficial de Bambu Lab** pensado para
montar réplicas de juguete imprimibles en 3D (modelo MakerWorld
["FMG9"](https://makerworld.com/en/models/770810-fmg9-water-spray-electric-water-spray-kit-02),
carcasa estilo TEC-9/T-15) — aquí se reaprovecha solo el mecanismo eléctrico,
no la carcasa de juguete.

## Identificación

- **Kit:** Bambu Lab "Electric Water Spray Kit 02", pieza interna
  identificada como **ZC005** (existe también en variantes ZC004/ZC006/
  ZH054/ZH055 para otros modelos de carcasa del mismo fabricante).
- **Motor:** DC, **3.7V / ~0.9A** en funcionamiento normal.
- **Batería de serie:** 18650 Li-ion **3.7V 2600mAh**, con cargador externo
  USB de **5V 1-2A** por conector **PH2.0** (el cargador solo carga la
  batería; no es la alimentación del motor, que sale directo de la 18650).
- **Prestaciones de fábrica:** alcance 7-8m, **8 ráfagas/segundo**, 133.9g,
  ~100×135×45mm.
- **Incluye:** el módulo motor+mecanismo, la batería 18650, el cargador y el
  pulsador de disparo. **No incluye bomba de autollenado** (eso es exclusivo
  del "Kit 01"): el depósito se rellena a mano.

## Cómo dispara (mecanismo)

Coincide con lo observado a simple vista: un **motor DC mueve un husillo**
(tornillo sin fin) que empuja/retrae un émbolo cargado por muelle contra su
recorrido; al llegar al final del husillo, un perfil de leva **suelta el
émbolo de golpe**, que dispara el chorro de agua por la energía acumulada en
el muelle — no es el motor empujando el agua directamente, es un mecanismo
de **carga y disparo por leva**, como un percutor. Mientras el motor sigue
girando, el ciclo se repite una y otra vez, dando el disparo **a ráfagas**
(los 8 "rounds/segundo" de fábrica) en vez de un chorro continuo — de ahí
también la "función de recoil simulado" que anuncia Bambu Lab: cada ráfaga
da un tirón mecánico perceptible.

De fábrica el disparo es puramente mecánico/eléctrico simple: un **pulsador**
en serie con el motor y la batería 18650. Eléctricamente es un motor DC de
bajo consumo activado por un interruptor — no lleva ninguna electrónica de
control propia que haya que respetar ni "hablar" con protocolo alguno.

## Integración en el proyecto

- **Sustituir el pulsador por un GPIO del ESP32-S3**, siguiendo la
  recomendación ya comentada de usar un **MOSFET de canal N a nivel lógico**
  (con diodo flyback en el motor) en vez de un relé: es un motor DC de bajo
  consumo (0.9A en marcha), conmuta en silencio y sin desgaste — encaja
  igual de bien aquí que para una electroválvula. "Disparar una ráfaga"
  desde firmware es simplemente poner el GPIO en alto el tiempo necesario
  para que el husillo complete uno o varios ciclos de leva, no un PWM.
- **Corriente a presupuestar:** 0.9A en marcha es modesto comparado con los
  servos de la torreta (ver [`servos-pan-tilt.md`](servos-pan-tilt.md)),
  pero un motor DC puede dar un **pico de arranque/atasco de 3-5× la
  corriente nominal** (~2.7-4.5A) si el husillo se traba — dimensionar el
  MOSFET y cualquier fusible/PTC de protección con ese margen, no solo con
  los 0.9A nominales.
- **Tensión: no hace falta pasar por el riel de 5V.** El motor de fábrica ya
  corre a 3.7V, que es prácticamente la tensión nativa de la batería Li-ion
  1S del nodo (3.0-4.2V) descrita en
  [`cn3791-mppt-charger.md`](cn3791-mppt-charger.md). Lo más simple es
  alimentar este motor **directamente desde la batería principal**, en
  paralelo con la entrada del [TPS63020](tps63020-buck-boost.md) y del
  [TPS61088](tps61088-boost-converter.md), en vez de sacarlo de una salida
  ya regulada — así no se añade esta carga al presupuesto de corriente de
  los rieles de la cámara (3.3V) ni de los servos (5V).
- **Batería:** se puede prescindir de la 18650 propia del kit y alimentar el
  motor desde la batería única del nodo, o mantenerla como una segunda
  batería dedicada solo al disparo si se prefiere aislar esa carga por
  completo del resto del sistema — a decidir según cómo evolucione el
  diseño final del "espantapájaros".
- **Sin bomba de autollenado:** el depósito de agua de este kit concreto
  (ZC005 / Kit 02) hay que rellenarlo a mano; si el nodo final necesita
  recarga automática, esa pieza vendría del "Kit 01" (u otra solución aparte),
  no de este componente.

El esquema de este interruptor (mismo circuito que para los servos) está en
[`servos-pan-tilt.md`](servos-pan-tilt.md#cortar-la-alimentación-de-los-servos-ahorro-en-reposo)
como SVG y también editable en
[KiCad](diagrams/mosfet-load-switch/mosfet-load-switch.kicad_pro).

---

*Documento de referencia de hardware, sin foto propia adjunta en este
proyecto — datos recopilados de la ficha oficial de Bambu Lab, Alibaba y el
modelo MakerWorld FMG9 que usa este mismo kit. Componente aún no integrado
en ningún YAML del proyecto.*
