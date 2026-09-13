# Comparativa de convertidores step-up (boost) para el nodo solar

Tabla resumen de los módulos/chips step-up considerados para elevar la
tensión de la batería Li-ion 1S (3.0-4.2V) a 5V en el nodo autónomo
("water-tower-defense", ver [`README.md`](../../README.md)). Cada fila tiene
su propia ficha con el detalle completo; este documento es solo para
comparar de un vistazo y decidir qué va en cada riel.

## Tabla comparativa

| Módulo / chip | Tipo | Corriente real utilizable | Consumo con el regulador habilitado, sin carga | Consumo si se corta con su propio pin | ¿El breakout expone ese pin? | Rol recomendado |
| --- | --- | --- | --- | --- | --- | --- |
| **[MT3608](mt3608-boost-converter.md)** | Boost puro | ~0.8-1A sostenidos (el "2A" del rótulo es optimista) | 100-200µA en PFM (carga ligera) a 1.6-2.2mA en PWM | No aplica — el chip no tiene pin EN | No | Riel de servos **pico/MG90S** si no se necesita cortar la alimentación por software |
| **[XL6009](xl6009-boost-converter.md)** | Boost/buck-boost/inversor | ~1-1.5A con módulo genérico (aunque el rótulo diga 4A) | **2.5-5mA** — el peor de los tres módulos "de trimpot" | 70-100µA con EN=0V, pero el breakout típico no saca ese pin a ningún pad | No (chip sí tiene EN, el módulo no lo expone) | Riel de servos **detrás de un MOSFET externo** ([IRLZ44N](irlz44n-mosfet.md)) que corte el consumo en deep sleep, ver la ficha del XL6009 |
| **[TPS63020](tps63020-buck-boost.md)** | Buck-boost | ~1.5A real (el "2A" del datasheet es optimista con 1S descargada) | **~25µA** en modo Power Save | **<1µA** con EN a nivel bajo | Sí, normalmente como pad `EN` | Riel de **lógica/cámara** (ESP32-S3-CAM) — carga constante y predecible |
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
   [`servos-pan-tilt.md`](servos-pan-tilt.md#cortar-la-alimentación-de-los-servos-con-un-mosfet-ahorro-en-reposo)).
3. **TPS63020 (lógica) + TPS61088 (servos).** Sin MOSFET externo — el
   propio módulo, si expone `EN` como anuncia el listado de AliExpress
   citado en [`tps61088-boost-converter.md`](tps61088-boost-converter.md),
   resuelve el apagado sin componentes extra y con más margen de corriente
   (10A de switch) que el XL6009. Es la opción más simple de las tres si se
   confirma que el módulo concreto que llegue trae ese pin accesible.

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
ESP32-S3-CAM y los servos desde un único módulo. Aun así, **no conviene
fusionarlo todo en un solo riel**, por dos motivos que no desaparecen solo
por tener más amperios disponibles:

1. **Problema de topología, no de corriente:** si el mismo TPS61088
   alimentase tanto a la ESP32 como a los servos, cortar ese riel para
   ahorrar el consumo de los servos en deep sleep cortaría también la
   alimentación de la propia ESP32 — que es quien tiene que llevar el pin
   `EN` a nivel alto para volver a encenderlo. La placa no puede apagar la
   fuente que la alimenta a sí misma y esperar seguir viva para
   despertarla luego. Esto es justo lo que ya obligaba a
   [`tps63020-buck-boost.md`](tps63020-buck-boost.md#notas-de-integración-con-el-proyecto)
   a mantener la lógica en un riel aparte del de los servos.
2. **Sag transitorio bajo stall, independiente de la corriente media:**
   [`tps63020-buck-boost.md`](tps63020-buck-boost.md#el-pico-de-consumo-real-dos-servos--cámara-en-el-mismo-tps63020)
   y [`servos-pan-tilt.md`](servos-pan-tilt.md) documentan que compartir
   riel entre servos y lógica causa brownouts porque un servo forzando
   contra un tope hunde momentáneamente la tensión de salida del mismo
   lazo de realimentación que alimenta a la ESP32 — un problema de
   respuesta transitoria y condensador de salida compartido, no solo de
   cuántos amperios de media soporta el chip. El "10A" del datasheet no
   garantiza por sí solo que no haya ese sag; habría que validarlo en
   banco si algún día se quisiera compartir de todas formas.

**Recomendación:** mantener dos etapas de potencia separadas. Como
variante de las combinaciones de arriba, sí tiene sentido usar **dos
TPS61088** (uno fijo para lógica, otro con `EN` conmutado para servos) en
vez de TPS63020+XL6009 o TPS63020+TPS61088 — unifica el catálogo de
piezas a un solo componente y da más margen de corriente que cualquiera de
las otras combinaciones, sin caer en el problema de auto-apagado descrito
arriba.

---

*Documento de referencia de hardware, sin código ni YAML asociado. Ver las
fichas individuales enlazadas arriba para el detalle de cada componente.*
