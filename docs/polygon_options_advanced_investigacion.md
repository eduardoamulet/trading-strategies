# Polygon Options Advanced — Plan de explotación para backtesting de compra de opciones 0-1DTE

**Fecha**: 2026-07-04 · **Autor**: análisis para SignalForge · **Plan**: Options Advanced ($199/mes)
**Alcance**: solo COMPRA de CALL/PUT (sin ventas ni spreads), 0DTE/1DTE, sobre SPY, SPX, QQQ
(+ IWM, DIA, NVDA, TSLA, AMD, COIN, META, AAPL ocasionales).

---

## 0. Qué incluye tu plan (leído de las capturas) y qué NO aparece

**Confirmado en las imágenes (Options Advanced):**

| Capacidad | Estado | Nota clave |
|---|---|---|
| All US Options Tickers | ✓ | cobertura 100 % del mercado |
| **API Calls ilimitadas** | ✓ | el límite de 600/min de nuestro runner era autoimpuesto (ya subido a 6.000) |
| Históricos **5+ años** | ✓ | nos interesa 6 meses → sobra margen |
| **Real-time** (sin delay de 15 min) | ✓ | irrelevante para backtesting; oro para trading en vivo futuro |
| Reference Data / Corporate Actions | ✓ | contratos, strikes, vencimientos, splits |
| Technical Indicators | ✓ | poco valor (los calculamos localmente mejor) |
| Aggregates de **minuto** y **segundo** | ✓ | minuto ya lo usamos; segundo es nuevo |
| **Greeks, IV & Open Interest** | ✓ | vía **Snapshot** (ver la verdad incómoda, §2) |
| **Flat Files** | ✓ | descargas masivas (S3) — la joya operativa desaprovechada |
| WebSockets: minute/second aggs, **Trades**, **Quotes** | ✓ | Quotes por WebSocket es EXCLUSIVO de Advanced |
| REST **Trades** históricos | ✓ | cada print ejecutado, con condiciones |
| REST **Quotes** históricos (NBBO tick a tick) | ✓ | EXCLUSIVO de Advanced — ya lo usamos a nivel minuto |
| Snapshot (cadena completa) | ✓ | greeks/IV/OI **actuales** de todos los strikes |
| Uso | Individual, non-pros | sin implicancia técnica |

**NO aparece en las imágenes (y hay que decirlo explícitamente):**
- La **granularidad histórica de Greeks/IV/OI**: las capturas confirman el dato como *capability*,
  pero no dicen si existe una SERIE histórica por REST. A mi conocimiento del producto, **no
  existe endpoint de greeks/IV históricos**: el Snapshot da el estado ACTUAL, y el Open Interest
  es un dato diario (D-1). Verificar contra la documentación vigente antes de asumir lo
  contrario — el diseño de §2 se hace cargo de este vacío.
- Límites de concurrencia HTTP prácticos (no publicados; "unlimited" ≠ latencia cero).
- Profundidad de libro (level 2): las opciones de Polygon son NBBO — **no hay book depth**.

---

## 1. Mapa capacidad → componente de SignalForge

| Dato Polygon | Qué entrega | Backtesting | Selector de contrato | Market Direction | Playbook |
|---|---|---|---|---|---|
| Aggs 1-min opción (ya en uso) | OHLCV por contrato/minuto | base actual de fills por barra | prima de entrada | — | base actual |
| **Quotes NBBO tick** | bid/ask con timestamp exacto | fills realistas al tick; spread efectivo en la ventana de entrada | spread real al momento exacto | microestructura (widening) | spread% por posición |
| **Trades** | prints: precio, size, condiciones, exchange | validar que el fill asumido ERA alcanzable (hubo prints); slippage empírico | liquidez REAL del strike (prints/min) | **flujo**: sweeps, blocks, PCR intradía, delta-volume | volumen del contrato elegido |
| **Snapshot cadena** | greeks, IV, OI, day stats de TODOS los strikes AHORA | — (es presente) | delta/IV/OI del candidato EN VIVO (futuro live) | GEX del chain, skew | — |
| Snapshot **capturado a diario** (nuestro) | serie histórica PROPIA de greeks/IV/OI | features por fecha | percentiles históricos de IV por strike | GEX/skew históricos | régimen de vol por día |
| Aggs 1-seg | OHLCV por segundo | timing de entrada sub-minuto (0DTE: 09:30-09:35 se mueve por segundos) | — | confirmaciones más finas | — |
| **Flat Files** | archivos día completos (aggs/trades/quotes) | **backfills masivos sin loops REST** (el cuello de hoy) | — | — | — |
| Reference/contracts | universo de strikes/vencimientos por fecha | reconstrucción exacta del chain histórico | validar 1-ITM histórico | — | — |
| WebSockets (aggs/trades/quotes RT) | streaming | — | — | — (research) | — |
| Technical indicators API | SMA/EMA/RSI server-side | **no usar** (los nuestros son causales y testeados) | — | no | no |

