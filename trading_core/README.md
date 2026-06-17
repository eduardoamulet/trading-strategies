# trading_core — lógica de trading desacoplada del proveedor (ports & adapters)

Capa de negocio **reusable en backtesting, paper trading y vivo** sin cambiar la lógica.
La fuente de datos y el broker son detalles enchufables; la estrategia no los conoce.

## Por qué (el problema que resuelve)

Antes había **dos stacks paralelos**: el backtest (`options_replay/engine.py` acoplado a
`Downloader`/Polygon) y el vivo (`live_trader/` con su propio broker). La misma decisión
podía divergir entre ambos. `trading_core` unifica la lógica detrás de **puertos**: una
sola implementación de selección/ejecución/decisión corre en los tres entornos.

## Arquitectura (hexagonal / ports & adapters)

```
            ┌──────────────────────────────────────────────┐
            │                  DOMINIO (puro)                │
            │   selection.py · execution.py · strategy_core  │  ← reglas de negocio
            │        depende SOLO de los puertos ↓           │     (sin I/O, sin proveedor)
            └───────────────┬───────────────┬────────────────┘
              puerto MarketData         puerto Broker / Clock
                    ▲                          ▲
        ┌───────────┴──────────┐   ┌───────────┴───────────────┐
        │  ADAPTERS (concretos)│   │  ADAPTERS (concretos)     │
        │  PolygonBacktestData │   │  SimulatedBroker (sim)    │  ← backtest / paper
        │  TradierMarketData * │   │  TradierBroker *  (vivo)  │  ← producción
        │  SchwabMarketData  * │   │  SchwabBroker  *  (vivo)  │
        └──────────────────────┘   └───────────────────────────┘
                      (* = a implementar; el dominio NO cambia)
```

**Regla de dependencia (la flecha apunta hacia adentro):** los adapters dependen del
dominio, nunca al revés. `domain.py` y `ports.py` no importan nada de proveedores.

### Piezas

| Archivo | Responsabilidad (SRP) |
|---|---|
| `domain.py` | Value objects: `Contract`, `Quote`, `OrderRequest`, `Fill`, `Leg`, `TradeResult`. |
| `ports.py` | Interfaces `MarketData`, `Broker`, `Clock` (lo que el dominio necesita). |
| `selection.py` | Selección "Opción 1" + **ventana de búsqueda**; compuerta de spread **inyectable**. |
| `execution.py` | `run_straddle`: abrir → gestionar (marca al bid) → cerrar; decide con `strategy_core`. |
| `adapters/polygon_backtest.py` | `MarketData` sobre el `Downloader` (parquet de Polygon). |
| `adapters/simulated_broker.py` | `Broker` que llena al ask (compra) / bid (venta) = fills Fase 2. |
| `adapters/clocks.py` | `BacktestClock` (grilla de minutos). |

La unificación clave: **todo dato es AS-OF un instante `at`**. Backtest → `at` es el tiempo
simulado (resuelve el parquet de ese minuto); vivo → el adapter devuelve lo último real.

## Uso

```python
from trading_core import run_straddle, SelectionParams, make_range_gate
from trading_core.adapters import PolygonBacktestData, SimulatedBroker, BacktestClock

market = PolygonBacktestData(downloader)        # ← cambiar este adapter = cambiar de entorno
broker = SimulatedBroker(market)                #   (vivo: TradierBroker, etc.)
clock  = BacktestClock(step_min=1)

res = run_straddle(
    market, broker, clock,
    ticker="IWM", expiry="2026-06-04",
    entry_ts=entry, session_end=eod,
    invest_call=1000, invest_put=1000, umbral_pct=10, stop_pct=-100,
    params=SelectionParams(premium_min=0.25, premium_max=1.00, window_min=5),
    gate=make_range_gate(0.25, 1.00, max_spread=0.05))
print(res.entry_ts, res.exit_reason, res.roi)
```

