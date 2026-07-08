"""Playbook persistido (Configuración → Backtesting) — la tabla día-de-la-semana OPERAR/NO OPERAR
con su escenario, métricas y CONDICIONES, evaluada sobre un rango de fechas.

Fuente de verdad de las condiciones: el TEMPLATE del batch (hoja «Backtesting scenarios»).
Pipeline de reevaluación: el MISMO del puente de la app — copiar el template con el rango/tickers
elegidos, correr `run_ucbatch.py` en una CONSOLA APARTE (480 escenarios, multiproceso) y, al
terminar, interpretar el results detallado con bt_analysis → veredicto por día a nivel CARTERA.

Persistencia: data/playbook.json (+ data/playbook_job.json mientras corre la reevaluación).
Sin Streamlit: lo consume configuracion_app.py (UI) y app.py («(playbook automático)»).

SUPUESTOS (documentados):
  · «Playbook» = veredicto por día a nivel cartera (Σganancia/Σinversión por fecha) del motor de
    interpretación, con el escenario ganador por Sharpe y sus condiciones del template.
  · «Aplicación automática por fecha» = por DÍA DE LA SEMANA del playbook guardado; el panel
    aplica UNA config por corrida, así que solo aplica cuando las filas cargadas comparten un
    único día de la semana (si no, avisa). NO OPERAR bloquea la aplicación.
  · La reevaluación tarda ~20 s por día hábil del rango (batch completo de 480 escenarios).
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
PB_PATH = HERE / "data" / "playbook.json"
JOB_PATH = HERE / "data" / "playbook_job.json"
# Solo para la migración ÚNICA del template legacy a combinations.db (ensure_legacy) — el
# pipeline ya NO lee este archivo: los escenarios viven en la base (Combinaciones).
TEMPLATE_PATH = HERE.parent / "excels for backtesting" / "Backtesting_use_cases_template.xlsx"
RESULTS_DIR = HERE.parent / "resultados"

_WD_ES = {0: "Lun", 1: "Mar", 2: "Mié", 3: "Jue", 4: "Vie", 5: "Sáb", 6: "Dom"}


# ── Persistencia ─────────────────────────────────────────────────────────────
def load_playbook(path: Path = PB_PATH):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — sin playbook guardado todavía
        return None


def save_playbook(pb: dict, path: Path = PB_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(pb, indent=2, ensure_ascii=False), encoding="utf-8")


def scenario_for_date(pb, fecha) -> dict | None:
    """Config del playbook que corresponde a una FECHA (por su día de la semana). Devuelve la
    entrada del día ({scenario, recommendation, config, …}) o None si no hay playbook / el día
    no está cubierto (finde). El caller decide qué hacer con recommendation=NO OPERAR."""
    if not pb:
        return None
    try:
        import pandas as pd
        dia = _WD_ES.get(pd.Timestamp(str(fecha)).weekday())
    except Exception:  # noqa: BLE001
        return None
    return (pb.get("per_day") or {}).get(dia)


# ── Construcción desde un results detallado (post-batch) ─────────────────────
def build_from_results(results_path, combination: str | None = None) -> dict:
    """(compat) Interpreta un results FILE → playbook. El camino sin-Excel (ingesta directa)
    usa `build_from_df` con las filas del almacén — misma semántica, sin parsear archivos."""
    from bt_analysis import loader
    rdf, gran = loader.load_results(str(results_path))
    return build_from_df(rdf, gran, combination=combination, results_ref=str(results_path))


def build_from_df(rdf, gran: str = "detailed", combination: str | None = None,
                  results_ref: str = "") -> dict:
    """Interpreta un DataFrame CANÓNICO detallado → dict del playbook persistible: rango
    evaluado, tickers, veredicto por día (escenario, WR, ROI cartera, Sharpe, n, condiciones)
    y el desglose día×ticker. Las CONDICIONES salen de la COMBINACIÓN (combinations.db).
    Mismo motor que «Interpretar resultados»; `results_ref` es solo trazabilidad (antes era el
    path del xlsx; con ingesta directa es 'almacén:<run_id>')."""
    import pandas as pd

    import combinations as _comb
    from bt_analysis import engine
    from trade_plan import scenario_config_summary

    combination = combination or _comb.active_combination() or _comb.LEGACY_ID
    _c = _comb.get_combination(combination) or {}
    sc = _comb.scenarios_df(combination)
    _seed_info = dict(_c.get("seed") or {})
    _seed_info["Tickers"] = ", ".join(_seed_info.get("tickers") or [])
    rep = engine.analyze(rdf, gran, scenarios_df=(None if sc.empty else sc), seed=_seed_info)
    dow = rep.get("dow") or {}
    if not dow.get("available"):
        raise ValueError("El results no permite el análisis por día de la semana "
                         "(¿granularidad agregada?). Usá un results DETALLADO.")
    # Rango evaluado: de las FECHAS del results (no del seed, que puede traer otro rango).
    _f = pd.to_datetime(rdf.get("fecha"), errors="coerce").dropna()
    from bt_analysis import playbook as _pbk
    cfgs = _pbk._scenario_configs(rep)

    per_day = {}
    for dia, i in (dow.get("per_day") or {}).items():
        cfg = cfgs.get(str(i.get("scenario") or "").strip())
        per_day[dia] = {
            "scenario": i.get("scenario"), "win_rate": i.get("win_rate"),
            "avg_roi": i.get("avg_roi"), "sharpe": i.get("sharpe"), "n": i.get("n"),
            "recommendation": i.get("recommendation"), "reason": i.get("reason", ""),
            "config": cfg, "config_txt": scenario_config_summary(cfg),
        }
    dt = rep.get("dow_ticker")
    por_ticker = dt.to_dict("records") if dt is not None and not dt.empty else []

    return {
        "evaluado_desde": str(_f.min().date()) if len(_f) else None,
        "evaluado_hasta": str(_f.max().date()) if len(_f) else None,
        "tickers": _seed_info.get("Tickers") or "",
        "generado_en": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "results_file": results_ref,
        "combination": combination, "combination_nombre": _c.get("nombre") or combination,
        "n_dias": rep.get("n_days"), "n_posiciones": rep.get("n_positions"),
        "per_day": per_day, "por_ticker": por_ticker,
    }


# ── Actualización INCREMENTAL: veredicto sobre ventana rodante con decaimiento ───────────────────
HISTORY_PATH = HERE / "data" / "playbook_history.jsonl"


def _betacf(a: float, b: float, x: float) -> float:
    """Fracción continua de la beta incompleta (método de Lentz, Numerical Recipes §6.4)."""
    MAXIT, EPS, FPMIN = 200, 3e-12, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < EPS:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """I_x(a,b) regularizada (CDF de la Beta) en puro Python — sin scipy."""
    import math
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                  + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _beta_ppf_puro(q: float, a: float, b: float) -> float:
    """Cuantil de la Beta por bisección sobre _betainc (80 iteraciones — sobra precisión)."""
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if _betainc(a, b, mid) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _beta_p5(wins_w: float, losses_w: float) -> float:
    """Límite INFERIOR creíble (percentil 5) del win-rate: posterior Beta(1+wins, 1+losses)
    con pseudo-conteos PONDERADOS (el decaimiento reduce la evidencia efectiva → intervalos
    más anchos con muestras viejas/chicas — la lección del Lun C061). En %.

    scipy con FALLBACK puro-Python: Windows Application Control puede bloquear las DLL
    compiladas de scipy (visto 2026-07-05 con _linalg_pythran.pyd) — el gate del playbook
    no puede depender de eso. La bisección da el mismo número (≈1e-10 de diferencia)."""
    a, b = 1.0 + max(wins_w, 0.0), 1.0 + max(losses_w, 0.0)
    try:
        from scipy.stats import beta
        return float(beta.ppf(0.05, a, b) * 100.0)
    except Exception:  # noqa: BLE001 — ImportError / DLL bloqueada / scipy roto
        return _beta_ppf_puro(0.05, a, b) * 100.0


def _wmetrics(rois, w) -> dict:
    """Métricas PONDERADAS de una serie de ROI de cartera diarios: win-rate, media, Sharpe y el
    límite creíble P5 del win-rate (Beta), con pesos exponenciales (los días recientes votan
    más). `n` queda SIN ponderar (muestra honesta)."""
    sw = float(w.sum())
    if sw <= 0 or len(rois) == 0:
        return {}
    wins_w = float(w[rois > 0].sum())
    wr = wins_w / sw * 100
    mean = float((w * rois).sum() / sw)
    var = float((w * (rois - mean) ** 2).sum() / sw)
    std = var ** 0.5
    sharpe = (mean / std) if std > 0 else float("nan")
    return {"n": int(len(rois)), "win_rate": round(wr, 1),
            "wr_p5": round(_beta_p5(wins_w, sw - wins_w), 1),
            "avg_roi": round(mean, 3),
            "sharpe": (round(sharpe, 3) if sharpe == sharpe else None)}


def _gate(m: dict, min_n: int) -> tuple[bool, str]:
    """Gate BAYESIANO: en vez del win-rate puntual exige que el LÍMITE INFERIOR creíble (P5)
    supere 55% — un WR 75% con n=12 NO pasa (P5≈52%), el mismo 75–80% sostenido n≈30 sí
    (P5≈66%). Elimina de raíz los veredictos por muestra chica (la lección del Lun C061)."""
    if not m:
        return False, "sin datos"
    if m["n"] < min_n:
        return False, f"muestra insuficiente (n={m['n']} < {min_n} días)"
    # Sharpe None = desviación 0 (serie constante): si la media es positiva, es el MEJOR caso
    # (ganancia determinística), no un rechazo.
    sh_ok = (m["sharpe"] > 0) if m["sharpe"] is not None else (m["avg_roi"] > 0)
    ok = m.get("wr_p5", 0) > 55 and m["avg_roi"] > 0 and sh_ok
    return ok, ("ventaja creíble: P5(WR)>55% · ROI cartera>0 · Sharpe>0" if ok
                else f"sin ventaja creíble (P5 WR={m.get('wr_p5')}% ≤55, o ROI≤0, o Sharpe≤0)")


def _half_pass(rois, min_n_half: int) -> bool:
    """Gate SIMPLE (sin pesos) sobre una mitad de la ventana — para la máquina de estados de
    supervivencia: WR>55% · media>0 · n≥min_n_half."""
    from bt_analysis import detailed as det
    m = det.series_metrics(rois)
    if not m or m["n"] < min_n_half:
        return False
    sh = m.get("sharpe")
    sh_ok = (sh > 0) if (sh is not None and sh == sh) else (m["avg_roi"] > 0)
    return m["win_rate"] > 55 and m["avg_roi"] > 0 and sh_ok


def _estado_supervivencia(daily, w1: set, w2: set, min_n_half: int) -> tuple[str, str]:
    """Máquina de estados del escenario elegido, con la regla de las DOS VENTANAS (validada con
    feb–mar vs abr–jun) computada sobre las mitades de la ventana rodante + kill-switch:

      operable    — pasa el gate en AMBAS mitades (edge que sobrevive ventanas)
      candidato   — pasa solo en la mitad RECIENTE (edge nuevo, sin confirmar)
      suspendido  — pasaba antes y ya no / kill-switch (3 sesiones seguidas perdedoras
                    o ROI acumulado de la ventana ≤ −15%)
      sin ventaja — no pasa en ninguna

    `daily` = serie de ROI de cartera indexada por fecha (str), cronológica."""
    rois = daily.sort_index()
    # Kill-switch primero (manda sobre todo): protege del régimen que se dio vuelta.
    if len(rois) >= 3 and bool((rois.tail(3) < 0).all()):
        return "suspendido", "kill-switch: 3 sesiones seguidas perdedoras"
    if float(rois.sum()) <= -15.0:
        return "suspendido", f"kill-switch: ROI acumulado {rois.sum():.1f}% ≤ −15%"
    p1 = _half_pass(rois[rois.index.isin(w1)], min_n_half)
    p2 = _half_pass(rois[rois.index.isin(w2)], min_n_half)
    if p1 and p2:
        return "operable", "pasa el gate en las DOS mitades de la ventana"
    if p2:
        return "candidato", "pasa solo en la mitad reciente — edge sin confirmar (1 ventana)"
    if p1:
        return "suspendido", "pasaba en la mitad vieja y ya no — edge decaído"
    return "sin ventaja", "no pasa el gate en ninguna mitad"


def _rank(m: dict) -> float:
    """Clave de orden para elegir el mejor escenario: Sharpe ponderado; con desviación 0
    (Sharpe None), una media positiva rankea arriba y una negativa abajo."""
    if m.get("sharpe") is not None:
        return m["sharpe"]
    return 9e9 if (m.get("avg_roi") or 0) > 0 else -9e9


# ── Monitores de vigencia (política de mantenimiento) ────────────────────────
_CALIB_N = 10          # sesiones recientes para calibración y CUSUM
RETADOR_RACHA = 5      # re-agregaciones consecutivas que el retador debe dominar para destronar


def _monitor_calibracion(row: dict, serie) -> None:
    """Monitor «calibración rota»: si el WR REALIZADO de las últimas _CALIB_N sesiones cae por
    debajo del P5 prometido, el piso creíble está roto → suspende el día. Solo interviene en
    días accionables (OPERAR) y nunca pisa una suspensión previa (kill-switch conserva su
    motivo). Los campos wr_reciente/calib_alerta se publican siempre (informativos)."""
    rec = serie.sort_index().tail(_CALIB_N)
    if len(rec) < _CALIB_N:
        return
    wr_rec = round(float((rec > 0).mean() * 100), 1)
    row["wr_reciente"] = wr_rec
    p5 = row.get("wr_p5")
    row["calib_alerta"] = bool(p5 is not None and wr_rec < p5)
    if row["calib_alerta"] and row.get("recommendation") == "OPERAR" \
            and row.get("estado") != "suspendido":
        row["estado"] = "suspendido"
        row["estado_motivo"] = (f"calibración rota: WR realizado {wr_rec}% en las últimas "
                                f"{_CALIB_N} sesiones < P5 prometido {p5}%")
        row["recommendation"] = "NO OPERAR"
        row["reason"] = row["estado_motivo"]


def _monitor_cusum(row: dict, serie) -> None:
    """Monitor «CUSUM del error de pronóstico»: Σ(ROI_t − ROI prometido) de las últimas
    _CALIB_N sesiones contra la banda −2σ·√n. NO suspende: fuera de banda significa que lo
    realizado viene sistemáticamente bajo lo prometido → pide REVISIÓN ANTICIPADA de
    parámetros (la única causa válida de tocar ventana/half-life fuera del trimestre)."""
    import numpy as np
    s = serie.sort_index()
    rec = s.tail(_CALIB_N)
    prom = row.get("avg_roi")
    if len(rec) < _CALIB_N or prom is None:
        return
    sigma = float(s.std(ddof=1)) if len(s) > 1 else 0.0
    if not sigma > 0:
        return
    dev = float((rec - prom).sum())
    umbral = -2.0 * sigma * float(np.sqrt(len(rec)))
    row["cusum_dev"] = round(dev, 2)
    row["cusum_umbral"] = round(umbral, 2)
    row["cusum_alerta"] = bool(dev < umbral)


def compute_churn(history: list, per_day: dict, *, lookback: int = 20,
                  umbral: float = 0.30, min_snaps: int = 6) -> dict:
    """Monitor «churn de veredicto» (PURO): tasa de cambio del escenario campeón por día-semana
    a lo largo de las últimas re-agregaciones (snapshots del history + el veredicto actual).
    Un campeón que rota seguido es selección por ruido entre variantes correlacionadas —
    fragilidad, no señal. Con menos de `min_snaps` puntos no alerta (sin evidencia)."""
    out: dict = {}
    for wd, info in (per_day or {}).items():
        past = [s.get("per_day", {}).get(wd, {}).get("scenario") for s in (history or [])]
        serie = [x for x in past if x][-lookback:]
        if info.get("scenario"):
            serie.append(info["scenario"])
        if len(serie) < min_snaps:
            out[wd] = {"n": len(serie), "tasa": None, "alerta": False}
            continue
        flips = sum(1 for a, b in zip(serie, serie[1:]) if a != b)
        tasa = flips / (len(serie) - 1)
        out[wd] = {"n": len(serie), "tasa": round(tasa, 2), "alerta": bool(tasa > umbral)}
    return out


def _load_history_snapshots(path: Path = HISTORY_PATH, *, solo_incremental: bool = True,
                            combination: str | None = None) -> list:
    """Snapshots del history (JSONL) para el churn. Filtra los del modo incremental (una
    reevaluación manual con otro rango elige otro escenario legítimamente y contaminaría la
    tasa) y deduplica por día de generación quedándose con el último.

    `combination`: filtra los snapshots de ESA combinación. Bug 2026-07-08: la historia era
    global sin atribución y la ACTIVA fue rotando → el churn comparaba campeones de universos
    DISTINTOS (C0575 del 480 vs C05575 del 12.288…) → 83% de «cambios» falsos → suspensiones
    masivas injustas. Los snapshots legacy sin campo `combination` se EXCLUYEN al filtrar (no
    son atribuibles); el churn de cada combinación arranca limpio y junta muestra en días."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001 — sin history todavía
        return []
    dedup: dict = {}
    for ln in lines:
        try:
            s = json.loads(ln)
        except Exception:  # noqa: BLE001 — línea corrupta: se salta
            continue
        if solo_incremental and "incremental" not in str(s.get("modo", "")):
            continue
        if combination is not None and s.get("combination") != combination:
            continue
        dedup[str(s.get("generado_en", ""))[:10]] = s
    return [dedup[k] for k in sorted(dedup)]


