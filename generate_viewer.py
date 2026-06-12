#!/usr/bin/env python3
"""
generate_viewer.py
Corre el scraper, recolecta los resultados y los inyecta en viewer.html
para generar un archivo viewer_output.html que puede compartirse.

Uso:
    python generate_viewer.py

El archivo resultante es viewer_output.html — abrir en cualquier navegador.
"""

import json
import datetime
import subprocess
import sys
import os
from pathlib import Path

# ── Importar el scraper directamente ──────────────────────────────────────────
# Asegurarse de que estamos en el directorio correcto
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR / "scripts"))

# Parchear el scraper para capturar resultados sin enviar email
import importlib.util, types

# Cargar scraper como módulo sin ejecutarlo
spec = importlib.util.spec_from_file_location("scraper", SCRIPT_DIR / "scripts" / "scraper.py")
scraper = importlib.util.module_from_spec(spec)

# Override send_email para no enviar nada
def _no_email(jobs): pass

spec.loader.exec_module(scraper)
scraper.send_email = _no_email

# ── Ejecutar scrapers ─────────────────────────────────────────────────────────
print("🔍 Obteniendo ofertas para el viewer...")

all_jobs = []
scrapers = [
    scraper.scrape_yotspot,
    scraper.scrape_crewnetwork,
    scraper.scrape_bluewateryachting,
    scraper.scrape_findacrew,
    scraper.scrape_yacrew,
    scraper.scrape_saltwater,
    scraper.scrape_crewin,
    scraper.scrape_faststream,
    scraper.scrape_ypicrew,
    scraper.scrape_mycrewkit,
    scraper.scrape_bespokecrew,
    scraper.scrape_wilsonhalligan,
    scraper.scrape_quaycrew,
    scraper.scrape_northropjohnson,
    scraper.scrape_telegram_seamenjob,
    scraper.scrape_telegram_marinepublic,
    scraper.scrape_linkedin_rss,
]

for fn in scrapers:
    name = fn.__name__.replace("scrape_", "")
    print(f"  → {name}...", end=" ", flush=True)
    try:
        found = fn()
        print(f"{len(found)} match(es)")
        all_jobs.extend(found)
    except Exception as e:
        print(f"ERROR: {e}")

# Deduplicar por URL
seen = set()
unique_jobs = []
for j in all_jobs:
    if j["url"] not in seen:
        unique_jobs.append(j)
        seen.add(j["url"])

print(f"\n✅ {len(unique_jobs)} ofertas únicas encontradas")

# ── Inyectar en el HTML ───────────────────────────────────────────────────────
viewer_template = (SCRIPT_DIR / "viewer.html").read_text(encoding="utf-8")
now_iso = datetime.datetime.utcnow().isoformat() + "Z"

jobs_json = json.dumps(unique_jobs, ensure_ascii=False, indent=2)
injection = f"""
<script>
window.JOBS_DATA = {jobs_json};
window.GENERATED_AT = "{now_iso}";
</script>
"""

# Insertar antes del cierre de </head>
output_html = viewer_template.replace("</head>", injection + "\n</head>", 1)

output_path = SCRIPT_DIR / "viewer_output.html"
output_path.write_text(output_html, encoding="utf-8")
print(f"📄 Viewer generado: {output_path}")
print("   Abrí viewer_output.html en tu navegador para ver todas las ofertas.")
