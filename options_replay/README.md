# Options Replay — Intraday 0 DTE

Herramienta independiente para reproducir el comportamiento minuto a minuto
de un **Call + Put 0 DTE** comprados al inicio de una sesión, usando datos
reales de Polygon.io.

## Qué hace

A partir de 4 parámetros que vos definís:

1. **Ticker** (cualquier activo con opciones 0 DTE — SPY, QQQ, IWM, etc.)
2. **Fecha** específica de mercado
3. **Rango óptimo del premium** del contrato (ej. $1.50 – $4.00)
4. **Ventana horaria** (default 09:30–16:00 ET)

el sistema **automáticamente**:

- Restringe la búsqueda a **0 DTE** (expiración == fecha)
- Para cada lado (Call y Put), recorre los strikes de menor a mayor distancia
  al spot al inicio de la ventana
- Toma el **primer contrato cuyo premium de apertura** caiga dentro del rango
  definido → ese es **el más cercano a ATM** que cumple la condición
- Calcula minuto a minuto: precio Call real, precio Put real, total, PnL
  acumulado desde el premium de entrada

## Setup

```powershell
cd options_replay
py -m pip install -r requirements.txt
```

Agregar la API key de Polygon a `..\config.py`:

```python
POLYGON_API_KEY = "tu_key_aqui"
```

## Uso

```powershell
py -m streamlit run app.py
```

Se abre en `http://localhost:8501`. En la barra lateral:

- Ticker → SPY, QQQ, IWM, DIA…
- Fecha → el día que querés estudiar
- Rango premium → ej. Min $1.50, Max $4.00
- Ventana horaria → ej. 09:30–16:00

Pulsás **Ejecutar replay** y obtenés:

- **Resumen** con strikes elegidos, OCC symbols, premium de entrada
- **Strikes probados** (expandible) — útil para entender por qué se eligió ese strike
- **Gráfico Plotly** con subyacente, prima total, PnL y banda del rango premium
- **Tabla minuto a minuto**
- **Descargas** CSV y Excel

## Importante: 0 DTE solo existe en algunos tickers

Tickers con expiración diaria (lunes a viernes):
**SPY, QQQ, IWM, DIA, VIX** y algunos ETFs sectoriales.

Para AAPL, NVDA, TSLA, etc. solo hay weeklies (vencen los viernes). En esos
casos solo podrás usar la app cuando la fecha caiga en viernes.

Si no hay 0 DTE en la fecha elegida, el sistema te lo dice claramente.

## Arquitectura

```
adapter_polygon.py   Cliente HTTP con rate-limit y builder OCC
downloader.py        Cache Parquet (underlying / chain / option)
engine.py            replay_session: filtro 0 DTE + probe por premium-range
analytics.py         Estadísticas de la sesión
app.py               UI Streamlit
smoke_test.py        Validación end-to-end de Polygon (opcional)
data/                Cache Parquet (gitignored)
```

## Cache

Los datos descargados se cachean en `data/`:

```
data/underlying/{ticker}_{date}.parquet
data/chain/{ticker}_{expiry}.parquet
data/options/{occ_symbol}_{date}.parquet
```

Una vez bajados, podés cancelar la suscripción a Polygon y seguir usando
los Parquets locales para siempre.
