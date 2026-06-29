"""Corre el batch en un proceso AISLADO (multiproceso real). Existe porque `multiprocessing` lanzado
DESDE Streamlit en Windows se cuelga (los hijos re-importan el __main__ de Streamlit). Acá el __main__
es este CLI (guard limpio) → mp.Pool funciona, igual que en un script normal.

Uso:  python batch_cli.py <request.pkl> <result.json> <progress.json>
  request.pkl   pickle de {configs, tickers, dates, tipo, data_dir, api_key, processes?}
  progress.json se va actualizando: {"done": N, "total": M}
  result.json   al terminar: {"ok": true, "rows": [...]}  |  {"ok": false, "error": "..."}
"""
import json
import os
import pickle
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))


def _atomic_write_json(path, obj):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def main(req_path, result_path, progress_path):
    import batch_runner as br
    with open(req_path, "rb") as f:
        req = pickle.load(f)
    total = len(req["configs"]) * max(1, len(req["tickers"])) * max(1, len(req["dates"]))
    _atomic_write_json(progress_path, {"done": 0, "total": total})

    def _cb(done, tot):
        _atomic_write_json(progress_path, {"done": int(done), "total": int(tot)})

    try:
        rows = br.run_batch_parallel(req["data_dir"], req["api_key"], req["configs"],
                                     req["tickers"], req["dates"], req["tipo"],
                                     progress_cb=_cb, processes=req.get("processes"))
        _atomic_write_json(result_path, {"ok": True, "rows": rows})
    except Exception as e:   # noqa: BLE001
        import traceback
        _atomic_write_json(result_path, {"ok": False, "error": str(e), "trace": traceback.format_exc()})


if __name__ == "__main__":
    import multiprocessing as mp
    mp.freeze_support()
    main(sys.argv[1], sys.argv[2], sys.argv[3])
