# Estudio contrafactual — ¿reforzar, comprar en contra o cortar cuando la 0DTE pierde X%?

_Corrido el 2026-07-06 22:02 · QQQ,SPY,IWM 2026-01-02→2026-07-02 · 363 días-ticker con posición (de 375; 12 sin posición) · eventos: 3372 (38 excluidos por ask no usable en t*)._

## Método (resumen)
- Posición base de la casa: CALL y PUT 0DTE, $1,000 50/50, entrada 09:30 (ventana de búsqueda 4 min), contrato «Menor spread en rango óptimo», fills **Fase 2** (compra al ask, valuación/venta al bid por minuto). 100% cache local, 1 min.
- En el **primer cruce** de −X% (X ∈ 20/30/40/50/60) se bifurcan 4 ramas y corren hasta 15:55: **aguantar** · **reforzar** (+$500 de la pierna perdedora al ask de t*, patrón martingala del engine) · **contra** (+$500 de la pierna opuesta al ask de t*) · **cortar** (vender al bid de t*).
- Dos disparadores por separado: **combined** (la posición 50/50 cruza −X%; cortar vende ambas piernas) y **leg** (una pierna cruza −X% de SU capital; cortar vende solo esa pierna). En *leg*, los deltas vs cortar equivalen exactamente al caso de **pierna única** (la otra pierna es idéntica en las 4 ramas y se cancela).
- EV$ = P&L medio al cierre; EV% sobre el capital comprometido de cada rama ($1,000 hold/cortar; $1,500 reforzar/contra). win = % de eventos donde la rama termina con MÁS dinero que cortar. ✱ = t-test pareado (rama−cortar) p<0.05 (significance.py). En la matriz por hora, «mejor alt. −N$» = cuánto pierde vs cortar la mejor de las otras ramas (cortar domina por N$). Sin comisiones; unidades fraccionales.
- 356 eventos cruzan en el MISMO minuto de la entrada: ahí el drawdown es en gran parte el costo del spread inicial (se compra al ask y se marca al bid), no un movimiento adverso — pesan sobre todo en leg −20/−30%.

## Disparador COMBINADO (la posición completa pierde X%)

| X% | n | EV hold | EV reforzar | EV contra | EV cortar | win hold | win ref | win contra |
|---|---|---|---|---|---|---|---|---|
| −20% | 334 | -300$ (-30.0%) | -442$ (-29.5%) | -335$ (-22.3%) | -238$ (-23.8%) | 25% | 20% | 24% |
| −30% | 327 | -352$ (-35.2%) | -567$ (-37.8%) | -322$ (-21.4%) | -330$ (-33.0%) | 25% | 20%✱ | 24% |
| −40% | 309 | -475$ (-47.5%) | -700$ (-46.7%) | -482$ (-32.1%) | -425$ (-42.5%) | 23% | 17%✱ | 24% |
| −50% | 285 | -639$ (-63.9%) | -856$ (-57.0%) | -775$ (-51.7%) | -519$ (-51.9%) | 19%✱ | 13%✱ | 19%✱ |
| −60% | 264 | -740$ (-74.0%) | -969$ (-64.6%) | -929$ (-62.0%) | -616$ (-61.6%) | 15%✱ | 11%✱ | 15%✱ |

### Matriz de decisión por hora del cruce (mejor rama por EV$)

| X% | 09:30-10:30 | 10:30-12:00 | 12:00-13:30 | 13:30-15:55 |
|---|---|---|---|---|
| −20% | **Cortar** (mejor alt. -76$) · n=279 | **Contra** +358$ vs cortar (p=0.516) · n=32 | **Contra** +185$ vs cortar (p=0.772) · n=8 | **Cortar** (mejor alt. -257$) · n=15 |
| −30% | **Cortar** (mejor alt. -130$) · n=224 | **Contra** +837$ vs cortar (p=0.077) · n=70 | **Contra** +559$ vs cortar (p=0.322) · n=11 | **Cortar** (mejor alt. -295$) · n=22 |
| −40% | **Cortar** (mejor alt. -109$) · n=155 | **Contra** +72$ vs cortar (p=0.791) · n=105 | **Contra** +725$ vs cortar (p=0.387) · n=19 | **Cortar** (mejor alt. -128$) · n=30 |
| −50% | **Cortar** (mejor alt. -211$) · n=87 | **Cortar** (mejor alt. -69$) · n=128 | **Cortar** (mejor alt. -73$) · n=34 | **Cortar** (mejor alt. -124$) · n=36 |
| −60% | **Cortar** (mejor alt. -32$) · n=35 | **Cortar** (mejor alt. -119$) · n=128 | **Cortar** (mejor alt. -89$) · n=50 | **Cortar** (mejor alt. -136$) · n=51 |

## Disparador POR PIERNA (una pierna pierde X% de su capital)