Para **paper/vivo**, lo único que cambia son las tres primeras líneas (los adapters). La
llamada a `run_straddle` es idéntica.

## Cómo agregar un proveedor (extensibilidad — la 'O' de SOLID)

1. Implementá `MarketData` (`underlying_price`, `chain`, `quote`, `nearest_expiry`) sobre
   la API del proveedor (ej. Tradier REST en tiempo real).
2. Implementá `Broker` (`execute`) — o reusá el `BrokerAdapter` ya existente en
   `live_trader/brokers/` con un wrapper fino.
3. Inyectalos. **No se toca `selection.py` ni `execution.py`.**

## Testeabilidad

`tests/test_decoupling.py` corre `run_straddle` completo con un `FakeMarketData` en memoria
(sin Polygon, sin red): demuestra que el dominio es testeable de forma aislada y verifica la
ventana de búsqueda + el take-profit de forma determinista.

```
python trading_core/tests/test_decoupling.py
```

## Estado y plan de migración (por fases, sin romper lo que anda)

- ✅ **Fase A (hecha):** puertos + dominio (selección con ventana + ejecución straddle) +
  adapters de backtest (`PolygonBacktestData`, `SimulatedBroker`) + test de desacople.
  El `options_replay/engine.py` actual sigue intacto y funcionando.
- ✅ **Fase B (hecha):** toda la familia de modos intradía sobre los puertos —
  `run_refuerzo` (martingala por pierna), `run_single` (Sólo CALL/PUT), `run_both_plus`
  (salida forzada por hora), `run_call_or_put` (sale al +100% de cualquier pierna),
  `run_call_or_put_plus` (banca la 1ª, recupera con la 2ª), y **Opción 2** (`criterion=
  'itm_first'`: elige el 1-ITM ignorando compuerta y rango). `Position` multi-tranche;
  `run_straddle` = `run_refuerzo(refuerzo_max=0)`. Tests: `test_refuerzo.py`,
  `test_variants.py`. Paridad verificada contra el motor (refuerzo TSLA: +32.0% vs +32.9%).
  _Único pendiente:_ **Opción 3** (overnight 1DTE) — es otra estructura de tiempo (compra
  hoy, vende al vencimiento del día siguiente), no un modo intradía; va con Fase C/D.
- ✅ **Fase C (hecha):** `TradierMarketData` + `TradierBroker` (`adapters/tradier_live.py`)
  envuelven el broker de Tradier de `live_trader` (inyectado, duck-typed) y traducen sus
  modelos a los del dominio. `LiveClock` (ticks en tiempo real). Test `test_tradier_adapter.py`:
  el MISMO `run_refuerzo` corre por los adapters de Tradier contra un broker FALSO en memoria
  (sin red ni credenciales) → take_profit. Migrar de backtest a paper = inyectar estos adapters.
- ✅ **Fase D (hecha — integración):** `live_runner.py` corre la MISMA `run_refuerzo` en VIVO
  contra Tradier **SANDBOX** (`run_paper_refuerzo`), cableando los adapters reales. Guard de
  seguridad `assert_sandbox`: se NIEGA a correr si `LIVE_TRADING_ENABLED=True` (probado).
  Sandbox/paper únicamente; el token va en `live_trader/secrets.py` (no acá). _Limpieza
  opcional restante:_ unificar `live_trader/core/models.py` en `trading_core/domain.py` (hoy
  los adapters ya traducen entre ambos, así que NO es bloqueante).

## Correr en paper (Tradier sandbox)

Con el token de sandbox en `live_trader/secrets.py` y `LIVE_TRADING_ENABLED=False`:

```python
from trading_core.live_runner import run_paper_refuerzo
res = run_paper_refuerzo(ticker="QQQ", invest_call=1000, invest_put=1000,
                         umbral_pct=10, stop_pct=-100, refuerzo_max=0, max_spread=0.05)
```
El runner se niega a arrancar si el modo real está habilitado. No mueve dinero real.