def _incumbents_from(pb) -> dict:
    """Campeones vigentes (y su retador en racha) del playbook anterior → histéresis."""
    out: dict = {}
    for wd, i in ((pb or {}).get("per_day") or {}).items():
        if i.get("scenario"):
            out[wd] = {"scenario": i["scenario"], "retador": i.get("retador")}
    return out


def compute_vol_regime(dates: list[str], *, ticker: str = "SPY", n_recent: int = 5,
                       n_base: int = 60, factor: float = 2.0) -> dict | None:
    """Monitor «cambio de régimen de vol»: vol realizada intradía (std de retornos 1-min) de
    las últimas `n_recent` sesiones vs la MEDIANA de las últimas `n_base` — el proxy de VIX
    sin dependencia nueva (usa el mismo cache de subyacente del MD engine). ratio > factor →
    alerta. Devuelve None si no hay datos suficientes (nunca rompe el build)."""
    try:
        import numpy as np
        import pandas as pd
        from market_direction.data import default_provider
        from market_direction.data.caching_provider import CachingProvider

        prov = CachingProvider(default_provider())
        rv: dict = {}
        for f in (dates or [])[-n_base:]:
            try:
                s = prov.session(ticker, f)
                if s is None or getattr(s, "empty", True) or len(s) < 30:
                    continue
                r = pd.Series(np.asarray(s.closes, float)).pct_change().dropna()
                if len(r):
                    rv[f] = float(r.std())
            except Exception:  # noqa: BLE001 — día sin datos: se salta
                continue
        if len(rv) < max(20, n_recent * 2):
            return None
        ser = pd.Series(rv).sort_index()
        vol_rec = float(ser.tail(n_recent).mean())
        med = float(ser.median())
        if not med > 0:
            return None
        ratio = vol_rec / med
        return {"ticker": ticker, "vol_5d": round(vol_rec, 6), "mediana_60d": round(med, 6),
                "ratio": round(ratio, 2), "alerta": bool(ratio > factor),
                "n_sesiones": int(len(ser))}
    except Exception:  # noqa: BLE001 — el monitor es informativo, jamás tumba el build
        return None