---

## 2. La verdad incómoda: greeks/IV/OI históricos NO se descargan — se construyen

Este es el punto que separa un sistema amateur de uno serio, y donde tu plan da ventaja:

1. **Empezar HOY a capturar el Snapshot de la cadena** (1-2 veces por día: 09:35 y 15:45) para
   los ~10 subyacentes. Costo: minutos y megabytes. En 3 meses tenés una base de greeks/IV/OI
   **que no se puede comprar retroactivamente** — cada día que pasa sin capturar es un día
   perdido para siempre. (El OI es D-1: la captura de la mañana es la buena.)
2. **Derivar IV y greeks históricos localmente** para los 6 meses pasados: con el NBBO mid del
   contrato (ya lo tenemos por minuto), el spot del subyacente (lo tenemos), r (tasa) y el
   tiempo al vencimiento → **Black-Scholes inverso** da IV; de ahí delta/gamma/theta/vega
   analíticos. Para 0DTE americanas sobre ETFs, BS europeo es aproximación aceptable para
   RANKING (no para pricing fino — sesgo conocido y documentable). Validación: comparar los
   derivados de hoy contra el Snapshot de hoy (error tolerable → los históricos son creíbles).
3. **OI histórico**: no reconstruible hacia atrás con exactitud. Para 0DTE importa poco (el OI
   de un 0DTE nace ese día); para 1DTE, la captura diaria del punto 1 lo resuelve de acá en más.

---

## 3. Datos de los últimos 6 meses: qué bajar, cómo y cuánto pesa

| Dataset | Método recomendado | Volumen estimado (10 tickers, 6 meses) | Valor |
|---|---|---|---|
| Aggs 1-min opciones (contratos tocados) | ya lo tenemos (REST on-demand + cache) | ya en disco | base |
| **Aggs 1-min por Flat Files (día completo)** | S3 flat files → filtrar nuestros 10 subyacentes → parquet | ~1-3 GB/mes filtrado | mata el cuello de backfill: un archivo por día en vez de miles de requests |
| **Quotes NBBO** | selectivo: solo contratos candidatos (±15 strikes de ATM, DTE≤1) desde flat files | ~5-15 GB/mes filtrado (los quotes son lo más pesado) | fills al tick + spread efectivo |
| **Trades opciones** | flat files filtrados igual | ~1-2 GB/mes | slippage empírico + señales de flujo |
| Subyacente 1-min (ya) + **1-seg (nuevo, solo 09:28-09:40 y 15:50-16:00)** | REST | cientos de MB | timing fino 0DTE |
| Snapshot cadena (desde hoy) | REST 2×/día | ~5-20 MB/día | la base de greeks/IV/OI propia |
| Reference contracts por fecha | REST 1×/día | trivial | chain exacto histórico |

**Regla de decisión**: REST para lo incremental diario (pocos contratos nuevos), **Flat Files
para cualquier backfill** (rangos largos o combinaciones nuevas que sondean strikes no
cacheados — exactamente lo que hoy tardaba 37 min por tanda).

---

## 4. Backtesting profesional con estos datos (evolución del actual)

