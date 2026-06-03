# live_trader — trading de opciones en vivo (SANDBOX por defecto)

⚠ **Por defecto apunta a Tradier SANDBOX (paper money).** Operar en vivo requiere
flippear `LIVE_TRADING_ENABLED = True` en `config.py` y cargar credenciales prod.
**Probá semanas en sandbox antes de pensar siquiera en real.**

## Arquitectura

```
UI (Streamlit)  ──comandos──▶  SQLite store  ◀──estado──  Daemon (headless)
   control + dashboard                                     streaming + ROI + auto-TP
                                       │
                                BrokerAdapter (Tradier)
```

**Clave:** el monitoreo de ROI y el auto take-profit corren en el **daemon**, NO en
la UI. Así la venta automática sigue funcionando aunque cierres el browser.

## Setup

1. **Token de sandbox**: creá cuenta en https://dashboard.tradier.com/ → Sandbox → API Access.
2. **Credenciales**:
   ```
   cp live_trader/secrets.example.py live_trader/secrets.py
   # editá secrets.py con tu TRADIER_SANDBOX_TOKEN y ACCOUNT_ID
   ```
   (o exportá `TRADIER_SANDBOX_TOKEN` / `TRADIER_SANDBOX_ACCOUNT_ID` como env vars)
3. **Dependencias**: `requests`, `streamlit`, `pandas` (ya las tenés del otro proyecto).

## Correr

Dos procesos, en dos terminales:

```bash
# Terminal 1 — el daemon (monitoreo + auto-TP). DEBE estar corriendo.
cd live_trader
py -m daemon.runner

# Terminal 2 — la UI
cd Traiding
py -m streamlit run live_trader/ui/app.py
```

## Flujo de uso

1. UI: ticker + CALL/PUT + cantidad + ROI objetivo → **Analizar** → muestra el mejor contrato.
2. Revisás strike/exp/bid/ask/spread/delta/vol/OI → pasa validación de riesgo → **Comprar**.
3. El daemon empieza a monitorear ROI (usando el **bid**, lo realmente vendible).
4. UI: **Activar TP** → cuando ROI ≥ objetivo, el daemon vende automáticamente.
5. **EOD flatten**: el daemon cierra cualquier 0DTE antes de las 15:55 ET (riesgo de asignación).
6. **Kill switch**: botón que cierra todo y desarma TP.

## Selección de contrato (misma lógica que el backtest)

`core/selector.py`: filtra por spread ≤ máx del bucket de precio + liquidez mínima
(OI ≥ 100, vol ≥ 10) y prioriza (menor spread, cercanía ATM/1-ITM, mayor OI).

## Controles de riesgo (`config.py → RISK`)

| Control | Default |
|---|---|
| Máx % equity por posición | 10% |
| Órdenes máx/día | 20 |
| Circuit breaker (pérdida diaria) | -5% del equity |
| Máx slippage entre selección y envío | 5% |
| OI / volumen mínimos | 100 / 10 |
| EOD flatten | 15:55 ET |
| Confirmación en live | sí |

Todas las órdenes son **LIMIT** (nunca market). La salida usa marketable-limit @ bid
con reprecio controlado (máx 3 intentos, 1 tick más agresivo cada vez).

## Limitaciones conocidas (sandbox)

- **Quotes con delay 15 min** en sandbox → el ROI no es realtime. Para realtime real,
  conectá quotes de Polygon (que ya tenés) o el streaming WebSocket de Tradier (prod).
- El default `stream_quotes` es **polling** (cada 3s). Para baja latencia, implementá el
  WebSocket de Tradier en `TradierAdapter.stream_quotes`.
- Fills en sandbox son simulados → no reflejan slippage real del mercado.

## Antes de ir a real (checklist)

- [ ] Semanas de paper sin bugs en fills/ROI/auto-TP.
- [ ] Verificar que el daemon sobrevive desconexiones (probá cortando la red).
- [ ] Confirmar que el EOD flatten dispara.
- [ ] Confirmar circuit breaker y kill switch.
- [ ] Empezar con 1 contrato y límites de riesgo chicos.
- [ ] Revisar el audit_log tras cada sesión.
