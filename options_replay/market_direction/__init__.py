"""market_direction — motor de evaluación de dirección de mercado para 0DTE (SPY, confirmado por QQQ).

Arquitectura limpia por capas (sin look-ahead, SOLID, dos costuras swappables):

    domain/         entidades + contratos (TradeSignal, Candle/Session, FeatureSet, Confirmation, enums)
    data/           MarketDataProvider (interface) + impl cacheada (Downloader)        ← costura de DATOS
    indicators/     cómputo CAUSAL de los ~50 features (solo velas ≤ t)
    confirmation/   confirmación cross-asset (SPY↔QQQ) → Confirmation
    model/          DirectionModel (interface) + RuleBasedModel (hoy) / ML (futuro)    ← costura de MODELO
    calibration/    Calibrator (interface): Heuristic (hoy) → Empirical (post-backtest) ← costura de CONFIANZA
    decision/       SignalGenerator + LevelCalculator (entry/stop/target, R:R≥2)
    engine.py       market_direction_engine() + evaluate_at_minute()
    ui/             medidor (Streamlit HTML/CSS) para la página «Evaluar dirección del mercado»

El score (0–100, fuerza direccional) lo da el `model`; la confianza (0–1, prob. de acierto) la da el
`calibration`. Separadas a propósito: la confianza pasa de heurística a calibrada-por-backtest a ML
sin tocar el resto. Punto de entrada público: `market_direction.engine.market_direction_engine(...)`.
"""
