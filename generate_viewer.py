#!/usr/bin/env python3
"""
generate_viewer.py
Genera docs/index.html y docs/jobs.json con todas las ofertas activas.

IMPORTANTE: El viewer muestra TODAS las ofertas que pasen los filtros hoy,
más el historial de los últimos 15 días. No filtra por "ya visto" —
eso es solo para el email. El viewer siempre muestra todo.
"""

import json, hashlib, datetime, sys, os, re
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR / "scripts"))

import importlib.util
spec    = importlib.util.spec_from_file_location("scraper", SCRIPT_DIR / "scripts" / "scraper.py")
scraper = importlib.util.module_from_spec(spec)
def _no_email(jobs): pass
spec.loader.exec_module(scraper)
scraper.send_email = _no_email

# ── Correr todos los scrapers (sin filtro de "ya visto") ──────────────────────
print("🔍 Scrapeando todas las fuentes para el viewer...")

all_jobs = []
fns = [
    scraper.scrape_yotspot, scraper.scrape_crewnetwork,
    scraper.scrape_bluewateryachting, scraper.scrape_findacrew,
    scraper.scrape_yacrew, scraper.scrape_saltwater,
    scraper.scrape_crewin, scraper.scrape_faststream,
    scraper.scrape_ypicrew, scraper.scrape_mycrewkit,
    scraper.scrape_bespokecrew, scraper.scrape_wilsonhalligan,
    scraper.scrape_quaycrew, scraper.scrape_northropjohnson,
    scraper.scrape_xelvin,
    scraper.scrape_telegram_seamenjob, scraper.scrape_telegram_marinepublic,
    scraper.scrape_linkedin_rss,
]
for fn in fns:
    name = fn.__name__.replace("scrape_", "")
    print(f"  → {name}...", end=" ", flush=True)
    try:
        found = fn()
        print(f"{len(found)}")
        all_jobs.extend(found)
    except Exception as e:
        print(f"ERROR: {e}")

# Deduplicar por URL, marcando seen_at = ahora
now_iso = datetime.datetime.utcnow().isoformat() + "Z"
seen_urls = set()
fresh_jobs = []
for j in all_jobs:
    if j["url"] not in seen_urls:
        j.setdefault("seen_at", now_iso)   # solo poner si no tiene ya
        fresh_jobs.append(j)
        seen_urls.add(j["url"])

print(f"\n✅ {len(fresh_jobs)} ofertas únicas del run actual")

# ── Combinar con historial de 15 días ────────────────────────────────────────
KEEP_DAYS = 15
cutoff    = datetime.datetime.utcnow() - datetime.timedelta(days=KEEP_DAYS)
json_path = SCRIPT_DIR / "docs" / "jobs.json"

def _salary_from_tags(tags):
    for t in (tags or []):
        m = re.search(r'~([\d,]+)€', t)
        if m:
            return int(m.group(1).replace(',',''))
    return None

historical = []
if json_path.exists():
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        for j in data.get("jobs", []):
            # Solo incluir si está dentro del rango de 15 días
            seen_at = j.get("seen_at")
            if seen_at:
                try:
                    dt = datetime.datetime.fromisoformat(seen_at.replace("Z",""))
                    if dt <= cutoff:
                        continue   # demasiado viejo
                except Exception:
                    pass
            # Skip si ya está en el run actual (misma URL)
            if j["url"] in seen_urls:
                continue
            # Re-filtrar: descartar salario explícito bajo 6000€
            sal = _salary_from_tags(j.get("tags", []))
            if sal is not None and sal < 6000:
                continue
            historical.append(j)
            seen_urls.add(j["url"])
        print(f"📚 Histórico: {len(historical)} ofertas adicionales de los últimos {KEEP_DAYS} días")
    except Exception as e:
        print(f"  ⚠ Error leyendo histórico: {e}")

# Fresh jobs primero (más recientes), luego histórico
combined = fresh_jobs + historical
print(f"📦 Total viewer: {len(combined)} ofertas")

# ── Contraseña ────────────────────────────────────────────────────────────────
password = os.environ.get("VIEWER_PASSWORD", "")
pw_hash  = hashlib.sha256(password.encode()).hexdigest() if password else ""
if password:
    print("🔒 Viewer protegido con contraseña")
else:
    print("⚠️  Sin contraseña (VIEWER_PASSWORD no configurado)")

# ── Generar HTML ──────────────────────────────────────────────────────────────
viewer_template = (SCRIPT_DIR / "viewer.html").read_text(encoding="utf-8")
jobs_json = json.dumps(combined, ensure_ascii=False, indent=2)
injection = f"""<script>
window.JOBS_DATA = {jobs_json};
window.GENERATED_AT = "{now_iso}";
window.PASSWORD_HASH = "{pw_hash}";
</script>"""

output_html = viewer_template.replace("</head>", injection + "\n</head>", 1)

output_path = SCRIPT_DIR / "docs" / "index.html"
output_path.parent.mkdir(exist_ok=True)
output_path.write_text(output_html, encoding="utf-8")

jobs_data = {"generated_at": now_iso, "total": len(combined), "jobs": combined}
json_path.write_text(json.dumps(jobs_data, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"📄 Viewer: {output_path}")
print(f"📦 JSON:   {json_path}")
print(f"   → https://TU_USUARIO.github.io/yacht-job-monitor/")
