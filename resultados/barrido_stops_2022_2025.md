# Barrido de stops 2022-06 → 2025-12 (+2026 ene-jul por el planner) — validación del estudio refuerzo-vs-contra

_Corrido 2026-07-08 · combinación `comb_20260708_002011_759653` (32 escenarios) · 1.053 días-fecha
(984 válidos por configuración) × QQQ/SPY/IWM · base fija: solo tickers · umbral ticker 10% ·
criterio compuesto · Fase 2 · ROI de cartera = Σ ganancia ÷ Σ inversión de los 3 tickers por día._

## Las tres respuestas (comparaciones PAREADAS por configuración y día)

**(1) El Stop por pierna AYUDA — mejor mecánica del barrido.**
| leg-stop | Δ vs off (pp/día) | gana el % de los días |
|---|---|---|
| −40% | **+1.50** | 57% |
| −50% | +1.04 | 53% |
| −60% | +0.75 | 46% |
Monótono: cuanto antes se corta la pierna perdedora, mejor. Confirma FUERA de la ventana del
estudio (2026) su hallazgo «pierna ≥−40% → cortar esa pierna».

**(2) El stop del ticker (combinado −50/−60) es un seguro de cola, no un generador de EV.**
Δ ≈ +0.07/+0.08 pp/día (gana solo 16-29% de los días), PERO recorta el peor día de −95% a
−56/−66%. Barato: mismo EV, cola mucho más corta.

**(3) El refuerzo (martingala) es una apuesta al RÉGIMEN, no una ventaja.**
Pareado global: −0.25 pp/día (gana 42% de los días). Por año:
| año | Δ refuerzo (pp/día) |
|---|---|
| 2022 (vol alta) | **+1.39** |
| 2023 | −0.09 |
| 2024 (tranquilo) | **−1.91** |
| 2025 (tranquilo) | **−1.41** |
| 2026 ene-jul | **+3.68** |
En años de volatilidad/tendencia paga; en años calmos destruye. Coherente con que los campeones
2026 del playbook lo lleven — y con que el estudio (que miraba reforzar como acción de RESCATE en
drawdowns profundos) lo condene: como rescate tardío es malo siempre; como mecánica integrada
depende del régimen. Además compromete ~3× más capital (P&L en $ mucho más negativo) y sin stop
su peor día llega a −92%.

## La lectura incómoda (y esperada)

**Ninguna de las 32 configuraciones planas es rentable en 3.5 años**: la mejor (normal · leg-stop
40 · stop −60) promedia −4.60%/día de cartera. Esto NO contradice los veredictos del playbook
(que seleccionan día-de-semana × configuración con gate P5 y sí encontraron bolsillos positivos
en 2026): confirma que comprar 0DTE «siempre, con una config fija» tiene expectativa negativa —
la literatura académica de compra sistemática de opciones, reproducida en casa. La ventaja, si
existe, vive en la SELECTIVIDAD (cuándo operar), no en la mecánica base (cómo salir). Las
mecánicas de este barrido solo achican la sangría del lado malo.

## Acciones sugeridas
1. Probar «Stop por pierna (%) (escenario)» = 40 como variable EN las combinaciones campeonas
   (12,288 / tipos de salida): ¿mejora los días OPERABLES del playbook?
2. Tratar el refuerzo como variable condicionada al régimen de vol (el monitor regimen_vol ya
   existe) — no como default.
3. Stop ticker −50/−60: mantener como seguro de cola en configs sin leg-stop.

## Advertencias de la casa
- Comparaciones pareadas direccionales (sin p-values acá; los días están correlacionados entre
  configs y tickers — cualquier test sería optimista).
- Base plana sin selección por día de semana ni gate: mide MECÁNICAS, no un sistema operable.
- 2022 arranca en junio y 2026 es parcial (ene-jul, agregado por el planner incremental).
