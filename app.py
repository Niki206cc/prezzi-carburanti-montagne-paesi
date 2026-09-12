import csv
import html
import io
import json
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, url_for
from PIL import Image, ImageDraw, ImageFont

VERSION = "1.1.0"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
BASE_IMAGE = DATA_DIR / "carburanti-base.jpg"
LOG_FILE = DATA_DIR / "app.log"
TZ = ZoneInfo(os.getenv("TZ", "Europe/Rome"))
ANAG_URL = "https://www.mimit.gov.it/images/exportCSV/anagrafica_impianti_attivi.csv"
PRICE_URL = "https://www.mimit.gov.it/images/exportCSV/prezzo_alle_8.csv"
SOURCE_URL = "https://www.mimit.gov.it/it/open-data/elenco-dataset/carburanti-prezzi-praticati-e-anagrafica-degli-impianti"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s",
                    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()])
log = logging.getLogger("carburanti")
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "cambia-questa-chiave")
publish_lock = threading.Lock()

FUELS = [
    ("Benzina", "Benzina", True),
    ("Gasolio", "Diesel", True),
    ("GPL", "GPL", False),
    ("Metano", "Metano", False),
]
PROVINCES = [("BG", "Bergamo"), ("BS", "Brescia")]


def env_bool(name, default=False):
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


def load_state():
    state = {
        "automation_enabled": env_bool("AUTOMATION_ENABLED", False),
        "publish_time": os.getenv("PUBLISH_TIME", "09:30"),
        "category_id": int(os.getenv("WP_CATEGORY_ID", "1869") or 1869),
        "top_count": int(os.getenv("TOP_COUNT", "5") or 5),
        "max_age_days": int(os.getenv("MAX_PRICE_AGE_DAYS", "8") or 8),
        "last_publish": None, "last_url": None, "last_error": None,
        "image_x": 50, "image_y": 73, "font_size": 62,
        "text_color": "#ffffff", "stroke_color": "#000000", "stroke_width": 2,
    }
    if STATE_FILE.exists():
        try:
            state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception:
            log.exception("Stato non leggibile")
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def download_csv(url):
    response = requests.get(url, timeout=90, headers={"User-Agent": "MontagnePaesiCarburanti/1.0"})
    response.raise_for_status()
    text = response.content.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    if len(lines) < 3 or not lines[0].startswith("Estrazione del "):
        raise RuntimeError("Formato del file MIMIT non riconosciuto")
    extraction = datetime.strptime(lines[0].replace("Estrazione del ", "").strip(), "%Y-%m-%d").date()
    return extraction, csv.DictReader(io.StringIO("\n".join(lines[1:])), delimiter="|")


def fetch_data():
    anag_date, anag_rows = download_csv(ANAG_URL)
    stations = {}
    for row in anag_rows:
        province = row.get("Provincia", "").strip().upper()
        if province not in {"BG", "BS"}:
            continue
        stations[row["idImpianto"].strip()] = {
            "id": row["idImpianto"].strip(), "manager": row.get("Gestore", "").strip(),
            "brand": row.get("Bandiera", "").strip(), "name": row.get("Nome Impianto", "").strip(),
            "address": row.get("Indirizzo", "").strip(), "city": row.get("Comune", "").strip().title(),
            "province": province, "lat": row.get("Latitudine", "").strip(), "lon": row.get("Longitudine", "").strip(),
        }
    price_date, price_rows = download_csv(PRICE_URL)
    state = load_state()
    cutoff = datetime.now(TZ).replace(tzinfo=None) - timedelta(days=state["max_age_days"])
    records = []
    accepted = {x[0].lower(): x for x in FUELS}
    for row in price_rows:
        station = stations.get(row.get("idImpianto", "").strip())
        fuel = row.get("descCarburante", "").strip()
        if not station or fuel.lower() not in accepted:
            continue
        try:
            updated = datetime.strptime(row["dtComu"].strip(), "%d/%m/%Y %H:%M:%S")
            price = float(row["prezzo"].replace(",", "."))
        except (ValueError, KeyError):
            continue
        if updated < cutoff or price <= 0:
            continue
        records.append(station | {"fuel": accepted[fuel.lower()][0], "price": price,
                                  "self": row.get("isSelf", "0").strip() == "1", "updated": updated})
    if not records:
        raise RuntimeError("Nessun prezzo valido trovato per Bergamo e Brescia")
    return {"extraction_date": min(anag_date, price_date), "records": records, "stations": len(stations)}


