# Comparativa de convertidores step-up (boost) para el nodo solar

Tabla resumen de los módulos/chips step-up considerados para regular la
tensión de la batería Li-ion 1S (3.0-4.2V) en el nodo autónomo
("water-tower-defense", ver [`README.md`](../../README.md)): **3.3V** para
la lógica/cámara y **5V** para los servos. Cada fila tiene su propia ficha
con el detalle completo; este documento es solo para comparar de un vistazo
y decidir qué va en cada riel.

**Decisión adoptada:** [TPS63020](tps63020-buck-boost.md) puenteado a
`3V3` → pin `3V3` de la ESP32-S3-CAM, y [TPS61088](tps61088-boost-converter.md)
fijado a 5V → servos MG90S, con `GND` común (combinación 3 más abajo).

## Tabla comparativa

| Módulo / chip | Tipo | Corriente real utilizable | Consumo con el regulador habilitado, sin carga | Consumo si se corta con su propio pin | ¿El breakout expone ese pin? | Rol recomendado |
| --- | --- | --- | --- | --- | --- | --- |
| **[MT3608](mt3608-boost-converter.md)** | Boost puro | ~0.8-1A sostenidos (el "2A" del rótulo es optimista) | 100-200µA en PFM (carga ligera) a 1.6-2.2mA en PWM | No aplica — el chip no tiene pin EN | No | Riel de servos **pico/MG90S** si no se necesita cortar la alimentación por software |
| **[XL6009](xl6009-boost-converter.md)** | Boost/buck-boost/inversor | ~1-1.5A con módulo genérico (aunque el rótulo diga 4A) | **2.5-5mA** — el peor de los tres módulos "de trimpot" | 70-100µA con EN=0V, pero el breakout típico no saca ese pin a ningún pad | No (chip sí tiene EN, el módulo no lo expone) | Riel de servos **detrás de un MOSFET externo** ([IRLZ44N](irlz44n-mosfet.md)) que corte el consumo en deep sleep, ver la ficha del XL6009 |
| **[TPS63020](tps63020-buck-boost.md)** | Buck-boost | ~1.5A real (el "2A" del datasheet es optimista con 1S descargada) | **~25µA** en modo Power Save | **<1µA** con EN a nivel bajo | Sí, normalmente como pad `EN` | Riel de **lógica/cámara** a **3.3V**, directo al pin `3V3` de la ESP32-S3-CAM (saltando el AMS1117) — carga constante y predecible, y buck-boost en su rango ideal alrededor de 3.3V |
| **[TPS61088](tps61088-boost-converter.md)** | Boost síncrono | **10A de switch** (mucho margen sobre los picos de servos, ~4.2A con dos DS3218MG) | **~100-250µA** (1-3µA por VIN + 110-250µA por el divisor de feedback en VOUT) | **1-3µA** con EN a nivel bajo | Sí — el chip tiene pull-down interno en EN (por defecto apagado si flota), así que el módulo *tiene* que exponerlo para poder funcionar; hay módulos reales en venta (p. ej. [este de AliExpress](https://es.aliexpress.com/item/1005009535413093.html)) | Mejor candidato para el riel de **servos** sin necesitar el MOSFET externo — sustituye a la combinación XL6009+IRLZ44N con un único componente |

## Cómo leer esta tabla

- **"Consumo con el regulador habilitado, sin carga"** es la cifra que
  importa para decidir si hace falta cortar la alimentación en deep sleep:
  cuanto más alta, más batería se va en no hacer nada.
- **"¿El breakout expone ese pin?"** es la trampa real de los módulos
  baratos: el MT3608 ni siquiera tiene EN en el chip, y el XL6009 lo tiene
  en el chip pero casi ningún módulo con trimpot lo saca a un pad — en la
  práctica, ambos se comportan igual de "siempre encendidos" salvo que se
  añada un interruptor externo.
- El TPS63020 y el TPS61088 son los únicos de la tabla con un apagado real
  accesible sin modificar la placa — y en el caso del TPS61088 no es
  opcional: al tener pull-down interno en `EN`, el propio chip obliga a
  cualquier módulo comercial a exponer ese control para poder arrancar.

## Combinaciones consideradas para este proyecto

1. **TPS63020 (lógica) + MT3608 (servos pico/MG90S), ambos siempre
   encendidos.** Sencillo, pero el MT3608 deja un consumo residual de
   0.1-2.2mA continuo en el riel de servos durante todo el sueño.
2. **TPS63020 (lógica) + XL6009 (servos) detrás de un IRLZ44N.** Es la
   combinación documentada en [`xl6009-boost-converter.md`](xl6009-boost-converter.md#rol-previsto-xl6009-como-riel-de-servos-cortado-por-un-irlz44n):
   reutiliza el XL6009 que el usuario ya tiene, a cambio de añadir un
   MOSFET y su circuito de puerta (ya documentado en
   [`irlz44n-mosfet.md`](irlz44n-mosfet.md) y en
   [`servos-pan-tilt.md`](servos-pan-tilt.md#cortar-la-alimentación-de-los-servos-ahorro-en-reposo)).
3. **TPS63020 a 3.3V (lógica) + TPS61088 a 5V (servos) — ELEGIDA.** Sin
   MOSFET externo — el propio módulo, si expone `EN` como anuncia el listado
   de AliExpress citado en
   [`tps61088-boost-converter.md`](tps61088-boost-converter.md), resuelve el
   apagado sin componentes extra y con más margen de corriente (10A de
   switch) que el XL6009. Es la opción más simple de las tres si se confirma
   que el módulo concreto que llegue trae ese pin accesible. El TPS63020 va
   puenteado a `3V3` y entra por el pin `3V3` de la placa, no por `5V`:
   así trabaja en buck la mayor parte de la descarga y se evita el AMS1117
   lineal de a bordo (detalle en su ficha).

En cualquiera de los tres casos, según lo documentado en
[`servos-pan-tilt.md`](servos-pan-tilt.md#consumo-si-no-se-corta-la-alimentación-durante-el-deep-sleep),
**cortar la alimentación de los servos importa más que optimizar el
regulador**: incluso el mejor boost de la tabla sin cortar (TPS63020, 25µA)
es insignificante comparado con lo que consumen los propios servos en
reposo si se dejan alimentados (desde unos pocos mA hasta varios cientos de
mA según el equilibrio mecánico de la torreta).

## ¿Y usar un único TPS61088 como riel para todo (lógica + servos)?

Con 10A de switch y hasta ~30W (~6A a 5V) según el datasheet TI, el
TPS61088 tiene de sobra corriente nominal para alimentar a la vez la
ESP32-S3-CAM y los servos desde un único módulo a 5V. Hay dos formas de
plantearlo, y solo una es viable:

### Variante A: un solo riel, sin nada más — descartada

Cortar el TPS61088 por `EN` para ahorrar el consumo de los servos en deep
sleep cortaría también la alimentación de la propia ESP32, que es quien
tiene que volver a poner `EN` a nivel alto. La placa no puede apagar la
fuente que la alimenta a sí misma y esperar seguir viva para despertarla
luego. Es un problema de topología, no de corriente.

### Variante B: un solo TPS61088 siempre encendido + [IRLZ44N](irlz44n-mosfet.md) cortando solo la rama de servos

Aquí el TPS61088 queda fijo a 5V y alimenta:

- la ESP32-S3-CAM por su pin `5V` (pasando por el AMS1117-3.3 de a bordo), y
- los servos a través de un interruptor de bajo lado con IRLZ44N gobernado
  por un GPIO — el mismo esquema ya documentado en
  [`servos-pan-tilt.md`](servos-pan-tilt.md#cortar-la-alimentación-de-los-servos-ahorro-en-reposo)
  para el motor de la pistola.

Esto **resuelve el problema de auto-apagado** de la variante A: la ESP32
nunca se queda sin tensión, y el corte de servos en reposo sigue existiendo
(vía MOSFET en vez de vía `EN`). A cambio:

| | Un TPS61088 + IRLZ44N (variante B) | TPS63020 a 3.3V + TPS61088 a 5V (elegida) |
| --- | --- | --- |
| Reguladores | 1 | 2 |
| Componentes extra | IRLZ44N + R gate + R pull-down + diodo flyback | Ninguno (`EN` del TPS61088 a un GPIO) |
| Sag por stall de servos | **Compartido** con la lógica: un servo forzando hunde el mismo lazo y condensador de salida que alimenta la ESP32 → riesgo de brownout (ver [`servos-pan-tilt.md`](servos-pan-tilt.md#el-pico-de-consumo-real-por-qué-los-servos-no-comparten-regulador-con-la-cámara)). El "10A" del datasheet no lo evita por sí solo; habría que validarlo en banco con un condensador grande cerca de la placa | Aislado: cada riel tiene su propio lazo |
| Eficiencia en lógica | 1S → boost a 5V → AMS1117 lineal a 3.3V: dos etapas, la segunda disipa (5−3.3)×I en calor | 1S → buck-boost directo a 3.3V por el pin `3V3`, una sola etapa |
| Consumo en reposo (servos cortados) | TPS61088 habilitado (~100-250µA) + AMS1117 (~5mA de Iq típico, **dominante**) | TPS63020 en Power Save (~25µA) + TPS61088 apagado (1-3µA) |
| Flasheo por USB | Sin precaución especial (el 5V del USB y el del TPS61088 se juntan en la entrada del AMS1117, como con cualquier fuente externa de 5V) | Hay que bajar `EN` del TPS63020 antes de enchufar USB |

**Recomendación:** la variante B es válida y más barata en módulos, pero
paga en autonomía (el AMS1117 solo ya consume más en reposo que los dos
reguladores de la opción elegida juntos) y en robustez ante el stall de los
servos. Para un nodo a batería con deep sleep, se mantiene la decisión de
**dos etapas separadas** (TPS63020 a 3.3V + TPS61088 a 5V). La variante B
queda como plan de respaldo si solo se dispone de un TPS61088.

---

*Documento de referencia de hardware, sin código ni YAML asociado. Ver las
fichas individuales enlazadas arriba para el detalle de cada componente.*

*Documento generado con IA; revisar los valores antes de montar.*
