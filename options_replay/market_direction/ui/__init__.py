"""ui — capa de presentación (Streamlit) del motor de dirección.

`universe_view` arma la tabla del universo operable (núcleo diario + roster viernes) a partir del
registro de `TickerProfile`. La construcción de la tabla es PURA (testeable sin Streamlit); el
render con `st.*` vive en la página.
"""
from .universe_view import build_universe_df, build_verdict_df, tradeable_today_summary

__all__ = ["build_universe_df", "build_verdict_df", "tradeable_today_summary"]