def rankings(dataset):
    state = load_state()
    result = {}
    for province, _ in PROVINCES:
        result[province] = {}
        for fuel, _, prefer_self in FUELS:
            candidates = [r for r in dataset["records"] if r["province"] == province and r["fuel"] == fuel]
            if prefer_self:
                candidates = [r for r in candidates if r["self"]]
            candidates.sort(key=lambda r: (r["price"], -r["updated"].timestamp()))
            result[province][fuel] = candidates[:state["top_count"]]
    return result


def maps_url(row):
    try:
        lat, lon = float(row["lat"]), float(row["lon"])
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
    except (ValueError, TypeError):
        pass
    query = " ".join(filter(None, [row["name"], row["brand"], row["address"], row["city"], row["province"]]))
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(query)


def euro(value):
    return f"{value:.3f}".replace(".", ",")


def build_title(dataset, day):
    benzina = [r["price"] for r in dataset["records"] if r["fuel"] == "Benzina" and r["self"]]
    diesel = [r["price"] for r in dataset["records"] if r["fuel"] == "Gasolio" and r["self"]]
    if not benzina or not diesel:
        raise RuntimeError("Prezzo minimo di benzina o diesel non disponibile")
    return (f"Benzina da {euro(min(benzina))} € e diesel da {euro(min(diesel))} € "
            f"oggi a Bergamo e Brescia: prezzi del {day.strftime('%d/%m/%Y')}")


def build_article(dataset, day):
    ranked = rankings(dataset)
    date_text = day.strftime("%d/%m/%Y")
    parts = [
        f"<p><strong>Prezzi di benzina e diesel oggi, {date_text}, nelle province di Bergamo e Brescia.</strong> "
        "Ecco i distributori più economici rilevati nei dati ufficiali del Ministero delle Imprese e del Made in Italy, con il collegamento diretto a Google Maps.</p>",
        "<p>Le classifiche distinguono benzina e diesel in modalità self-service. Per GPL e metano vengono considerati gli impianti disponibili, dove il rifornimento è normalmente servito.</p>",
    ]
    for province, province_name in PROVINCES:
        parts.append(
            '<div style="margin:28px 0 18px;padding:14px 18px;background:#17354f;color:#ffffff;border-radius:6px;">'
            f'<p style="margin:0;font-size:22px;"><strong>PROVINCIA DI {province_name.upper()}</strong></p></div>'
        )
        for fuel, label, prefer_self in FUELS:
            rows = ranked[province][fuel]
            parts.append(
                '<div style="margin:16px 0;padding:14px 16px;border-left:5px solid #18a999;background:#f3f6f8;">'
                f"<p style=\"margin:0 0 10px;\"><strong>{label}{' self-service' if prefer_self else ''}</strong></p>"
            )
            if not rows:
                parts.append("<p>Nessun prezzo sufficientemente aggiornato disponibile.</p>")
            for index, row in enumerate(rows, 1):
                station_name = row["name"] or row["brand"] or row["manager"] or "Distributore"
                mode = "self" if row["self"] else "servito"
                link = html.escape(maps_url(row), quote=True)
                parts.append(
                    f"<strong>{index}. {html.escape(station_name)}</strong> – {html.escape(row['city'])}<br>"
                    f"Prezzo: <strong>{euro(row['price'])} €/litro</strong> ({mode})<br>"
                    f"{html.escape(row['address'])}<br>"
                    f"Aggiornato il {row['updated'].strftime('%d/%m/%Y alle %H:%M')} – "
                    f'<a href="{link}" target="_blank" rel="noopener noreferrer">📍 Apri su Google Maps</a>'
                    f"{'<br><br>' if index < len(rows) else ''}"
                )
            parts.append("</div>")
    source_date = dataset["extraction_date"].strftime("%d/%m/%Y")
    parts.append(
        f'<p><small>Fonte: <a href="{SOURCE_URL}" target="_blank" rel="noopener noreferrer">Ministero delle Imprese e del Made in Italy</a>, '
        f"open data con licenza IODL 2.0, estrazione del {source_date}. I prezzi sono comunicati dai gestori e possono variare: "
        "prima del rifornimento verificare sempre il prezzo esposto presso l’impianto.</small></p>"
    )
    return "\n".join(parts), ranked


