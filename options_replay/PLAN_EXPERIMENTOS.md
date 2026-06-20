# Plan de experimentos — backtest 0DTE con la muestra grande (4 años)

Tras el backfill de NBBO (2026-06-19), QQQ/SPY/IWM tienen **~4 años de 0DTE diario con fills
honestos (Fase 2)**, cruzando bear 2022 / recuperación 2023 / bull 2024 / 2025-26. Eso habilita
pasar de "anécdota de 7 días" a **estadística que cruza regímenes**. Los 8 semanales (~100 días,
2025-04→) son suplementarios.

## Pregunta madre
1. ¿La **martingala (Refuerzo)** realmente se funde en NBBO real, o el resultado malo fue ruido
   de muestra chica? (En barras: ~88% win; en Fase 2 sobre 7 días: PF 0.32 / ruinoso.)
2. Si el straddle 0DTE ciego es break-even neto (~PF 0.97 medido), ¿hay **alguna config/filtro
   con edge que sobreviva los 4 regímenes** y out-of-sample?

## Fase 0 — Higiene (no medir nada sin esto)
- **Siempre Fase 2** (Modelo de fills = "NBBO por barra"). El precio de barra MIENTE para la martingala.
- Mirar **el panel de riesgo**, no el win rate: PF, max drawdown, peor día, # días −100%, Sortino,
  curva de equity, distribución. Un win rate alto con cola gorda = trampa.
- **Re-correr al cambiar cualquier parámetro** (los totales se renderizan del último run; no te fíes
  de un total que no recalculaste).
- **Comisiones**: sumar un costo por contrato realista. Sin eso, todo se ve mejor de lo que es. (Pendiente
  de cablear en el engine; mientras tanto, descontá mentalmente ~$1–2/contrato/lado.)

## Experimento 1 — Base rate honesto (el ancla)
- Straddle ciego diario · Opción 1 (menor spread) · Fase 2 · QQQ/SPY/IWM · Modo fecha = **Rango**, 4 años.
- Primero "hold hasta 16:00" (baseline puro), sin TP/stop.
- **Mide:** PF, retorno neto, max drawdown, peor día, distribución diaria por ticker.
- **Responde:** ¿el straddle 0DTE es estructuralmente break-even/perdedor por theta + spread? Fija el piso
  contra el que se compara todo lo demás.

## Experimento 2 — El veredicto de la martingala (el importante)
- Tipo = "CALL y PUT (Refuerzo)" · Fase 2 · QQQ/SPY/IWM · 4 años.
- Barrer con `sweep_sensitivity.py`: **refuerzo_max** (0,1,2,3,4) × **umbral pérdida refuerzo** × **stop**.
- **Métrica que manda:** # días −100% (ruina), max drawdown, peor racha, PF. NO el win rate.
- **Hipótesis a refutar:** "convierte muchos días ganadores chicos en pocos días catastróficos". Con 4 años
  (incluido 2022) los días de cola que la funden van a aparecer si existen.
- **Decisión:** si PF < 1 o hay días de ruina recurrentes → martingala descartada con evidencia dura.

## Experimento 3 — Buscar el edge (si lo hay)
- **Salida:** umbral ROI rápido (10%) vs medio (20%) vs lento (30%) × stop. En Fase 2 el ranking se invierte
  (TP rápido gana) — confirmar en 4 años.
- **Ventana de búsqueda:** 0 vs 2 vs 4 vs 6 min. ¿Rescatar la señal 09:30 (spread de subasta ancho) mejora
  el neto, o mete contratos ilíquidos?
- **Criterio:** Opción 1 (spread) vs Opción 2 (ITM) — cuál da mejor PF honesto.
- **Direccional:** Single CALL / Single PUT filtrado por las Alertas (señales) — ¿las señales tienen edge
  direccional en 0DTE, o conviene neutral (straddle)?

## Experimento 4 — Robustez cruzando regímenes (lo que la muestra grande habilita)
- Partir por régimen: **2022 (bear) · 2023 (recuperación) · 2024 (bull) · 2025-26 (reciente)**. ¿La mejor
  config del Exp.3 sobrevive en los 4, o solo en bull?
- **Walk-forward:** optimizar en 2022-2024, validar **out-of-sample** en 2025-2026. Si el edge desaparece
  OOS = overfitting.
- **Por-ticker vs portfolio:** ¿QQQ/SPY/IWM se comportan igual? ¿combinarlos diversifica la curva?

## Experimento 5 — Del backtest al paper (cerrar el loop)
- La/las config que pasen Exp.2-4 → correrlas en **Tradier sandbox** vía `trading_core` (paper, sin plata
  real) y comparar fills reales vs los del backtest. Si coinciden, la lógica es sólida.

## Criterios de éxito (cuándo una config "merece" paper)
- **PF ≥ ~1.2-1.3 neto de comisiones**, en los 4 años Y out-of-sample.
- Max drawdown tolerable para tu capital; **sin días de ruina recurrentes**.
- Sortino decente; curva de equity que sube **sin un día que borre meses**.
- **Estable cruzando 2022-2026** (no solo bull).

> Orden sugerido: Exp.1 (ancla) → Exp.2 (¿martingala sí o no?) → si no, Exp.3 (edge) → Exp.4 (robustez) →
> Exp.5 (paper). Cada uno con el panel de riesgo, siempre Fase 2.
