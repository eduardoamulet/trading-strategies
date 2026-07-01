"""Vista de la «Simulación Intradía» — builders PUROS de HTML (sin Streamlit).

`build_matrix_html` arma la matriz Activo × minuto con celdas SOLO color (CALL verde · PUT rojo ·
NO TRADE gris) y un tooltip moderno al hacer hover (con el TradeSignal completo). El HTML se mantiene
liviano: cada celda solo lleva `data-tk`/`data-mn`; todos los datos van en UN blob JSON y un único
tooltip flotante los muestra por JS. `legend_html` y `signal_lines` son helpers de presentación.
"""
from __future__ import annotations

import json

ACTION_COLOR = {"CALL": "#16a34a", "PUT": "#dc2626", "NO TRADE": "#9ca3af"}

# CSS/JS constantes (no f-string → sin escapar llaves). El JSON de datos se injecta por replace.
_MATRIX_CSS = """
<style>
.simx-wrap{overflow:auto;max-height:200px;border:1px solid #2a2e39;border-radius:8px;background:#0e1117;}
.simx-wrap table{border-collapse:separate;border-spacing:0;font-family:ui-sans-serif,system-ui;}
.simx-wrap th.simx-corner,.simx-wrap th.simx-tk{position:sticky;left:0;z-index:3;background:#161a23;
  color:#d1d4dc;font-size:11px;font-weight:600;text-align:right;padding:2px 8px;white-space:nowrap;}
.simx-wrap thead th{position:sticky;top:0;z-index:2;background:#161a23;color:#8b93a7;font-size:9px;
  font-weight:500;padding:2px 0;white-space:nowrap;}
.simx-wrap th.simx-corner{z-index:4;top:0;}
.simx-wrap td.simx-c{width:9px;min-width:9px;height:22px;padding:0;cursor:pointer;border:none;}
.simx-wrap td.simx-c:hover{outline:2px solid #facc15;outline-offset:-2px;}
#simx-tt{position:fixed;z-index:99999;display:none;pointer-events:none;max-width:280px;
  background:#1b1f2a;color:#e5e7eb;border:1px solid #333a4d;border-radius:8px;padding:10px 12px;
  font-family:ui-sans-serif,system-ui;font-size:12px;box-shadow:0 8px 24px rgba(0,0,0,.5);}
#simx-tt .h{font-size:14px;font-weight:700;margin-bottom:4px;}
#simx-tt .g{display:grid;grid-template-columns:auto auto;gap:1px 10px;margin:5px 0;}
#simx-tt .k{color:#8b93a7;} #simx-tt .v{text-align:right;font-weight:600;}
#simx-tt .r{color:#cbd5e1;margin-top:4px;} #simx-tt .r div{margin:1px 0;}
</style>
"""

_MATRIX_JS = """
<script>
(function(){
  const DATA = /*DATA*/;
  const COL = {"CALL":"#16a34a","PUT":"#dc2626","NO TRADE":"#9ca3af"};
  const tt = document.getElementById('simx-tt');
  function pct(x){ return x==null? '—' : Math.round(x*100)+'%'; }
  function num(x){ return x==null? '—' : x; }
  function rr(x){ return x==null? '—' : ('1:'+x); }
  function show(tk, mn, ev){
    const s = (DATA[tk]||{})[mn]; if(!s){ return; }
    const rs = (s.rs||[]).map(function(r){ return '<div>✓ '+r+'</div>'; }).join('');
    tt.innerHTML =
      '<div class="h" style="color:'+(COL[s.a]||'#9ca3af')+'">'+tk+' · '+mn+' · '+s.a+'</div>'+
      '<div class="g">'+
      '<span class="k">Confianza</span><span class="v">'+pct(s.c)+'</span>'+
      '<span class="k">Score</span><span class="v">'+num(s.s)+'</span>'+
      '<span class="k">Market Strength</span><span class="v">'+num(s.ms)+'</span>'+
      '<span class="k">Trend</span><span class="v">'+num(s.tr)+'</span>'+
      '<span class="k">Entry</span><span class="v">'+num(s.ep)+'</span>'+
      '<span class="k">Stop</span><span class="v">'+num(s.sl)+'</span>'+
      '<span class="k">Target</span><span class="v">'+num(s.tg)+'</span>'+
      '<span class="k">Risk Reward</span><span class="v">'+rr(s.rr)+'</span>'+
      '</div>'+ (rs? '<div class="r">'+rs+'</div>' : '');
    tt.style.display='block';
    let x=ev.clientX+14, y=ev.clientY+14;
    if(x+290>window.innerWidth){ x=ev.clientX-294; }
    if(y+220>window.innerHeight){ y=Math.max(8, ev.clientY-224); }
    tt.style.left=x+'px'; tt.style.top=y+'px';
  }
  document.querySelectorAll('td.simx-c').forEach(function(td){
    td.addEventListener('mousemove', function(ev){ show(td.dataset.tk, td.dataset.mn, ev); });
    td.addEventListener('mouseleave', function(){ tt.style.display='none'; });
    // Click en la celda → setea ?sim_cell=TICKER|MINUTO en la ventana top (con nonce _sn para que
    // reclickear la MISMA celda también dispare). pushState+popstate = rerun SUAVE de la página;
    // si el iframe fuese cross-origin, cae a location.search (recarga, igual funciona).
    td.addEventListener('click', function(){
      var v = td.dataset.tk + '|' + td.dataset.mn;
      try {
        var w = window.top;
        var u = new URL(w.location.href);
        u.searchParams.set('sim_cell', v);
        u.searchParams.set('_sn', String(Date.now()));
        w.history.pushState({}, '', u.toString());
        w.dispatchEvent(new PopStateEvent('popstate'));
      } catch(e) {
        window.top.location.search = '?sim_cell=' + encodeURIComponent(v) + '&_sn=' + Date.now();
      }
    });
})();
</script>
"""


