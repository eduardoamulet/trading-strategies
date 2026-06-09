# live_trader — trading de opciones en vivo (SANDBOX / paper por defecto)

⚠ **Por defecto apunta a Tradier SANDBOX (paper money).** `LIVE_TRADING_ENABLED = False`
en `settings.py`. **Probá semanas en paper antes de pensar siquiera en real.**

---

## Fases del proyecto

| Fase | Qué es | Estado |
|---|---|---|
| **0** | Paper trading sobre **Tradier sandbox**: operar alertas → comprar/monitorear/vender automático | ✅ funcional |
| **1** | **Núcleo de estrategia compartido** (`strategy_core.py`, en la raíz) → backtest y live usan reglas idénticas | ✅ verificado |
| **2** | **Schwab Trader API** (thinkorswim real), OAuth2 | 🟡 andamiaje, sin verificar (esperando credenciales) |
| **3** | Live real gateado (flag + confirmación manual) | ⛔ no empezado |

---

## Arquitectura

```
Alertas (options_replay/signals_app.py)
   │  marcás "Selección" → botón "Operar en vivo (paper)" → handoff
   ▼
UI Live (ui/app.py) ──comandos──▶  SQLite store  ◀──estado + latido──  Daemon (daemon/runner.py)
  preview → confirm                 positions / audit /                 monitoreo ROI + auto-TP
  params por alerta                 commands / meta(heartbeat)          + EOD flatten (headless)
                                            │
                                     BrokerAdapter (brokers/base.py)
                                       ├─ TradierAdapter   ← paper, ACTIVO
                                       └─ SchwabBrokerAdapter ← Fase 2, place_order HARD-GATED
                                            │
                                     strategy_core.py  ← reglas compartidas con el backtest
```

**Clave:** el monitoreo de ROI y la venta automática (auto take-profit) corren en el
**daemon**, NO en la UI. Así la venta automática sigue funcionando aunque cierres el browser.

---

## Setup

1. **Token de sandbox**: cuenta en https://dashboard.tradier.com/ → Sandbox → API Access.
2. **Credenciales** (archivo gitignored):
   ```
   cp live_trader/secrets.example.py live_trader/secrets.py
   # editá secrets.py: TRADIER_SANDBOX_TOKEN y TRADIER_SANDBOX_ACCOUNT_ID
   ```
   (o exportá `TRADIER_SANDBOX_TOKEN` / `TRADIER_SANDBOX_ACCOUNT_ID` como env vars)
3. **Dependencias**: `requests`, `streamlit`, `pandas` (ya las tenés).

---

## Correr

Dos procesos, en dos terminales:

```bash
# Terminal 1 — el daemon (monitoreo + auto-TP + EOD flatten). DEBE estar corriendo.
cd live_trader
py -m daemon.runner

# Terminal 2 — la UI (o entrá por el trading_suite en el puerto 8501)
cd Traiding
py -m streamlit run live_trader/ui/app.py
```

> Si el daemon NO corre, la UI lo avisa con **🔴 Daemon NO detectado** y la venta
> automática no ocurre. Con el daemon vivo verás **🟢 Daemon activo (hace Xs)**.

---

## Flujo A — operar ALERTAS (el camino principal, paper)

1. **Alertas** (`options_replay/signals_app.py`): marcás la casilla **Selección** de una o
   más alertas → botón **"🟢 Operar en vivo (paper) → Live"** (te redirige a la página Live).
2. **Live**, panel *"Operar alertas seleccionadas"*: tabla **editable por alerta**
   (Inversión $ / Umbral ROI % / Strike atm·itm) + checkbox **🎯 Auto-armar TP**.
3. **🔍 Previsualizar (sin comprar)** → dry-run: muestra por alerta el contrato elegido,
   ask, spread, OI, cantidad, costo y **Estado**:
   - `✅ lista` — pasa selección + riesgo
   - `⚠ bloqueada: <motivos>` — p. ej. *Mercado cerrado*, *spread > máximo*, *ya hay posición*
   - `✗ error: <motivo>` — no hay contrato líquido, etc.
   **No coloca ninguna orden.**
4. **✅ Confirmar y comprar (paper)** → abre 1 posición por alerta (re-valida al ejecutar).
   Con Auto-armar TP, el take-profit queda **armado** → el daemon vende solo al Umbral.
5. **Panel de posiciones**: estado del daemon (🟢/🔴), ROI/PnL en vivo (con el **bid**),
   venta automática al Umbral, **EOD flatten** 15:55 ET, **Vender ya**, **Kill switch**.

> El puente está en `core/alert_entry.py`: `preview_from_alert` (dry-run) y
> `enter_from_alert` (compra + auto-arm) comparten `_resolve_contract` → sin drift entre
> lo que previsualizás y lo que se compra.

## Flujo B — entrada MANUAL (un contrato puntual)

1. UI: ticker + CALL/PUT + cantidad + ROI objetivo + estrategia (atm/itm) → **Analizar**.
2. Revisás strike/exp/bid/ask/spread/delta/vol/OI → validación de riesgo → **Comprar**.
3. **Activar TP** en el panel de posiciones → el daemon vende al llegar al ROI objetivo.

---

## Selección de contrato (idéntica al backtest — Fase 1)