Lo que ya está bien: Fase 2 (NBBO por barra, triggers sobre el bid), cascada de rango de prima
por ASK, compuerta de spread por bucket, verify al centavo, almacén incremental segregado.

Mejoras en orden de realismo ganado por esfuerzo:

1. **Fill al tick en la ventana de entrada** (Quotes tick): hoy la entrada usa el NBBO del
   minuto; con ticks, el fill es el ask VIGENTE en el segundo exacto + regla de mejora (si el
   spread se cierra en los siguientes N segundos, ¿tu límite hubiera llenado?). Para 0DTE a las
   09:30-09:33 la diferencia por minuto vs por segundo es material (la subasta se acomoda).
2. **Validación por Trades**: un fill simulado solo es creíble si HUBO prints en ese contrato
   dentro de ±X segundos y a precio ≤ tu ask asumido. Columna nueva: `fill_validado` (bool) y
   `slippage_empirico` (diferencia vs el print real más cercano). Reglas de exclusión para el
   Playbook: posiciones no-validadas no votan.
3. **Slippage medido, no asumido**: distribución del *effective spread* (print vs mid) por
   ticker × hora × moneyness, calculada de los trades históricos → el backtest cobra el
   slippage de esa distribución (p50 conservador: p75).
4. **Liquidez como filtro duro**: prints/min y quote-updates/min mínimos en la ventana de
   entrada (además del spread). Un strike "barato" sin prints es una trampa que el backtest
   actual no ve.
5. **Salidas con la misma vara**: el trailing/stop sobre bid por barra (ya) puede refinarse a
   bid tick en la ventana de salida (13:55) — mismo patrón que la entrada.

---

## 5. Selección de contrato: de "premium range + 1-ITM" a scoring multifactor

Variables disponibles y su papel (para COMPRA 0-1DTE):

| Variable | Fuente | Papel en el selector | Comentario |
|---|---|---|---|
| **Delta** | derivado (§2) / snapshot | LA variable de diseño: delta objetivo ≈ probabilidad ITM y apalancamiento; reemplaza al proxy "rango de prima" | un rango de prima de $0.30-0.60 es en realidad un rango de delta implícito que DERIVA con el spot — delta directo es más estable entre días |
| **Spread %** (ask-bid)/mid | NBBO (ya) | filtro duro + desempate (ya) | pasar de $ absolutos a % del mid normaliza entre primas |
| **IV del strike** | derivada | contexto: comprar theta caro vs barato; feature del Playbook | percentil IV propio (captura diaria) > IV absoluta |
| **Gamma/Theta ratio** | derivados | para 0DTE: cuánta convexidad comprás por unidad de sangría | candidato a factor de ranking nuevo |
| **Volume del contrato (día)** | aggs/trades | filtro de liquidez mínima | complementa prints/min |
| **Open Interest** | snapshot diario | 1DTE sí; 0DTE casi irrelevante (nace ese día) | no sobreponderar |
| **Prints/min en ventana** | trades | filtro de ejecutabilidad (§4.4) | nuevo |
| Time-of-day | reloj | el theta de un 0DTE no es lineal: la tarde quema distinto | interactúa con hora de entrada del Playbook |

**Propuesta concreta**: selector `"delta_target"` (p.ej. delta 0.45-0.55 = ATM; 0.55-0.70 =
lig. ITM) con desempate por (spread%, prints/min) y fallback al 1-ITM actual. Backtesteable
contra los tres criterios existentes con el mismo verify — es una extensión natural del
selector compuesto que ya agregamos.

---

## 6. Market Direction Engine: señales del mercado de OPCIONES

¿Puede el flujo de opciones anticipar al subyacente? Para 0DTE intradía, la evidencia práctica
dice: *a veces, y medible*. Señales por valor esperado:

