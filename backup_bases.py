"""Backup semanal comprimido de las bases SQLite + resultados/ hacia OneDrive.

Lo ejecuta la Tarea Programada "SignalForge Backup Bases" (domingos 12:00; si la
maquina estaba apagada, corre al encenderla). Usa la API de backup de SQLite para
sacar un snapshot consistente aunque la app este escribiendo en ese momento.
Retiene los ultimos KEEP zips y borra los mas viejos.

Restaurar: descomprimir el zip y copiar los .db de vuelta a options_replay/data/
(con la app cerrada); resultados/ va a la raiz del proyecto.
"""
from pathlib import Path
import datetime
import sqlite3
import tempfile
import zipfile

HERE = Path(__file__).resolve().parent                      # Traiding (ruta-independiente)
DEST = Path(r"C:\Users\ROG ZEPHYRUS\OneDrive\Backups SignalForge")
KEEP = 8                                                    # semanas retenidas


def snapshot_db(src_path: Path, dst_path: Path) -> None:
    """Copia consistente via sqlite3 online backup (WAL incluido)."""
    src = sqlite3.connect(str(src_path), timeout=60)
    dst = sqlite3.connect(str(dst_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def main() -> None:
    stamp = datetime.date.today().isoformat()
    DEST.mkdir(parents=True, exist_ok=True)
    zpath = DEST / f"signalforge_bases_{stamp}.zip"

    dbs = sorted((HERE / "options_replay" / "data").glob("*.db"))
    dbs += sorted((HERE / "live_trader").rglob("*.db"))

    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for db in dbs:
                snap = Path(td) / db.name
                snapshot_db(db, snap)
                z.write(snap, f"data/{db.name}")
            res = HERE / "resultados"
            if res.exists():
                for f in sorted(res.rglob("*")):
                    if f.is_file():
                        z.write(f, f"resultados/{f.relative_to(res)}")

    viejos = sorted(DEST.glob("signalforge_bases_*.zip"))[:-KEEP]
    for f in viejos:
        f.unlink()

    mb = zpath.stat().st_size / 1e6
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M} backup OK -> {zpath.name} "
          f"({mb:.1f} MB, {len(dbs)} bases, retencion {KEEP})")


if __name__ == "__main__":
    main()