`core/selector.py` usa el núcleo **`strategy_core.py`** (raíz del repo), el MISMO que el
backtest (`options_replay/engine.py`). Regla "Opción 1":

1. filtro de **spread ≤ máximo** del bucket de precio del subyacente,
2. **liquidez** mínima (OI ≥ 100, vol ≥ configurable),
3. orden: **menor spread → cercanía ATM/1-ITM → mayor open interest**.

La salida (Umbral de ROI / Stop) también vive en `strategy_core.exit_decision`. Hay un test
de equivalencia en `Traiding/test_strategy_core.py` (`py test_strategy_core.py`).

---

## Controles de riesgo (`settings.py → RISK`)

| Control | Default |
|---|---|
| Máx % equity por posición | 10% |
| Órdenes máx/día | 20 |
| Circuit breaker (pérdida diaria) | -5% del equity |
| Máx slippage entre selección y envío | 5% |
| OI / volumen mínimos | 100 / 0 |
| EOD flatten | 15:55 ET |
| Confirmación en live | sí |

Todas las órdenes son **LIMIT** (nunca market). La salida usa marketable-limit @ bid con
reprecio controlado (máx 3 intentos, 1 tick más agresivo cada vez).

---

## Fase 2 — Schwab / thinkorswim (andamiaje, SIN VERIFICAR)

`brokers/schwab.py` implementa `SchwabBrokerAdapter` (mismos métodos que Tradier) + OAuth2,
mapeado a los endpoints **documentados** de Schwab Trader API. **No se pudo probar** sin
credenciales aprobadas.

- ⚠ Schwab **no tiene sandbox para individuos**: las órdenes son **dinero real** → por eso
  `place_order` está **HARD-GATED** por `LIVE_TRADING_ENABLED` (False = lanza error antes de
  tocar la red). El bróker activo sigue siendo Tradier paper.
- Credenciales en `secrets.py`: `SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET`, `SCHWAB_CALLBACK_URL`.
  El token OAuth se cachea en `secrets_schwab_token.json` (gitignored).

**Al aprobarte las credenciales** (https://developer.schwab.com/):
```bash
cd live_trader
py -m brokers.schwab authorize   # imprime la URL; pegás el redirect → guarda el token
py -m brokers.schwab check       # lee cuenta + chain SPY (NO opera)
```
Recién después de validar la lectura se ajusta el parseo si algo difiere de los docs, y mucho
más adelante (Fase 3) se evalúa habilitar `LIVE_TRADING_ENABLED` con confirmación manual.

---

## Limitaciones conocidas (sandbox)

- **Quotes con delay 15 min** en Tradier sandbox → el ROI no es realtime. Para realtime,
  conectá quotes de Polygon (que ya tenés) o el WebSocket de Tradier (prod).
- `stream_quotes` por defecto es **polling** (cada `POLL_INTERVAL_SEC` = 3s).
- Fills en sandbox son simulados → no reflejan slippage real.
- **Colisión `secrets.py`**: un script suelto que importe **pandas/numpy** corriendo *desde*
  `live_trader/` falla (numpy hace `from secrets import randbits` y agarra tu `secrets.py`).
  No afecta la app (Streamlit importa pandas antes) ni al daemon (no usa pandas). Si corrés
  scripts sueltos, hacelo **desde la raíz** del repo.

---

## Antes de ir a real (checklist)

- [ ] Semanas de paper sin bugs en fills/ROI/auto-TP.
- [ ] El daemon sobrevive desconexiones (probá cortando la red).
- [ ] El EOD flatten dispara.
- [ ] Circuit breaker y kill switch confirmados.
- [ ] Empezar con 1 contrato y límites de riesgo chicos.
- [ ] Revisar el `audit_log` tras cada sesión.

---

## Mapa de archivos

```
live_trader/
├─ settings.py            # config + RISK + credenciales (lee secrets.py por ruta)
├─ secrets.example.py     # plantilla (Tradier + Schwab) → copiar a secrets.py (gitignored)
├─ brokers/
│  ├─ base.py             # BrokerAdapter (interfaz abstracta)
│  ├─ tradier.py          # adapter Tradier (paper, ACTIVO)
│  └─ schwab.py           # adapter Schwab (Fase 2, andamiaje, place_order gated)
├─ core/
│  ├─ models.py           # dataclasses (Contract, Position, Order…)
│  ├─ selector.py         # selección de contrato → usa strategy_core
│  ├─ risk.py             # RiskGuard.validate_entry (pre-orden)
│  ├─ order_manager.py    # buy/sell LIMIT, fills parciales, reprecio
│  ├─ monitor.py          # PositionMonitor → ROI con bid + auto-TP (strategy_core.exit_decision)
│  ├─ alert_entry.py      # PUENTE alerta→entrada: preview_from_alert / enter_from_alert
│  └─ store.py            # SQLite (positions/audit/commands/meta=heartbeat)
├─ daemon/
│  └─ runner.py           # proceso headless: poll + auto-TP + EOD + latido + comandos
└─ ui/
   └─ app.py              # UI Streamlit: operar alertas (preview/confirm) + manual + posiciones

../strategy_core.py        # (raíz) reglas PURAS compartidas backtest↔live
../test_strategy_core.py   # test de equivalencia engine == strategy_core
```
