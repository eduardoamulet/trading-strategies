# Especificación-contrato — Trend Reversal Up & Down BB 15m (TR-UD-15m)

> **VERSIÓN RENDIMIENTO (híbrida)** — prioriza performance sobre fidelidad a Investep (decisión del usuario 2026-06-23).
> R3 = ruptura del midpoint del **DÍA PREVIO** (no el basis BB) + R4 = apertura **dentro de las bandas** (filtro de sobre-extensión).
> Backtest: apertura OTM **PF 1.73 / +$28,280** (vs 1.18 de la versión Bollinger pura). Se descartó la volatilidad (filtro débil).
> Pine (indicador + EXPORT) y Python (`strategies/trend_reversal_bb_15m.py`) implementan esto **idénticamente**.

---

## 1. Configuración base

- **Timeframe:** 15 minutos.
- **Bollinger Bands:** `length = 20`, `mult = 2.0`, sobre `close`.
  - `basis = SMA(close, 20)` ← línea media = **el "Punto Medio" de las BB** (criterio 3)
  - `dev   = 2.0 * stdev(close, 20)` · `upper = basis+dev` · `lower = basis-dev`
- **Dos modos de detección:**
  - **Apertura** (`intraday = OFF`, default): solo la vela de apertura (9:30 ET, `isOpeningBar`); las condiciones se evalúan sobre el **`open`** (gap de apertura).
  - **Intradía** (`intraday = ON`): cualquier vela de la sesión regular; las condiciones se evalúan sobre el **`close`** (cruce), con **cooldown** entre señales.
- Direcciones independientes: `enableUp` (CALL) / `enableDown` (PUT).

---

## 2. Los 4 criterios (correspondencia 1-a-1 con Investep)

### R1 — Tendencia Previa B15m
```
basisChangePct = (basis - basis[L]) / basis[L] * 100      (L = lateralLookback = 20)
CALL: basisChangePct <= +lateralThreshold  (0.5)          → basis NO sube (bajista/lateral)
PUT:  basisChangePct >= -lateralThreshold
```

### R2 — Ruptura de la Línea de Tendencia
El precio **rompe** la trendline multi-punto (§3):
```
Apertura:  CALL = upTrendlineActive AND open  > upTrendlineNow   (el gap rompe la resistencia)
Intradía:  CALL = crossover(close, upTrendlineNow)               (el cierre cruza)
(PUT espejo: open < / crossunder dnTrendlineNow)
```

### R3 — Ruptura del Punto Medio del DÍA PREVIO
`prevMidpoint = prevLow + (prevHigh - prevLow) * 0.5` (50% del rango del día anterior, vía daily).
El precio **rompe ese midpoint**:
```
Apertura:  CALL = isOpeningBar AND open  > prevMidpoint     PUT = ... open  < prevMidpoint
Intradía:  CALL = close > prevMidpoint                      PUT = close < prevMidpoint
```
> El midpoint del día previo selecciona mejores reversiones que el basis (PF 1.73 vs 1.18). R4 (dentro de bandas) sigue siendo el filtro de calidad clave.

### R4 — Apertura DENTRO de las Bandas de Bollinger
El precio de la vela de señal está **dentro de las bandas** (filtro de sobre-extensión):
```
precio = (intraday ? close : open)
R4 = lower <= precio <= upper
```

### Señal completa
```
fullSignalUp = enableUp   AND R1up AND R2up AND R3up AND R4   [AND cooldown si intradía]
fullSignalDn = enableDown AND R1dn AND R2dn AND R3dn AND R4   [AND cooldown si intradía]
breakout = fullSignal AND NOT fullSignal[1]   (flanco)
```
Cada regla tiene toggle `useRuleN`. Cooldown intradía: `bar_index - lastSigBar >= cooldownBars` (8).

---

## 3. Trendline multi-punto (auto-detectada) — sin cambios

- Pivots (`pivothigh/low`, strength `pivotLen=3`), ventana `trendlineLookback=40`.
- **Resistencia (CALL):** ancla = pivot-high MÁS ALTO; pendiente = máx razón sobre pivots posteriores (upper hull, `<0`).
- **Soporte (PUT):** ancla = pivot-low MÁS BAJO; pendiente = mín razón (lower hull, `>0`).
- Proyección `maxBarsForward=20`; `trendlineNow = ancla + pendiente*(bar - barAncla)`.
- Gotcha Pine: iterar `for k=0 to n-1` + filtrar `bar>ancla` (NUNCA `ancla+1 to n-1`).

---

## 4. Dataset de señales (salida Python — auditoría)
```
fecha, hora_et, ticker, direccion(CALL/PUT),
open, basis, upper, lower, trendline, basisChangePct, R1, R2, R3, R4, fuente="python"
```
(Intradía: hora = CIERRE de la vela del cruce, sin lookahead; se saltea la vela 15:45.)

---

## 5. Parámetros (defaults — coinciden Pine ↔ Python)
| Parámetro | Default |
|---|---|
| bbLen / bbMult | 20 / 2.0 |
| lateralLookback / lateralThreshold | 20 / 0.5 % |
| pivotLen / trendlineLookback / maxBarsForward | 3 / 40 / 20 |
| intraday / cooldownBars | OFF / 8 |

---

## 6. v1 (documento) vs v2 (Bollinger) — para referencia
| | v1 (`.docx`, superseded) | **v2 (Investep, ACTUAL)** |
|---|---|---|
| R3 | midpoint del rango del DÍA ANTERIOR | **ruptura del BASIS (SMA20) de las BB** |
| R4 | volatilidad (rango ≥ ATR) | **apertura DENTRO de las bandas** |

Validado por backtest (Fase 4): el filtro "dentro de las bandas" (R4 v2) fue el de mejor rendimiento (PF 1.79 vs 1.26).