| X% | n | EV hold | EV reforzar | EV contra | EV cortar | win hold | win ref | win contra |
|---|---|---|---|---|---|---|---|---|
| −20% | 363 | -70$ (-7.0%) | -42$ (-2.8%) | -185$ (-12.4%) | -116$ (-11.6%) | 13% | 13% | 24% |
| −30% | 363 | -70$ (-7.0%) | -75$ (-5.0%) | -167$ (-11.1%) | -89$ (-8.9%) | 12% | 12% | 25% |
| −40% | 363 | -70$ (-7.0%) | -148$ (-9.9%) | -132$ (-8.8%) | -41$ (-4.1%) | 11% | 11% | 26% |
| −50% | 363 | -70$ (-7.0%) | -206$ (-13.7%) | -114$ (-7.6%) | -17$ (-1.7%) | 9% | 9% | 26% |
| −60% | 363 | -70$ (-7.0%) | -253$ (-16.9%) | -125$ (-8.4%) | -12$ (-1.2%) | 6% | 6%✱ | 25%✱ |

### Matriz de decisión por hora del cruce (mejor rama por EV$)

| X% | 09:30-10:30 | 10:30-12:00 | 12:00-13:30 | 13:30-15:55 |
|---|---|---|---|---|
| −20% | **Reforzar** +75$ vs cortar (p=0.664) · n=363 | — | — | — |
| −30% | **Aguantar** +18$ vs cortar (p=0.776) · n=363 | — | — | — |
| −40% | **Cortar** (mejor alt. -25$) · n=357 | **Cortar** (mejor alt. -31$) · n=6 | — | — |
| −50% | **Cortar** (mejor alt. -50$) · n=346 | **Contra** +169$ vs cortar (p=0.724) · n=17 | — | — |
| −60% | **Cortar** (mejor alt. -47$) · n=319 | **Cortar** (mejor alt. -143$) · n=43 | — | **Cortar** (mejor alt. -157$) · n=1 |

## Día de la semana (todas las horas)

| Nivel | X% | Lun | Mar | Mié | Jue | Vie |
|---|---|---|---|---|---|---|
| combined | −20% | Cortar (n=64) | Cortar (n=68) | Cortar (n=66) | Contra (n=68) | Contra (n=68) |
| combined | −30% | Contra (n=64) | Cortar (n=67) | Cortar (n=66) | Contra (n=64) | Contra (n=66) |
| combined | −40% | Contra (n=57) | Contra (n=63) | Cortar (n=66) | Contra (n=61) | Contra (n=62) |
| combined | −50% | Cortar (n=51) | Reforzar (n=61) | Cortar (n=64) | Cortar (n=53) | Cortar (n=56) |
| combined | −60% | Cortar (n=48) | Reforzar (n=54) | Cortar (n=57) | Cortar (n=51) | Cortar (n=54) |
| leg | −20% | Cortar (n=68) | Cortar (n=76) | Cortar (n=75) | Reforzar (n=74) | Reforzar (n=70) |
| leg | −30% | Cortar (n=68) | Reforzar (n=76) | Cortar (n=75) | Contra (n=74) | Reforzar (n=70) |
| leg | −40% | Cortar (n=68) | Cortar (n=76) | Cortar (n=75) | Contra (n=74) | Aguantar (n=70) |
| leg | −50% | Aguantar (n=68) | Contra (n=76) | Cortar (n=75) | Contra (n=74) | Cortar (n=70) |
| leg | −60% | Cortar (n=68) | Cortar (n=76) | Cortar (n=75) | Reforzar (n=74) | Cortar (n=70) |

## Resumen ejecutivo

**1. Cortar domina o empata en casi todos los estados, y su ventaja es SIGNIFICATIVA en
drawdowns profundos.** En el nivel combinado a −50%: cortar deja EV −519$ vs aguantar −639$
(+120$/evento, p=0.021), reforzar −856$ (+337$, p=0.002) y contra −775$ (+256$, p=0.024).
A −60% lo mismo, más fuerte (p=0.004 / 0.005 / 0.0008). Por pierna a −60%: cortar la pierna
deja −12$ vs aguantar −70$ (p=0.053), reforzar −253$ (p=0.022) y contra −125$ (p=0.039).
Una vez que la posición ya perdió la mitad, agregar dinero (de cualquier lado) o esperar es
sistemáticamente peor que aceptar la pérdida.

**2. Reforzar (la martingala del engine) no muestra ventaja en ningún estado con muestra
grande.** En el nivel combinado es significativamente peor que cortar desde −30%
(−237$/evento, p=0.043; a −40%: −275$, p=0.010). En su punto de disparo actual (pierna a
−40%) rinde −148$ vs −41$ de cortar la pierna (delta −107$, p=0.45 — no significativo, pero
con win-rate 11%: pierde el head-to-head ~9 de cada 10 veces y lo rescatan pocos días
grandes; el déficit crece monótonamente con la profundidad: −189$ a −50%, −241$ a −60%
p=0.022). El único estado donde tiene el mejor EV es pierna −20% (+75$ vs cortar, p=0.66),
justo el umbral más contaminado por cruces de puro spread del minuto de entrada.

