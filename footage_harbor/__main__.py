"""Entry point: python3 -m footage_harbor"""
import argparse
import json
import os
import threading

from .cloud import Rclone
from .db import DB
from .devices import make_backend
from .engine import Station
from .server import App, serve


def main():
    ap = argparse.ArgumentParser(prog="footage-harbor")
    ap.add_argument("--data", default=os.environ.get("FH_DATA", "/var/lib/footage-harbor"),
                    help="Ordner für Datenbank und Zugangsdaten")
    ap.add_argument("--host", default=os.environ.get("FH_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("FH_PORT", "80")))
    ap.add_argument("--cards-dir", default=os.environ.get("FH_CARDS_DIR"),
                    help="Entwicklungsmodus: jeder Unterordner gilt als eingesteckte Karte")
    args = ap.parse_args()

    os.makedirs(args.data, exist_ok=True)
    os.chmod(args.data, 0o700)
    mount_root = os.environ.get("FH_MOUNT_ROOT", "/run/footage-harbor") if not args.cards_dir else args.cards_dir
    db = DB(os.path.join(args.data, "harbor.db"))
    cloud = Rclone(args.data)
    cloud.bwlimit = db.get("bwlimit", "")

    def protected():
        paths = [args.data]
        for d in db.q("SELECT config FROM destinations WHERE kind='local'"):
            paths.append(json.loads(d["config"])["path"])
        return paths

    backend = make_backend(args.cards_dir, protected)
    station = Station(db, backend, cloud, args.data, mount_root)
    app = App(db, station, cloud, dev=bool(args.cards_dir))
    threading.Thread(target=station.run_watcher, daemon=True).start()
    httpd = serve(app, args.host, args.port)
    mode = f"Ordner {args.cards_dir}" if args.cards_dir else "Kartenleser"
    print(f"Footage Harbor läuft auf http://{args.host}:{args.port} · Quelle: {mode}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