| Señal | Datos | Valor esperado para 0DTE | Costo |
|---|---|---|---|
| **PCR intradía por volumen** (primeros 15-30 min, calls vs puts del día) | trades/aggs | alto: sesgo direccional de la manada temprana | bajo |
| **Delta-volume imbalance** (Σ delta×size de prints, signo por lado del NBBO) | trades + NBBO + delta derivado | alto: presión direccional REAL comprada | medio |
| **GEX (gamma exposure) del chain** | OI (captura diaria) + gamma derivado | medio-alto: régimen pin/expansión — con GEX positivo el subyacente tiende a mean-revert (malo para compra direccional); negativo = movimientos amplios | medio |
| **IV crush/spike intradía** | IV derivada por minuto | medio: spikes de IV antes de movimientos; crush post-apertura define si conviene entrar 09:30 o 09:45 | medio |
| **Sweeps/blocks** (condiciones de trade + multi-exchange en ms) | trades con conditions | medio: convicción institucional; ruidoso en ETFs | alto (parsing fino) |
| Widening de spreads del chain | quotes | bajo-medio: proxy de nerviosismo pre-movimiento | bajo |
| Cambios de OI intradía | — | **no disponible** (OI es diario) — descartar | — |

Integración: cada señal entra al MDE como feature CAUSAL (solo datos ≤ start_time), se cachea
en `md_cache` (ya versionado con `MD_ENGINE_VERSION`), y su aporte se valida con la
infraestructura que YA existe: el Playbook cruza `md_*` contra ROI por posición.

---

## 7. Playbook: qué columnas nuevas almacenar por posición

El almacén ya guarda md_action/score/confianza/trend + snapshot del contrato (strike, bid/ask,
spread, primas, greeks del contrato si están). Agregar por posición al momento de ENTRADA:

- `iv_entrada`, `iv_percentil_20d` (derivadas) — régimen de vol del strike.
- `delta_entrada`, `gamma_entrada`, `theta_entrada` (derivados — hoy esas columnas existen pero
  dependen del snapshot; pasarían a poblarse siempre).
- `spread_pct_entrada`, `prints_min_entrada`, `fill_validado`, `slippage_empirico` (§4).
- `pcr_15m`, `delta_vol_imbalance_15m`, `gex_apertura` (§6) — las tres señales de flujo.
- `vix_open` o proxy (ya hay régimen vol SPY — mantener).

Con eso el Playbook puede descubrir cortes como «los martes solo operar si GEX<0 y PCR<0.8» —
patrones por régimen, no solo por día de la semana. La maquinaria (ventana 120d, decaimiento,
gates, gate P5(Beta), gemelos campeón/retador) ya existe y es agnóstica a las features.

---

## 8. Variables a almacenar — clasificación

**Críticas** (sin esto el backtest miente): NBBO bid/ask en entrada y salida (ya) ·
spread% (ya en $) · prima de entrada real (ya) · timestamp exacto de fill (ya por barra;
mejorar a tick) · resultado y motivo de salida (ya).

**Muy importantes** (ventaja competitiva): delta/IV derivadas de entrada · prints/min +
fill_validado + slippage_empirico · captura diaria del Snapshot (greeks/IV/OI del chain) ·
PCR-15m y delta-volume imbalance · GEX de apertura.

**Importantes** (contexto/robustez): gamma/theta de entrada · percentil IV 20d · volumen del
contrato · aggs 1-seg de las ventanas 09:28-09:40/15:50-16:00 · reference del chain por fecha.

**Opcionales** (medir antes de invertir): sweeps/blocks clasificados · widening del chain ·
OI para 1DTE · second aggs fuera de las ventanas · technical indicators de Polygon (nunca).

---

## 9. Arquitectura de datos

```
capa 0  RAW (frío, comprimido zstd, inmutable)
        flat files filtrados por nuestros 10 subyacentes: trades/, quotes/, aggs/
        → se puede REGENERAR todo lo de abajo desde acá; borrable solo esto si aprieta el disco
capa 1  CURADO (parquet por ticker-mes — no por ticker-día: hoy 1,2 M archivitos, malo para
        scans/backups; consolidar a ~200 archivos/mes)
        opciones_1min/, quotes_1min/ (ya existen conceptualmente), trades_ventanas/
capa 2  DERIVADOS (recalculables, versionados con ENGINE_VERSION/MD_ENGINE_VERSION)
        iv_greeks_1min.parquet · flujo_diario.parquet (PCR, imbalance, GEX) · slippage_dist.parquet
capa 3  ALMACENES SQLite (ya): bt_results.db (+reeval_runs) · md_cache.db · playbook.json
        + NUEVO: chain_snapshots.db (captura diaria §2.1 — APPEND-ONLY, nunca borrar)
```

