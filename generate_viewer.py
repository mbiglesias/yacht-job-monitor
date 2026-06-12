#!/usr/bin/env python3
"""
generate_viewer.py
Corre el scraper, recolecta los resultados y los inyecta en viewer.html
para generar docs/index.html con contraseña.

La contraseña se configura en el secret VIEWER_PASSWORD de GitHub Actions.
Si no está configurado, el viewer queda sin contraseña.
"""

import json
import hashlib
import datetime
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

# ── Hash de contraseña ────────────────────────────────────────────────────────
password = os.environ.get("VIEWER_PASSWORD", "")
if password:
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    print(f"🔒 Viewer protegido con contraseña")
else:
    pw_hash = ""
    print(f"⚠️  Sin contraseña (VIEWER_PASSWORD no configurado)")

# ── Inyectar en el HTML ───────────────────────────────────────────────────────
viewer_template = (SCRIPT_DIR / "viewer.html").read_text(encoding="utf-8")
now_iso = datetime.datetime.utcnow().isoformat() + "Z"

# Acumular ofertas de los últimos 15 días
KEEP_DAYS = 15
cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=KEEP_DAYS)
json_path = SCRIPT_DIR / "docs" / "jobs.json"

existing_jobs = []
if json_path.exists():
    try:
        existing_data = json.loads(json_path.read_text(encoding="utf-8"))
        raw_existing = [
            j for j in existing_data.get("jobs", [])
            if j.get("seen_at") and
               datetime.datetime.fromisoformat(j["seen_at"].replace("Z","")) > cutoff
        ]
        # Re-filtrar el histórico: descartar cualquier job que tenga salario explícito < 6000€
        # Esto limpia los falsos positivos de versiones anteriores del scraper
        SALARY_MIN = 6000
        import re as _re
        def _salary_from_tags(tags):
            for t in (tags or []):
                m = _re.search(r'~([\d,]+)€', t)
                if m:
                    return int(m.group(1).replace(',',''))
            return None

        for j in raw_existing:
            sal = _salary_from_tags(j.get("tags", []))
            if sal is not None and sal < SALARY_MIN:
                continue  # tenía salario explícito bajo → descartar del histórico
            existing_jobs.append(j)

        print(f"  Histórico: {len(raw_existing)} → {len(existing_jobs)} después de re-filtrar")
    except Exception as e:
        print(f"  ⚠ No se pudo leer jobs.json existente: {e}")

# Combinar nuevos + histórico, deduplicar por URL
all_url_seen = set()
combined = []
for j in unique_jobs + existing_jobs:
    if j["url"] not in all_url_seen:
        combined.append(j)
        all_url_seen.add(j["url"])
print(f"📦 Total combinado (últimos {KEEP_DAYS} días): {len(combined)} ofertas")

jobs_json = json.dumps(combined, ensure_ascii=False, indent=2)
injection = f"""
<script>
window.JOBS_DATA = {jobs_json};
window.GENERATED_AT = "{now_iso}";
window.PASSWORD_HASH = "{pw_hash}";
</script>
"""

output_html = viewer_template.replace("</head>", injection + "\n</head>", 1)

# Guardar docs/index.html para GitHub Pages
output_path = SCRIPT_DIR / "docs" / "index.html"
output_path.parent.mkdir(exist_ok=True)
output_path.write_text(output_html, encoding="utf-8")

# Guardar docs/jobs.json acumulado
jobs_data = {"generated_at": now_iso, "total": len(combined), "jobs": combined}
json_path.write_text(json.dumps(jobs_data, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"📄 Viewer generado: {output_path}")
print(f"📦 JSON generado:   {json_path}  ({len(combined)} ofertas)")
print(f"   URL: https://TU_USUARIO.github.io/yacht-job-monitor/")