def font_for(size):
    for path in (os.getenv("IMAGE_FONT", ""), "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if path and Path(path).exists(): return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def render_image(dataset, day):
    state = load_state()
    image = Image.open(BASE_IMAGE).convert("RGB")
    ranked = rankings(dataset)
    cheapest = [r[0] for p in ranked.values() for fuel, rows in p.items() if fuel == "Benzina" and (r := rows)]
    line = f"{day.strftime('%d/%m/%Y')}"
    if cheapest: line += f"  •  Benzina da {euro(min(x['price'] for x in cheapest))} €/l"
    draw, font = ImageDraw.Draw(image), font_for(int(state["font_size"]))
    sw = int(state["stroke_width"])
    box = draw.textbbox((0, 0), line, font=font, stroke_width=sw)
    w, h = box[2] - box[0], box[3] - box[1]
    x = image.width * float(state["image_x"]) / 100 - w / 2
    y = image.height * float(state["image_y"]) / 100 - h / 2
    draw.text((x, y), line, font=font, fill=state["text_color"], stroke_fill=state["stroke_color"], stroke_width=sw)
    output = io.BytesIO(); image.save(output, "JPEG", quality=94, optimize=True); output.seek(0)
    return output


def wp_base(): return os.getenv("WP_URL", "").rstrip("/") + "/wp-json/wp/v2"
def wp_auth(): return os.getenv("WP_USERNAME", ""), os.getenv("WP_APP_PASSWORD", "")
def wp_headers(): return {"User-Agent": "MontagnePaesiCarburanti/1.0"}


def validate_wp():
    missing = [k for k in ("WP_URL", "WP_USERNAME", "WP_APP_PASSWORD") if not os.getenv(k)]
    if missing: raise RuntimeError("Configurazione mancante: " + ", ".join(missing))


def duplicate_exists(title):
    response = requests.get(f"{wp_base()}/posts", params={"search": title, "per_page": 20, "status": "publish,draft,future"},
                            auth=wp_auth(), headers=wp_headers(), timeout=30)
    response.raise_for_status()
    target = title.strip().lower()
    return any(html.unescape(x.get("title", {}).get("rendered", "")).strip().lower() == target for x in response.json())


def upload_image(dataset, day):
    if not BASE_IMAGE.exists(): return 0
    filename = f"prezzi-carburanti-{day.strftime('%Y-%m-%d')}.jpg"
    headers = wp_headers() | {"Content-Disposition": f'attachment; filename="{filename}"', "Content-Type": "image/jpeg"}
    response = requests.post(f"{wp_base()}/media", data=render_image(dataset, day).read(), auth=wp_auth(), headers=headers, timeout=60)
    response.raise_for_status(); media = response.json()
    requests.post(f"{wp_base()}/media/{media['id']}", auth=wp_auth(), headers=wp_headers(), timeout=30,
                  json={"alt_text": f"Prezzi carburanti a Bergamo e Brescia del {day.strftime('%d/%m/%Y')}"}).raise_for_status()
    return media["id"]


def publish():
    with publish_lock:
        day = datetime.now(TZ)
        state = load_state()
        try:
            validate_wp()
            dataset = fetch_data()
            title = build_title(dataset, day)
            if duplicate_exists(title): return {"message": "Articolo già presente: nessun duplicato creato"}
            content, _ = build_article(dataset, day); media_id = upload_image(dataset, day)
            post = {"title": title, "content": content, "status": os.getenv("WP_POST_STATUS", "draft"),
                    "categories": [int(state["category_id"])]}
            if media_id: post["featured_media"] = media_id
            if os.getenv("WP_AUTHOR_ID", "").isdigit(): post["author"] = int(os.getenv("WP_AUTHOR_ID"))
            response = requests.post(f"{wp_base()}/posts", json=post, auth=wp_auth(), headers=wp_headers(), timeout=60)
            response.raise_for_status(); result = response.json()
            state.update({"last_publish": day.isoformat(), "last_url": result.get("link"), "last_error": None}); save_state(state)
            return {"message": "Articolo inviato correttamente a WordPress", "url": result.get("link")}
        except Exception as exc:
            state["last_error"] = str(exc); save_state(state); log.exception("Pubblicazione fallita"); raise


def scheduled_job():
    if load_state()["automation_enabled"]:
        try: publish()
        except Exception: pass


@app.get("/")
def dashboard():
    state = load_state(); configured = all(os.getenv(x) for x in ("WP_URL", "WP_USERNAME", "WP_APP_PASSWORD"))
    return render_template("index.html", version=VERSION, state=state, configured=configured,
                           has_image=BASE_IMAGE.exists(), now=datetime.now(TZ))


@app.get("/preview")
def preview():
    try:
        data = fetch_data(); day = datetime.now(TZ); content, ranked = build_article(data, day)
        return render_template("preview.html", title=build_title(data, day), content=content)
    except Exception as exc:
        flash(f"Anteprima non disponibile: {exc}", "error"); return redirect(url_for("dashboard"))


@app.post("/settings")
def settings():
    state = load_state()
    try:
        parsed = datetime.strptime(request.form["publish_time"], "%H:%M")
        category = int(request.form["category_id"])
        if category <= 0: raise ValueError("ID categoria non valido")
        state.update({"publish_time": request.form["publish_time"], "category_id": category,
                      "top_count": max(1, min(10, int(request.form["top_count"])))})
        save_state(state)
        scheduler.reschedule_job("daily_fuel", trigger="cron", day_of_week="mon-sun", hour=parsed.hour, minute=parsed.minute, timezone=TZ)
        flash("Impostazioni salvate e orario aggiornato", "success")
    except Exception as exc: flash(f"Impostazioni non valide: {exc}", "error")
    return redirect(url_for("dashboard"))


@app.post("/image")
def image_upload():
    uploaded = request.files.get("image")
    try:
        if not uploaded or not uploaded.filename: raise ValueError("Seleziona un'immagine")
        image = Image.open(uploaded.stream).convert("RGB")
        if image.width < 600 or image.height < 315: raise ValueError("Dimensioni minime 600 × 315 pixel")
        image.save(BASE_IMAGE, "JPEG", quality=95); flash("Immagine base salvata", "success")
    except Exception as exc: flash(f"Immagine non valida: {exc}", "error")
    return redirect(url_for("dashboard"))


@app.get("/image-preview")
def image_preview():
    if not BASE_IMAGE.exists(): return "Immagine non caricata", 404
    try: return send_file(render_image(fetch_data(), datetime.now(TZ)), mimetype="image/jpeg")
    except Exception as exc: return str(exc), 500


@app.post("/image-settings")
def image_settings():
    state = load_state()
    try:
        state.update({"image_x": max(0, min(100, float(request.form["image_x"]))),
                      "image_y": max(0, min(100, float(request.form["image_y"]))),
                      "font_size": max(16, min(160, int(request.form["font_size"]))),
                      "text_color": request.form["text_color"], "stroke_color": request.form["stroke_color"],
                      "stroke_width": max(0, min(10, int(request.form["stroke_width"])))})
        save_state(state); flash("Grafica aggiornata", "success")
    except Exception as exc: flash(f"Impostazioni non valide: {exc}", "error")
    return redirect(url_for("dashboard"))


@app.post("/publish")
def publish_now():
    try: flash(publish()["message"], "success")
    except Exception as exc: flash(f"Pubblicazione fallita: {exc}", "error")
    return redirect(url_for("dashboard"))


@app.post("/automation/<action>")
def automation(action):
    state = load_state(); state["automation_enabled"] = action == "start"; save_state(state)
    flash("Automazione attivata" if state["automation_enabled"] else "Automazione fermata", "success")
    return redirect(url_for("dashboard"))


@app.get("/health")
def health(): return jsonify(status="ok", version=VERSION, automation=load_state()["automation_enabled"])


def start_scheduler():
    hour, minute = map(int, load_state()["publish_time"].split(":", 1))
    instance = BackgroundScheduler(timezone=TZ)
    instance.add_job(scheduled_job, "cron", day_of_week="mon-sun", hour=hour, minute=minute,
                     id="daily_fuel", max_instances=1, coalesce=True)
    instance.start(); return instance


scheduler = start_scheduler()
if __name__ == "__main__": app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