- **Precalcular**: derivados de capa 2 en el job de las 05:00 (solo día nuevo, incremental).
- **Recalcular bajo demanda**: agregaciones del Playbook (ya son ms).
- **Cachear**: md_cache (ya), quotes de candidatos por día (ya vía MemoDownloader).
- **Comprimir**: capa 0 zstd; parquet con compresión por defecto.
- **Eliminar**: resultados xlsx viejos de resultados/ (ya no son el almacén); capa 0 >12 meses.
- **Backup**: bt_results.db + chain_snapshots.db entran al zip semanal existente (son los
  únicos NO regenerables — el snapshot capturado es historia irrecuperable).

---

## 10. 25+ investigaciones concretas (si yo dirigiera el research con este plan)

Formato: **hipótesis** · datos · métrica de éxito. (B=Backtesting, P=Playbook, M=MDE, S=Selector)

1. **Delta óptimo por día-semana**: el delta de entrada explica más ROI que el rango de prima. Derivadas+almacén · ROI/Sharpe por bucket de delta · S,P — **prioridad 1**
2. **Spread% como predictor de ROI neto**: umbral de spread% (no $) que maximiza neto. NBBO · curva ROI vs spread% · S,B — **1**
3. **Slippage empírico por hora**: cuánto cuesta REALMENTE cruzar el spread 09:30 vs 10:30. Trades+NBBO · distribución effective-spread · B — **1**
4. **Fill-validation rate**: % de fills simulados que los prints confirman; sesgo del backtest actual. Trades · % validado por escenario · B — **1**
5. **Hora de entrada óptima por régimen de vol**: 09:30 vs 09:45 vs 10:00 condicionado a IV apertura. IV derivada · ROI por hora×régimen · P
6. **IV crush post-apertura**: magnitud del crush 09:30-10:00 en 0DTE y su costo para el comprador. IV 1-min · curva IV media · P,M
7. **PCR-15m como filtro direccional**: días con PCR extremo → ¿mejor WR del lado contrario/mismo? Trades · WR condicionado · M — **1 del MDE**
8. **Delta-volume imbalance vs movimiento 10:00-13:55**: ¿el flujo temprano predice la deriva del día? Trades+delta · IC/hit-rate · M
9. **GEX y amplitud del día**: |movimiento| esperado condicionado a GEX de apertura. Snapshot acumulado · regresión rango vs GEX · M,P
10. **GEX y éxito de compra direccional**: WR de comprar opciones con GEX>0 vs <0 (hipótesis: GEX>0 mata al comprador). · P,M
11. **Gamma/theta ratio como ranking de contrato**: ¿mejor que delta puro para 0DTE? Derivadas · ROI por ranking · S
12. **Theta burn intradía no-lineal**: perfil real de decaimiento por hora en 0DTE (vs teórico). Mid 1-min · curva de decaimiento · B,P
13. **Momento óptimo de salida**: 13:55 fijo vs salida por % de theta consumido. · P
14. **Liquidez mínima operable**: prints/min mínimo bajo el cual el ROI simulado no se realiza. Trades · ROI real vs simulado por bucket · S,B
15. **Sweeps en SPY/QQQ como confirmación**: WR con/sin sweep del mismo lado en los 5 min previos. Trades conditions · uplift de WR · M
16. **Skew intradía corto**: pendiente IV (OTM put vs call) a las 09:35 como señal de dirección. IV derivada · IC · M
17. **Spread widening pre-movimiento**: ¿el chain se ensancha ANTES de moverse el spot? Quotes · lead-lag · M
18. **1-ITM vs ATM vs delta-target head-to-head**: los 3 selectores sobre la misma historia (la infraestructura de combinaciones YA lo permite). · S — **1**
19. **Persistencia del campeón**: ¿cuántos días sobrevive el escenario campeón antes de rotar? (validación del sistema de histéresis actual). Almacén · vida media · P
20. **Régimen ES/SPX overnight** (gap del futuro) como feature del MDE matutino. Underlying aggs · WR condicionado · M
21. **Efecto viernes/vencimientos**: 0DTE del viernes vs resto (OPEX effects). Almacén · ROI por tipo de día · P
22. **Tamaño del gap de apertura vs éxito CALL/PUT**: umbrales de gap que invierten el sesgo. · M,P
23. **Segundos 09:30:00-09:30:59**: ¿cuánto ROI se pierde por entrar en la barra 09:30 vs el segundo 30? Aggs 1-seg · diferencia de prima · B
24. **Costo de la subasta**: spread del minuto 1 vs minuto 5 por ticker (¿la ventana de búsqueda de 4 min es óptima?). Quotes · curva spread vs minuto · S,B
25. **IV percentil propio como gate del Playbook**: operar solo días con IV en percentil 20-80 (extremos = comportamiento anómalo). Captura diaria · WR por percentil · P
26. **Slippage de salida con stop**: cuando el stop dispara, ¿a qué bid REAL se salió? (peor caso vs simulado). Quotes tick · distribución · B
27. **NVDA/TSLA vs ETFs**: ¿el edge del Playbook (entrenado en ETFs) transfiere a single-names con spreads mayores? Almacén multi-ticker · comparación WR · P

