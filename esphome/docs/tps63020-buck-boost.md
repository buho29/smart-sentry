# Módulo regulador buck-boost TPS63020

Ficha de referencia del módulo regulador pensado para el nodo exterior a
batería + panel solar ("espantapájaros" / water-tower-defense, ver
[`README.md`](../../README.md)). Igual que el [CN3791](cn3791-mppt-charger.md),
todavía no está cableado en ningún YAML del proyecto. Imagen original en
[`img/TPS63020.webp`](img/TPS63020.webp) (ficha del vendedor ICGOICIC, sin
URL propia guardada).

![TPS63020 buck-boost](img/TPS63020.webp)

## Identificación

- **Chip principal:** **TPS63020** (Texas Instruments) — convertidor
  **buck-boost** de alta eficiencia con un único inductor: regula la salida
  tanto si la entrada está por encima como por debajo de la tensión de
  salida objetivo.
- **Corriente de salida:** hasta **2A en modo boost** (subiendo desde una
  Li-ion 1S hacia 5V, que es el caso de este proyecto) y hasta 4A en modo
  buck puro, según el [datasheet de TI](https://www.ti.com/lit/ds/symlink/tps63020.pdf).
  Ese 2A es el techo del chip en condiciones ideales, no una cifra
  garantizada por cualquier placa genérica que lo monte con una bobina más
  pequeña — no dar por hecho que un módulo barato de AliExpress alcanza el
  máximo del datasheet.
- **Función en el proyecto:** convertir la tensión variable de una batería
  Li-ion 1S (3.0-4.2V, según carga y estado, ya sea directa o vía el
  [CN3791](cn3791-mppt-charger.md)) en una salida fija estable — necesaria
  porque esa batería nunca da directamente los 5V que espera la placa
  ESP32-S3-CAM.

## Cuánto da en la práctica (no solo en el datasheet)

El propio [foro E2E de TI](https://e2e.ti.com/support/power-management-group/power-management/f/power-management-forum/441028/tps63020-maximum-output-current)
tiene un caso de uso real muy parecido al de este proyecto: alguien
alimentando una carga a ráfagas de 2A (un amplificador GSM) desde una **Li-ion
1S**, igual que aquí. Lo que reportó:

- Con la batería a **3.0V** (célula casi descargada, el peor caso real de un
  nodo solar tras la noche), el máximo que consiguió sostener fueron
  **~1.9A** — ya por debajo del "2A" de la hoja de datos.
- Al pedir 2.0A la tensión de salida empezó a caer, y a 2.1A ya colapsaba
  más — es decir, pasado ese punto no hay un corte limpio, sino un
  **sag** (la tensión se hunde) que puede resetear la ESP32-S3 antes de que
  salte ninguna protección.
- Esto es con **3 módulos distintos** probados, así que no es un caso
  aislado de una placa defectuosa concreta.

Conclusión práctica: el "2A" del datasheet es optimista para el caso de este
proyecto (boost desde 1S, con la célula parcialmente descargada). Para
presupuestar con margen real, mejor tratar el límite utilizable como
**~1.5A**, no 2A — y tener en cuenta que ese límite baja más todavía cuando
la batería está más descargada (justo cuando el panel solar lleva más tiempo
sin cargarla).

## Conectores y pines del módulo

| Pin       | Función                                                                 |
| --------- | ------------------------------------------------------------------------- |
| `VIN`     | Entrada de alimentación (batería)                                         |
| `GND` (entrada) | Masa de entrada                                                      |
| `EN`      | Habilitación del regulador (activo, normalmente a nivel alto para encender) |
| `PS`      | Selección de modo **Power Save** (ahorro de energía a cargas ligeras) vs. modo PWM forzado |
| `OUT`     | Salida regulada                                                            |
| `GND` (salida) | Masa de salida                                                        |

La tensión de salida se fija por **puente de soldadura** entre las almohadillas
serigrafiadas `3V3` / `4V2` / `5V` junto al pad `OUT` — hay que puentear la
opción deseada antes de usar el módulo; de fábrica puede no venir puenteado.

## Notas de integración con el proyecto

- Para alimentar la placa ESP32-S3-CAM (que espera 5V en su entrada `5V` o vía
  USB-C) hay que puentear la salida a **5V**.
- Cadena de alimentación prevista para el nodo autónomo:
  panel solar → `CN3791` (carga MPPT) → batería 1S → `TPS63020` (puenteado a
  5V) → pin `5V` de la placa ESP32-S3-CAM.
- El pin `EN` podría usarse para cortar la alimentación de toda la etapa de
  potencia (servos, pistola de agua) de forma controlada, si el diseño final
  del nodo lo requiere — no implementado todavía en ningún YAML.
- `PS` afecta la eficiencia en reposo: en un nodo a batería con deep sleep
  (como `huerta.yaml`) interesa dejarlo en modo Power Save para minimizar el
  consumo en standby, en vez de forzar PWM continuo.
- **Los servos de la torreta cuelgan de la misma salida de 5V**, no de una
  tensión propia: al puentear el `TPS63020` a 5V para la cámara, esa es
  también la tensión fija que reciben los servos de
  [`servos-pan-tilt.md`](servos-pan-tilt.md). Eso descarta el servo original
  **Blue Arrow D03012** (máximo 4.2V) para esta cadena de alimentación; los
  reemplazos recomendados en esa ficha (ES9051, DSM44, MG90S, DS3218MG) sí
  admiten 5V dentro de su rango normal. Si en el futuro se quisiera usar el
  D03012 pese a todo, el pad `4V2` de este módulo encajaría con su rango,
  pero entonces haría falta un segundo regulador/módulo para seguir dando 5V
  a la cámara por separado.
- **El límite real no es solo de tensión, es de corriente de pico**: los 2A
  máximos de este módulo en modo boost tienen que cubrir a la vez la placa
  y los dos servos moviéndose o forzando contra un tope mecánico. Con
  servos pico (originales o sus reemplazos ES9051/DSM44) hay margen; con
  MG90S el margen es escaso; con **DS3218MG/DS3225MG los dos servos parados
  a la vez ya piden ~4.2A ellos solos**, muy por encima de lo que da este
  módulo — en ese caso los servos necesitan su propia etapa de alimentación,
  separada de la que alimenta la ESP32-S3-CAM. Detalle completo y tabla de
  corrientes de stall en [`servos-pan-tilt.md`](servos-pan-tilt.md#el-pico-de-consumo-real-dos-servos--cámara-en-el-mismo-tps63020).
- **Reparto de rieles con el [MT3608](mt3608-boost-converter.md):** en vez de
  colgar cámara y servos del mismo `TPS63020`, se puede dedicar este módulo
  solo a la ESP32-S3-CAM y usar el MT3608 (que ronda un techo real parecido,
  ~1A) como riel de 5V separado para los servos pico/MG90S. Ver el reparto
  completo en esa ficha.
- **El motor de la [pistola de agua ZC005](bambulab-zc005-water-gun.md) no
  debería colgar de este riel de 5V**: corre de fábrica a 3.7V, así que sale
  más simple y no compite por el presupuesto de corriente de este módulo
  alimentarlo directo desde la batería 1S, en paralelo con la entrada
  `VIN` de este mismo `TPS63020`.

---

*Documento de referencia de hardware, generado a partir de la captura del
vendedor en [`img/TPS63020.webp`](img/TPS63020.webp). Componente aún no
cableado en ningún YAML del proyecto.*