def compute_weighted_verdict(df, window_dates: list[str], *, half_life: int = 35,
                             min_n: int = 16, incumbents: dict | None = None) -> tuple[dict, list]:
    """PURO: filas canónicas (formato loader) + fechas de la ventana → (per_day, por_ticker) con
    pesos w = 0.5^(días_hábiles_atrás / half_life). ROI del día = Σpnl/Σinv (nivel cartera);
    por_ticker usa el Σ del ticker ese día. El mejor escenario del día se elige por Sharpe
    ponderado; el gate exige n (sin ponderar) ≥ min_n.

    `incumbents` (opcional) activa la HISTÉRESIS campeón/retador: {wd: {"scenario", "retador"}}
    del playbook anterior. El campeón vigente solo cede si está muerto (gate/estado) con retador
    vivo — promoción inmediata — o si el retador operable lo domina en P5 durante RETADOR_RACHA
    re-agregaciones consecutivas. Evita el flip-flop entre variantes correlacionadas.

    Cada día publica además los monitores de vigencia: wr_reciente/calib_alerta (suspende si el
    WR realizado de las últimas 10 sesiones < P5 prometido) y cusum_dev/umbral/alerta
    (informativo: pide revisión anticipada de parámetros)."""
    import numpy as np
    from bt_analysis import detailed as det

    d = det.prepare(df[df["fecha"].astype(str).str[:10].isin(window_dates)].copy())
    if d.empty:
        return {}, []
    # días hábiles atrás DENTRO de la ventana (0 = el más reciente) → peso exponencial
    ago = {f: i for i, f in enumerate(reversed(window_dates))}
    d["_fstr"] = d["date"].dt.strftime("%Y-%m-%d")
    d["_w"] = d["_fstr"].map(ago).map(lambda a: 0.5 ** (a / half_life) if a == a else np.nan)
    d = d.dropna(subset=["_w"])

    def _daily(gr, keys):
        g = gr.groupby(keys + ["_fstr"]).agg(pnl=("pnl", "sum"), inv=("inv", "sum"),
                                             w=("_w", "first"))
        g["roi"] = g["pnl"] / g["inv"].replace(0, np.nan) * 100.0
        return g.dropna(subset=["roi"])

    # Mitades de la ventana para la máquina de estados (regla de las dos ventanas).
    half = len(window_dates) // 2
    w1, w2 = set(window_dates[:half]), set(window_dates[half:])
    min_n_half = max(4, min_n // 2)

    per_day: dict = {}
    g_cart = _daily(d, ["weekday", "id"])
    for wd in det._WD_ORDER:
        try:
            sub = g_cart.xs(wd, level="weekday")
        except KeyError:
            continue
        cand: dict = {}
        for sid, gg in sub.groupby(level="id"):
            m = _wmetrics(gg["roi"], gg["w"])
            if m:
                cand[sid] = {"scenario": sid, **m}
        if not cand:
            per_day[wd] = {"recommendation": "NO OPERAR", "reason": "sin datos ese día",
                           "estado": "sin ventaja", "estado_motivo": "sin datos",
                           "retador": None}
            continue

        best = max(cand.values(), key=_rank)
        chosen_id, retador = best["scenario"], None
        inc = (incumbents or {}).get(wd) or {}
        inc_id = inc.get("scenario")
        if inc_id and inc_id in cand and inc_id != best["scenario"]:
            # HISTÉRESIS campeón/retador (anti flip-flop entre variantes correlacionadas).
            c_ok, _ = _gate(cand[inc_id], min_n)
            c_est, _ = _estado_supervivencia(sub.xs(inc_id, level="id")["roi"],
                                             w1, w2, min_n_half)
            r_ok, _ = _gate(best, min_n)
            r_est, _ = _estado_supervivencia(sub.xs(best["scenario"], level="id")["roi"],
                                             w1, w2, min_n_half)
            campeon_muerto = (not c_ok) or c_est in ("suspendido", "sin ventaja")
            retador_vivo = r_ok and r_est == "operable"
            domina = retador_vivo and (best.get("wr_p5") or 0) > (cand[inc_id].get("wr_p5") or 0)
            if campeon_muerto and retador_vivo:
                pass                       # promoción inmediata: no se sostiene un campeón muerto
            elif domina:
                prev_rt = inc.get("retador") or {}
                racha = (prev_rt.get("racha", 0) + 1
                         if prev_rt.get("scenario") == best["scenario"] else 1)
                if racha < RETADOR_RACHA:
                    chosen_id = inc_id
                    retador = {"scenario": best["scenario"], "racha": racha,
                               "wr_p5": best.get("wr_p5")}
            else:
                chosen_id = inc_id         # el retador no domina: el campeón sigue, racha a cero

        m = cand[chosen_id]
        ok, reason = _gate(m, min_n)
        # Estado de supervivencia del CAMPEÓN (serie diaria cronológica de cartera).
        serie = sub.xs(chosen_id, level="id")["roi"]
        estado, motivo = _estado_supervivencia(serie, w1, w2, min_n_half)
        row = {**m, "recommendation": "OPERAR" if ok else "NO OPERAR",
               "reason": reason, "estado": estado, "estado_motivo": motivo, "retador": retador}
        _monitor_calibracion(row, serie)
        _monitor_cusum(row, serie)
        per_day[wd] = row

    por_ticker: list = []
    g_tk = _daily(d, ["ticker", "weekday", "id"])
    for (tk, wd), sub in g_tk.groupby(level=["ticker", "weekday"]):
        cand = []
        for sid, gg in sub.groupby(level="id"):
            m = _wmetrics(gg["roi"], gg["w"])
            if m:
                cand.append({"scenario": sid, **m})
        if not cand:
            continue
        best = max(cand, key=_rank)
        ok, _ = _gate(best, min_n)
        por_ticker.append({"Ticker": tk, "Día": wd, "Escenario": best["scenario"],
                           "Win Rate %": best["win_rate"], "ROI %": best["avg_roi"],
                           "Sharpe": best["sharpe"], "n": best["n"],
                           "Recomendación": "OPERAR" if ok else "NO OPERAR"})
    por_ticker.sort(key=lambda r: (r["Ticker"], det._WD_ORDER.index(r["Día"])))
    return per_day, por_ticker


def compute_regime(df, window_dates: list[str], per_day: dict) -> list:
    """FASE 3 (light, INFORMATIVO — no condiciona el gate): para el escenario elegido de cada
    día, parte las sesiones por el RÉGIMEN ex-ante — el md_score del Market Direction Engine a
    la entrada (bajo <45 · medio 45–65 · alto >65, promedio de los tickers del día) — y muestra
    P(ganar | régimen). Con ~6 meses el n por bucket es chico: leer como tendencia, no como gate."""
    import numpy as np
    import pandas as pd
    from bt_analysis import detailed as det

    d = det.prepare(df[df["fecha"].astype(str).str[:10].isin(window_dates)].copy())
    if d.empty or "md_score" not in d.columns:
        return []
    d["md_score"] = pd.to_numeric(d["md_score"], errors="coerce")
    d = d.dropna(subset=["md_score"])
    if d.empty:
        return []
    d["_fstr"] = d["date"].dt.strftime("%Y-%m-%d")
    out = []
    for wd, info in (per_day or {}).items():
        sid = info.get("scenario")
        if not sid:
            continue
        sub = d[(d["weekday"] == wd) & (d["id"] == sid)]
        if sub.empty:
            continue
        g = sub.groupby("_fstr").agg(pnl=("pnl", "sum"), inv=("inv", "sum"),
                                     score=("md_score", "mean"))
        g["roi"] = g["pnl"] / g["inv"].replace(0, np.nan) * 100.0
        g = g.dropna(subset=["roi"])
        g["bucket"] = pd.cut(g["score"], bins=[-1, 45, 65, 101],
                             labels=["bajo (<45)", "medio (45–65)", "alto (>65)"])
        for b, gg in g.groupby("bucket", observed=True):
            if not len(gg):
                continue
            out.append({"Día": wd, "Escenario": sid, "Régimen (md_score)": str(b),
                        "n": int(len(gg)), "WR %": round(float((gg["roi"] > 0).mean() * 100), 1),
                        "ROI prom %": round(float(gg["roi"].mean()), 2)})
    return out


def build_from_store(*, window_days: int = 120, half_life: int = 35, min_n: int = 16,
                     combination: str | None = None) -> dict:
    """Playbook INCREMENTAL: agrega sobre el almacén (bt_store) los últimos `window_days` días
    hábiles de la COMBINACIÓN (default: la activa) con decaimiento exponencial → mismo dict que
    build_from_results + metadatos del modo incremental. Las condiciones salen de
    combinations.db — el template estático ya no participa.

    Política oficial (2026-07-03): ventana 120 días hábiles · half-life 35 (W/HL ≥ 3) · min_n 16.
    Aplica histéresis campeón/retador (incumbents del playbook anterior, SOLO si es de la misma
    combinación) y los monitores de vigencia: calibración y CUSUM (en compute_weighted_verdict),
    churn de veredicto (history) y cambio de régimen de vol (vol 5d de SPY vs mediana 60d)."""
    import bt_store
    import combinations as _comb
    from trade_plan import scenario_config_summary

    combination = combination or _comb.active_combination() or _comb.LEGACY_ID
    _c = _comb.get_combination(combination) or {}
    dates = bt_store.distinct_dates(combination)
    if not dates:
        raise ValueError(f"El almacén no tiene historia para la combinación "
                         f"«{_c.get('nombre') or combination}» — corré una Reevaluación desde "
                         "la página Playbook (o update_playbook.py) para poblarla.")
    win = dates[-window_days:]
    df = bt_store.load_range(win[0], win[-1], combination=combination)
    # Histéresis: los campeones del playbook anterior solo valen si son de ESTA combinación
    # (C001 de otra combinación es otro escenario — no puede ser incumbent).
    _prev = load_playbook()
    if _prev and _prev.get("combination") not in (None, combination):
        _prev = None
    per_day, por_ticker = compute_weighted_verdict(df, win, half_life=half_life, min_n=min_n,
                                                   incumbents=_incumbents_from(_prev))
    try:
        regimen = compute_regime(df, win, per_day)
    except Exception:  # noqa: BLE001 — el régimen es informativo, nunca rompe el build
        regimen = []

    # Monitor churn de veredicto: fragilidad de la selección a lo largo de las re-agregaciones.
    # SOLO snapshots de ESTA combinación — mezclar universos inventa churn (bug 2026-07-08).
    churn = compute_churn(_load_history_snapshots(combination=combination), per_day)
    for wd, ch in churn.items():
        i = per_day.get(wd)
        if not i:
            continue
        i["churn"] = ch
        if ch.get("alerta") and i.get("recommendation") == "OPERAR" \
                and i.get("estado") != "suspendido":
            i["estado"] = "suspendido"
            i["estado_motivo"] = (f"churn de veredicto: el campeón cambió en el "
                                  f"{int(ch['tasa'] * 100)}% de las últimas {ch['n']} "
                                  "re-agregaciones (>30%) — selección frágil")
            i["recommendation"] = "NO OPERAR"
            i["reason"] = i["estado_motivo"]

    regimen_vol = compute_vol_regime(dates)

    # Condiciones por escenario — desde la COMBINACIÓN (fuente única en combinations.db).
    try:
        cfgs = _comb.scenario_configs(combination)
    except Exception:  # noqa: BLE001 — sin condiciones: el playbook queda con IDs solamente
        cfgs = {}
    for i in per_day.values():
        cfg = cfgs.get(str(i.get("scenario") or "").strip())
        i["config"] = cfg
        i["config_txt"] = scenario_config_summary(cfg)

    cov = bt_store.coverage(combination)
    return {
        "evaluado_desde": win[0], "evaluado_hasta": win[-1],
        "tickers": ", ".join(cov.get("tickers") or []),
        "generado_en": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "results_file": f"bt_results.db ({cov['filas']:,} filas)",
        "combination": combination, "combination_nombre": _c.get("nombre") or combination,
        "n_escenarios_universo": int(_c.get("n_escenarios") or 0),
        "n_dias": len(win), "n_posiciones": int(len(df)),
        "modo": (f"incremental · combinación «{_c.get('nombre') or combination}» · ventana "
                 f"{len(win)} días hábiles · half-life {half_life}d · min_n {min_n}"),
        "ventana_dias": window_days, "half_life": half_life, "min_n": min_n,
        "mitades": {"vieja": [win[0], win[len(win) // 2 - 1]] if len(win) > 1 else [win[0]] * 2,
                    "reciente": [win[len(win) // 2], win[-1]]},
        "cobertura": {"desde": cov["desde"], "hasta": cov["hasta"], "dias": cov["dias"]},
        "per_day": per_day, "por_ticker": por_ticker, "regimen": regimen,
        "regimen_vol": regimen_vol,
        "revision_anticipada": bool(any(i.get("cusum_alerta") for i in per_day.values())),
    }


def append_history(pb: dict, path: Path = HISTORY_PATH) -> None:
    """Snapshot COMPACTO del veredicto a un JSONL (auditoría: cómo evolucionan los veredictos
    día a día — el dato que antes se perdía en cada reevaluación)."""
    snap = {"generado_en": pb.get("generado_en"),
            "combination": pb.get("combination"),   # segrega el churn por combinación
            "evaluado": [pb.get("evaluado_desde"), pb.get("evaluado_hasta")],
            "modo": pb.get("modo", "manual"),
            "per_day": {d: {k: i.get(k) for k in ("scenario", "recommendation", "win_rate",
                                                  "wr_p5", "avg_roi", "sharpe", "n", "estado")}
                        for d, i in (pb.get("per_day") or {}).items()}}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")


# ── Reevaluación en background (consola aparte, mismo patrón que el puente) ──
def launch_reevaluation(fecha_ini: str, fecha_fin: str, tickers: list[str],
                        combination: str | None = None) -> dict:
    """Lanza run_ucbatch.py en modo COMBINACIÓN en una CONSOLA NUEVA (async, multiproceso — no
    bloquea la app): corre los escenarios de la combinación (default: la activa) sobre el
    rango/tickers dados. Persiste el job en data/playbook_job.json para sobrevivir
    refresh/reinicio. Devuelve el dict del job."""
    import combinations as _comb
    from ucbatch import runner as _ucrun

    combination = combination or _comb.active_combination()
    if not combination:
        raise ValueError("No hay combinación ACTIVA — importá/generá una en «Combinaciones de "
                         "Backtesting» y activala.")
    c = _comb.get_combination(combination)
    if not c or c.get("estado") != "generada" or not c.get("n_escenarios"):
        raise ValueError(f"La combinación «{(c or {}).get('nombre') or combination}» no tiene "
                         "escenarios generados — tocá «Generar escenarios» primero.")

    import bt_store

    job_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    seed = _comb.seed_for(combination, fecha_inicial=str(fecha_ini), fecha_final=str(fecha_fin),
                          tickers=list(tickers))
    n_days = len(_ucrun.trading_days(seed.fecha_inicial, seed.fecha_final))

    # PERSISTENCIA DIRECTA: el batch ingesta al almacén (sin results xlsx — sin techo de 1M de
    # filas) y la ejecución queda registrada en reeval_runs. La fila se crea ACÁ ('corriendo')
    # para que la UI la vea al instante, antes de que el subproceso termine de bootear.
    bt_store.record_run_start(job_id, combination=combination,
                              combination_nombre=c.get("nombre") or combination,
                              tipo="reevaluacion", fecha_desde=str(fecha_ini),
                              fecha_hasta=str(fecha_fin), tickers=",".join(tickers),
                              n_escenarios=int(c.get("n_escenarios") or 0), n_dias=n_days)
    cmd = [sys.executable, str(HERE / "run_ucbatch.py"),
           "--combination", combination, "--desde", str(fecha_ini), "--hasta", str(fecha_fin),
           "--tickers", ",".join(tickers), "--ingest", "--run-id", job_id,
           "--run-tipo", "reevaluacion"]
    subprocess.Popen(cmd, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                     cwd=str(HERE))
    JOB_PATH.unlink(missing_ok=True)           # legacy: el json transitorio ya no se usa
    return {"id": job_id, "started": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "combination": combination, "combination_nombre": c.get("nombre") or combination,
            "rango": [str(fecha_ini), str(fecha_fin)], "tickers": list(tickers),
            "n_dias": n_days, "n_escenarios": int(c.get("n_escenarios") or 0)}


def check_job():
    """(estado, job) desde la tabla reeval_runs: 'running' si la ÚLTIMA reevaluación está
    corriendo, 'done' si terminó exitosa y falta finalizar (reconstruir el veredicto),
    'finalizing' si el finalizador asíncrono está interpretando, 'failed' si terminó
    fallida/abortada (o el finalize falló) sin descartar. None = nada pendiente.
    El dict conserva las claves que usa la UI (rango, n_dias, n_escenarios, nombre…)."""
    import bt_store

    bt_store.mark_stale_running()              # corridas huérfanas (proceso muerto) → abortada
    bt_store.revive_stale_finalizing()         # finalizadores muertos → vuelven a 'exitosa' (retry)
    r = bt_store.latest_run(tipo="reevaluacion")
    if not r or r.get("finalizado"):
        return None, None
    job = {"id": r["run_id"], "started": (r.get("started_at") or "")[:16],
           "combination": r["combination"],
           "combination_nombre": r.get("combination_nombre") or r["combination"],
           "rango": [r.get("fecha_desde"), r.get("fecha_hasta")],
           "tickers": (r.get("tickers") or "").split(","),
           "n_dias": r.get("n_dias"), "n_escenarios": r.get("n_escenarios"),
           "estado": r["estado"], "error": r.get("error_msg"),
           "n_filas_nuevas": r.get("n_filas_nuevas")}
    if r["estado"] == "corriendo":
        return "running", job
    if r["estado"] == "finalizando":
        return "finalizing", job
    if r["estado"] == "exitosa":
        if str(r.get("error_msg") or "").startswith("finalize:"):
            return "failed", job               # el finalize falló → mostrar y poder descartar
        return "done", job
    return "failed", job                       # fallida | abortada, pendiente de descartar


def _do_finalize(job: dict) -> dict:
    """El trabajo real de finalizar una reevaluación. Con combinaciones gigantes tarda MINUTOS
    (millones de filas) → por eso la página lo delega a spawn_finalize y nunca se bloquea.
    No toca estados de reeval_runs.

    · Si el run es de la combinación ACTIVA: reconstruye el veredicto POR RANGO del run
      (la semántica histórica de la reevaluación) → playbook.json + history, y además
      refresca su json segregado con el veredicto OFICIAL (ventana 120).
    · Si NO es la activa (las combinaciones pueden rotar con el run en vuelo): NO toca
      playbook.json ni history — solo refresca el json segregado de SU combinación (su
      tarjeta). Antes pisaba el vigente incondicionalmente (bug 2026-07-06: la finalización
      de un run viejo de la 12.288 dejó playbook.json apuntando a una combinación no activa)."""
    import bt_store

    _combo = job.get("combination") or bt_store.LEGACY_COMBINATION
    _es_activa = False
    try:
        import combinations as _cmb_fin
        _es_activa = (_cmb_fin.active_combination() == _combo)
    except Exception:  # noqa: BLE001 — sin módulo de combinaciones, conducta legacy (activa)
        _es_activa = True

    if _es_activa:
        rdf = bt_store.load_range(job["rango"][0], job["rango"][1], combination=_combo)
        pb = build_from_df(rdf, "detailed", combination=_combo,
                           results_ref=f"almacén:{job['id']}")
        save_playbook(pb)
        append_history(pb)
        pb["_filas_interpretadas"] = int(len(rdf))
        try:
            build_and_save_for(_combo)      # su tarjeta también queda al día (oficial 120)
        except Exception:  # noqa: BLE001 — el segregado nunca tumba la finalización
            pass
    else:
        pb = build_and_save_for(_combo)     # solo SU json segregado, con semántica oficial
        pb["_filas_interpretadas"] = None
    pb["_ingestado_al_almacen"] = job.get("n_filas_nuevas")
    return pb


def finalize_job() -> dict:
    """Camino SÍNCRONO (scripts/headless — la página usa spawn_finalize): finaliza la
    reevaluación pendiente y marca el run."""
    import bt_store

    status, job = check_job()
    if status != "done":
        raise RuntimeError("No hay reevaluación exitosa pendiente de finalizar.")
    pb = _do_finalize(job)
    bt_store.finish_run_finalized(job["id"])
    JOB_PATH.unlink(missing_ok=True)           # legacy: limpiar json viejo si quedó
    return pb


def finalize_run_by_id(run_id: str) -> dict:
    """Worker del finalizador ASÍNCRONO (`update_playbook.py --finalize-run <id>`): hace el
    trabajo y deja el estado final en reeval_runs — finalizado=1 si OK, o error con prefijo
    'finalize:' (la página lo muestra y permite descartar; un finalizador muerto sin excepción
    lo revive revive_stale_finalizing → retry automático)."""
    import bt_store

    r = bt_store.get_run(run_id)
    if not r:
        raise ValueError(f"run {run_id!r} inexistente")
    job = {"id": r["run_id"], "combination": r["combination"],
           "rango": [r.get("fecha_desde"), r.get("fecha_hasta")],
           "n_filas_nuevas": r.get("n_filas_nuevas")}
    try:
        pb = _do_finalize(job)
    except Exception as e:  # noqa: BLE001 — dejar el error visible en la página
        bt_store.record_finalize_error(run_id, f"{type(e).__name__}: {e}")
        raise
    bt_store.finish_run_finalized(run_id)
    return pb


def spawn_finalize(run_id: str) -> bool:
    """Lanza el finalizador en un PROCESO APARTE y devuelve al instante (la página nunca se
    bloquea — interpretar millones de filas tarda minutos). Claim atómico en reeval_runs:
    aunque N sesiones de la página vean el 'done' a la vez, UNA sola lanza el proceso.
    Devuelve True si esta llamada fue la que lo lanzó."""
    import bt_store

    if not bt_store.claim_run_finalizing(run_id):
        return False
    logs = HERE / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / "finalize.log", "ab") as out:
        subprocess.Popen([sys.executable, str(HERE / "update_playbook.py"),
                          "--finalize-run", run_id],
                         cwd=str(HERE), stdout=out, stderr=out,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return True


def display_name(nombre, max_len: int = 45) -> str:
    """Nombre CORTO y distintivo de una combinación para la UI: sin extensión ni el boilerplate
    «Backtesting_variables_template», y truncado POR LA CABEZA si hace falta — lo distintivo de
    los nombres largos vive al FINAL («… - tickers y colectivo»). Acepta el dict del playbook
    (usa combination_nombre) o el string directo."""
    if isinstance(nombre, dict):
        nombre = nombre.get("combination_nombre") or nombre.get("combination") or ""
    s = str(nombre or "").strip() or "Template 480 (legacy)"
    for suf in (".xlsx", ".xlsm"):
        if s.lower().endswith(suf):
            s = s[: -len(suf)]
    pref = "Backtesting_variables_template"
    if s.startswith(pref):
        s = s[len(pref):].lstrip(" -_")
    if len(s) > max_len:
        s = "…" + s[-(max_len - 1):]
    return s


# ── Veredicto PERSISTIDO POR COMBINACIÓN (tarjetas de la página + futuro comparador) ─────
def playbook_path_for(combination_id: str) -> Path:
    """data/playbook_<id>.json — el veredicto oficial de UNA combinación, persistido aparte
    del vigente (playbook.json, que es el de la ACTIVA)."""
    return HERE / "data" / f"playbook_{combination_id}.json"


def load_playbook_for(combination_id: str):
    return load_playbook(playbook_path_for(combination_id))


def promote_active_playbook(combination_id: str) -> bool:
    """Copia el veredicto segregado (playbook_<id>.json) a playbook.json al ACTIVAR una
    combinación: la síntesis vigente y el «(playbook automático)» pasan a reflejar a la nueva
    activa AL INSTANTE, sin esperar al job de las 05:00. NO escribe historia (activar no es
    reevaluar). Si la combinación aún no tiene veredicto calculado, BORRA playbook.json (mostrar
    el veredicto de la combinación anterior bajo la nueva activa sería mentir) y devuelve False."""
    pb = load_playbook_for(combination_id)
    if pb:
        save_playbook(pb)
        return True
    try:
        Path(PB_PATH).unlink(missing_ok=True)
    except OSError:
        pass
    return False


def build_and_save_for(combination_id: str, *, window_days: int = 120, half_life: int = 35,
                       min_n: int = 16) -> dict:
    """Calcula el veredicto OFICIAL (ventana + decaimiento + gates + estados) de UNA combinación
    sobre SU almacén y lo persiste en su json propio. NO toca playbook.json ni la activa.
    Con combinaciones gigantes tarda minutos (millones de filas) — llamarlo desde un botón
    con spinner o desde el job diario, nunca en el page-load."""
    pb = build_from_store(window_days=window_days, half_life=half_life, min_n=min_n,
                          combination=combination_id)
    save_playbook(pb, path=playbook_path_for(combination_id))
    return pb