---

## 11. Hoja de ruta priorizada

**Fase 1 — máximo impacto, implementación fácil (1-2 semanas):**
1. ✅ *HECHO HOY*: rate limit 600→6.000 (Advanced es ilimitado).
2. **Captura diaria del Snapshot del chain** (09:35 + 15:45, 10 tickers → chain_snapshots.db,
   integrada al job de las 05:00 + una tarea 15:45). *El reloj corre: cada día no capturado se
   pierde para siempre.* Beneficio: base de greeks/IV/OI propia. Complejidad: baja.
3. **Backfill por Flat Files** para combinaciones nuevas (el cuello de hoy: 37 min/tanda →
   minutos). Complejidad: media-baja (S3 + filtro + volcado al cache parquet existente).
4. **Derivación local de IV/delta** para las filas del batch (columnas ya existen, se poblarían
   siempre) + validación contra snapshot. Complejidad: media. Valor: S y P inmediato.

**Fase 2 — impacto alto, implementación media (2-4 semanas):**
5. Selector `delta_target` + head-to-head vs los 3 actuales (investigación #18/#1).
6. Slippage empírico + fill-validation por Trades (#3, #4) → el backtest deja de ser optimista.
7. Spread% en vez de $ + estudio #2 → recalibrar la compuerta.
8. Consolidación parquet ticker-mes (capa 1) — higiene que acelera todo lo demás.

**Fase 3 — investigación avanzada (1-2 meses):**
9. PCR-15m + delta-volume imbalance como features del MDE (#7, #8) con validación causal.
10. GEX diario (#9, #10) sobre la base de snapshots acumulada.
11. Estudios de timing fino con 1-seg (#23, #24) y salidas (#13, #26).

**Fase 4 — experimental:**
12. Sweeps/blocks (#15), skew intradía (#16), widening (#17), transferencia a single-names (#27).
13. Fills tick-level completos en el motor (reescritura de la simulación de ejecución).

**Qué NO hacer** (features del plan que no pagan): Technical Indicators API (inferiores a los
locales causales), WebSockets para research (solo live), second aggs fuera de ventanas
críticas, real-time para backtesting, OI intradía (no existe).

---

## 12. Criterios de éxito del programa

Cada mejora se mide con la vara que el sistema YA tiene: `--verify` al centavo para cambios de
motor; head-to-head por combinaciones (el almacén segregado permite A/B limpio); gates del
Playbook (P5 Beta, ventanas consecutivas) para que ningún «edge» in-sample entre a producción.
La regla de oro existente aplica a todo lo nuevo: **un patrón es real recién cuando sobrevive
dos ventanas consecutivas fuera de muestra.**