**3. Comprar en contra solo luce en cruces combinados de media mañana.** En 10:30–13:30 con
−20/−30/−40%: EV entre +72$ y +837$ sobre cortar (el mejor caso: −30% en 10:30–12:00,
+837$, n=70, p=0.077). Nunca alcanza p<0.05, los n son chicos y el EV viene de colas gordas
(reversiones violentas de tendencia). Es una HIPÓTESIS para re-testear fuera de esta
ventana, no una ventaja mecanizable. En cruces tempranos (09:30–10:30, la mayoría) y en
drawdowns profundos, contra es neutra o destructiva.

**4. Aguantar nunca es la mejor rama con respaldo.** Pierde vs cortar en todo el nivel
combinado y solo empata/gana marginal en pierna −20/−30% (+46$/+18$, p≥0.53), donde el
«drawdown» es en gran parte el costo del spread inicial, no un movimiento adverso.

En una línea: **con la posición combinada perdiendo ≥40–50%, cortar; con una pierna
perdiendo ≥40%, cortar esa pierna; no reforzar; «contra» solo como hipótesis de media
mañana pendiente de validación.**

### Propuesta de mecánica — IMPLEMENTADA (2026-07-06)

- **Corte POR PIERNA (nuevo — implementado)**: parámetro opcional `leg_stop_loss_pct` en el
  engine (`run_next_iteration`/`_run_one_iteration`): en modos de dos piernas con salida
  conjunta (both / both_plus / call_or_put / call_or_put_eod), la pierna cuyo ROI toca −X%
  se vende ese minuto (al bid: Fase 2 usa la serie bid; Fase 1 pide el bid puntual) y su
  valor queda CONGELADO — mismo patrón que «Until reach ROI(%)». La posición sigue su salida
  normal sobre la serie congelada. No aplica en single-leg (el Stop loss normal cubre), en
  modos con gestión por pierna propia (plus/until_roi) ni con refuerzo (semántica opuesta).
  Expuesto en `run_one` como `leg_stop_pct` (en %, tolerante al signo; None = off) con motivo
  de salida por pierna `leg_stop_loss` («Stop por pierna» en REASON). **Variable de
  combinación**: columna opcional «Stop por pierna (%) (escenario)» (COND_COLS_OPT en
  combinations.py + map_scenario), con el mismo gate de «Alcance de salida» que el stop del
  ticker; Excel viejos sin la columna siguen válidos. ENGINE_VERSION subido a 2026-07-06.
  Cobertura: tests/test_leg_stop.py (7 casos, congelamiento/triggers/limpieza de marca/
  refuerzo/single-leg/Fase 1) + validación E2E en cache real (QQQ 2026-01-05: −1,000$ →
  −523$ con leg-stop −50, doble corte 09:32 y 10:23).
- **Stop combinado profundo (−50%)** — ya era mecánico («Stop loss (%) del ticker»,
  `stop_loss_pct` sobre el ROI total). Pendiente de validación: barrerlo ∈ {−40, −50, −60}
  como variable de combinación sobre 2022→2025 antes de adoptar.
- **Refuerzo (martingala)**: correr la comparación refuerzo ON vs OFF como variable («Tipo
  de operación (escenario)»: CALL Y PUT vs CALL Y PUT (REFUERZO)) sobre la historia completa;
  si el patrón de 2026 se sostiene, considerar default OFF o subir el umbral de disparo.
- **Contra**: no mecanizado (p≥0.077, sin respaldo). Para seguir la pista: re-correr este
  mismo script sobre el cache 2022→2025 (`--desde 2022-06-01 --hasta 2025-12-31`, ya
  cacheado por el walk-forward) y mirar el bucket 10:30–13:30 del nivel combinado.

**Barrido sugerido para el próximo batch** (todo ya soportado): «Stop por pierna (%)
(escenario)» ∈ {vacío, 40, 50, 60} × «Cerrar si Stop loss ticker»/«Stop loss (%) del
ticker» ∈ {No, 50, 60} × «Tipo de operación (escenario)» ∈ {CALL Y PUT, CALL Y PUT
(REFUERZO)} sobre 2022→2025 — valida los tres puntos de una sola corrida.

### Advertencias de la casa
- **Una sola ventana temporal (ene→jul 2026) = in-sample**: no hay validación fuera de muestra; los resultados describen ESTE régimen de mercado.
- **Muestras chicas por celda** (sobre todo buckets tardíos y umbrales profundos): celdas con n<8 no tienen test; n<20 es indicativo, no concluyente.
- **Sesgo de selección de umbral**: los eventos de distintos X del mismo día están anidados (un día muy malo dispara 20→60) y los 3 tickers están correlacionados → los p-values son OPTIMISTAS (caveat estándar de significance.py). Úsalos como guía, no como prueba.
- Unidades fraccionales y sin comisiones (convención del engine); montos fijos $500 por acción de refuerzo/contra.