def build_matrix_html(sim: dict) -> tuple[str, int]:
    """Devuelve (html, alto_px) de la matriz Activo×minuto (solo color + tooltip en hover)."""
    minutes = sim.get("minutes", [])
    tickers = sim.get("tickers", [])
    results = sim.get("results", {})

    # Datos compactos para el tooltip (un blob JSON; las celdas solo referencian tk/mn).
    data: dict = {}
    for tk in tickers:
        d = {}
        for mn, sig in results.get(tk, {}).items():
            d[mn] = {"a": sig.get("action"), "c": sig.get("confidence"), "s": sig.get("score"),
                     "ms": sig.get("market_strength"), "tr": sig.get("trend"),
                     "ep": sig.get("entry_price"), "sl": sig.get("stop"), "tg": sig.get("target"),
                     "rr": sig.get("risk_reward"), "rs": sig.get("reasons") or []}
        data[tk] = d

    # Encabezado: etiqueta de minuto solo en los múltiplos de 15 (si no, ilegible con 390 columnas).
    head_cells = "".join(
        f'<th>{mn if mn.endswith((":00", ":15", ":30", ":45")) else ""}</th>' for mn in minutes)
    head = f'<thead><tr><th class="simx-corner">Activo \\ Hora</th>{head_cells}</tr></thead>'

    body_rows = ""
    for tk in tickers:
        cells = ""
        for mn in minutes:
            act = results.get(tk, {}).get(mn, {}).get("action", "NO TRADE")
            color = ACTION_COLOR.get(act, "#9ca3af")
            cells += f'<td class="simx-c" style="background:{color}" data-tk="{tk}" data-mn="{mn}"></td>'
        body_rows += f'<tr><th class="simx-tk">{tk}</th>{cells}</tr>'

    table = f'<div class="simx-wrap"><table>{head}<tbody>{body_rows}</tbody></table></div>'
    js = _MATRIX_JS.replace("/*DATA*/", json.dumps(data))
    html = _MATRIX_CSS + table + '<div id="simx-tt"></div>' + js
    # Alto del iframe = grid visible + espacio para el tooltip flotante (el iframe recorta lo que
    # sobresale; el JS reposiciona el tooltip para que quede dentro de este alto).
    grid_h = min(200, (len(tickers) + 1) * 26 + 40)
    height = grid_h + 190
    return html, height


def legend_html() -> str:
    """Leyenda 🟢 CALL · 🔴 PUT · ⚪ NO TRADE."""
    return (
        '<div style="display:flex;gap:16px;align-items:center;font-size:13px;margin:2px 0 6px 0;">'
        '<span><span style="color:#16a34a;font-size:15px;">■</span> CALL</span>'
        '<span><span style="color:#dc2626;font-size:15px;">■</span> PUT</span>'
        '<span><span style="color:#9ca3af;font-size:15px;">■</span> NO TRADE</span>'
        '</div>'
    )


def signal_lines(sig: dict) -> list[tuple[str, str]]:
    """(etiqueta, valor) del TradeSignal para el panel lateral. Puro; la UI decide cómo renderizar."""
    def _n(x):
        return "—" if x is None else x
    rr = sig.get("risk_reward")
    return [
        ("Confianza", f"{(sig.get('confidence') or 0) * 100:.0f}%"),
        ("Score", f"{_n(sig.get('score'))}"),
        ("Market Strength", f"{_n(sig.get('market_strength'))}"),
        ("Trend", f"{_n(sig.get('trend'))}"),
        ("Entry Price", f"{_n(sig.get('entry_price'))}"),
        ("Stop", f"{_n(sig.get('stop'))}"),
        ("Target", f"{_n(sig.get('target'))}"),
        ("Risk Reward", "—" if rr is None else f"1:{rr}"),
    ]
