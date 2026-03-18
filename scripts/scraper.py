#!/usr/bin/env python3
"""
Yacht Engineer Job Monitor
Filtros: Engineer/ETO | Rotación | Inicio ~2 meses | Salario ≥6.000€

Fuentes (9 total):
  Originales: Yotspot, Crew Network, Bluewater Yachting, Find a Crew, YaCrew
  Nuevas:     Saltwater Recruitment, Crewin, Faststream, YPI Crew
  LinkedIn:   vía RSS feed (rss.app) — ver instrucciones abajo
"""

import os
import re
import json
import hashlib
import smtplib
import datetime
import requests
import xml.etree.ElementTree as ET
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from bs4 import BeautifulSoup
from pathlib import Path

# ─── CONFIG ────────────────────────────────────────────────────────────────────
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_PASS = os.environ["GMAIL_PASS"]
EMAIL_TO   = os.environ["EMAIL_TO"]

# LinkedIn RSS: generá tu feed en https://rss.app y pegá la URL aquí.
# Dejalo vacío ("") para desactivar LinkedIn.
# Instrucciones: rss.app → New Feed → pegá esta URL de LinkedIn:
#   https://www.linkedin.com/jobs/search/?keywords=yacht+engineer&f_TPR=r86400
LINKEDIN_RSS_URL = os.environ.get("LINKEDIN_RSS_URL", "")

# ─── KEYWORDS ──────────────────────────────────────────────────────────────────

ENGINEER_KEYWORDS = [
    "engineer", "chief engineer", "1st engineer", "second engineer",
    "sole engineer", "eto", "electro-technical", "electrotechnical",
    "technical officer", "mechanic", "engine room", "marine engineer",
    "junior engineer", "relief engineer", "2nd engineer", "3rd engineer",
]
EXCLUDE_ROLE_KEYWORDS = [
    "stewardess", "steward", "chef", "captain", "deckhand", "bosun",
    "purser", "masseuse", "therapist", "interior manager", "cook",
    "head chef", "sous chef",
]

ROTATION_KEYWORDS = [
    "rotation", "rotational", "on/off", "on / off", "schedule",
    "2 on 2 off", "2:2", "2/2", "3 on 3 off", "3:3", "3/3",
    "4 on 4 off", "4:4", "4/4", "1 on 1 off", "rotary", "roster",
]
EXCLUDE_ROTATION_KEYWORDS = [
    "non-rotational", "non rotational", "nonrotational",
    "permanent contract", "permanent position", "permanent role",
    "full time permanent", "live aboard", "live-aboard", "liveaboard",
    "no rotation", "not rotational", "without rotation",
    "perm contract", "perm position",
]

def _availability_months():
    months_en = ["january","february","march","april","may","june",
                 "july","august","september","october","november","december"]
    months_es = ["enero","febrero","marzo","abril","mayo","junio",
                 "julio","agosto","septiembre","octubre","noviembre","diciembre"]
    now = datetime.datetime.utcnow()
    result = []
    for delta in range(3):
        m = (now.month - 1 + delta) % 12
        result.append(months_en[m])
        result.append(months_es[m])
    return result

AVAILABILITY_KEYWORDS = [
    "immediate", "immediately", "asap", "as soon as possible",
    "now", "available now", "urgently", "urgent",
    "join immediately", "start immediately",
] + _availability_months()

SALARY_MIN_EUR = 6000

def _parse_salary_eur(text: str):
    text = text.replace(",", "")
    patterns = [
        (r"(?:€|eur)\s*(\d{4,6})", "eur"),
        (r"(\d{4,6})\s*(?:€|eur)", "eur"),
        (r"(?:\$|usd)\s*(\d{4,6})", "usd"),
        (r"(\d{4,6})\s*(?:\$|usd)", "usd"),
        (r"(?:£|gbp)\s*(\d{4,6})", "gbp"),
        (r"(\d{4,6})\s*(?:£|gbp)", "gbp"),
    ]
    found = []
    for pat, currency in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            val = int(m.group(1))
            if currency == "usd":
                val = int(val * 0.92)
            elif currency == "gbp":
                val = int(val * 1.17)
            if val > 30000:
                val = val // 12
            found.append(val)
    return max(found) if found else None

# ─── HELPERS ───────────────────────────────────────────────────────────────────
SEEN_FILE = Path("seen_jobs.json")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()

def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(list(seen)))

def job_id(title, url):
    return hashlib.md5(f"{title}{url}".encode()).hexdigest()

def get(url, timeout=15):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"  ⚠ Error fetching {url}: {e}")
        return ""

# ─── FILTRO CENTRAL ────────────────────────────────────────────────────────────

def score_job(title: str, description: str = "") -> dict:
    text = (title + " " + description).lower()

    is_engineer = any(kw in text for kw in ENGINEER_KEYWORDS)
    is_excluded = any(kw in text for kw in EXCLUDE_ROLE_KEYWORDS)
    if not is_engineer or (is_excluded and not any(kw in title.lower() for kw in ENGINEER_KEYWORDS)):
        return {"passes": False}

    tags     = ["⚙️ Engineer"]
    warnings = []

    # Descarte duro: NO-rotación explícita
    if any(kw in text for kw in EXCLUDE_ROTATION_KEYWORDS):
        return {"passes": False}

    if any(kw in text for kw in ROTATION_KEYWORDS):
        tags.append("🔄 Rotation")
    else:
        warnings.append("⚠️ Rotación no mencionada")

    if any(kw in text for kw in AVAILABILITY_KEYWORDS):
        tags.append("📅 Inicio ~2 meses")
    else:
        warnings.append("⚠️ Fecha de inicio no especificada")

    salary = _parse_salary_eur(text)
    if salary is not None:
        if salary >= SALARY_MIN_EUR:
            tags.append(f"💶 ~{salary:,}€/mes")
        else:
            return {"passes": False}
    else:
        warnings.append("⚠️ Salario no especificado")

    return {"passes": True, "tags": tags, "warnings": warnings, "salary": salary}

# ─── HELPER GENÉRICO DE EXTRACCIÓN ─────────────────────────────────────────────

def _extract_jobs(soup, base_url, source, href_filters, title_min=8):
    jobs = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]
        if not text or len(text) < title_min:
            continue
        if any(kw in href.lower() for kw in href_filters):
            if not href.startswith("http"):
                href = base_url + href
            desc   = a.parent.get_text(" ", strip=True) if a.parent else ""
            result = score_job(text, desc)
            if result["passes"]:
                jobs.append({"title": text, "url": href, "source": source,
                             "tags": result["tags"], "warnings": result["warnings"]})
    return jobs[:15]

# ─── SCRAPERS ORIGINALES ────────────────────────────────────────────────────────

def scrape_yotspot():
    html  = get("https://www.yotspot.com/jobs/?department=engineer&page=1")
    soup  = BeautifulSoup(html, "html.parser") if html else None
    if not soup: return []
    cards = soup.select("div.job-listing, article.job-card, div.listing-item, li.job")
    if not cards:
        cards = [a.parent for a in soup.select("a[href*='/job/']")]
    jobs = []
    for card in cards[:25]:
        a = card.find("a", href=True)
        if not a: continue
        title = a.get_text(strip=True) or card.get_text(strip=True)[:100]
        href  = a["href"]
        if not href.startswith("http"):
            href = "https://www.yotspot.com" + href
        result = score_job(title, card.get_text(" ", strip=True))
        if result["passes"]:
            jobs.append({"title": title, "url": href, "source": "Yotspot",
                         "tags": result["tags"], "warnings": result["warnings"]})
    return jobs

def scrape_crewnetwork():
    html = get("https://www.crewnetwork.com/looking-for-a-job/")
    soup = BeautifulSoup(html, "html.parser") if html else None
    if not soup: return []
    return _extract_jobs(soup, "https://www.crewnetwork.com",
                         "Crew Network", ["/job/", "vacancy", "position"])

def scrape_bluewateryachting():
    html = get("https://www.bluewateryachting.com/crew-placement/yacht-crew/jobs")
    soup = BeautifulSoup(html, "html.parser") if html else None
    if not soup: return []
    return _extract_jobs(soup, "https://www.bluewateryachting.com",
                         "Bluewater Yachting", ["job", "position", "vacancy", "crew"])

def scrape_findacrew():
    html = get("https://www.findacrew.com/search/jobs?keywords=engineer&type=position")
    soup = BeautifulSoup(html, "html.parser") if html else None
    if not soup: return []
    return _extract_jobs(soup, "https://www.findacrew.com",
                         "Find a Crew", ["/job/", "/position/", "/crew/"])

def scrape_yacrew():
    html = get("https://www.yacrew.com/jobs?department=engineer")
    soup = BeautifulSoup(html, "html.parser") if html else None
    if not soup: return []
    return _extract_jobs(soup, "https://www.yacrew.com", "YaCrew", ["/job"])

# ─── SCRAPERS NUEVOS ────────────────────────────────────────────────────────────

def scrape_saltwater():
    """Saltwater Recruitment — agencia UK especializada en superyates 60m+"""
    html = get("https://www.saltwaterrecruitment.com/jobs/?category=engineering")
    soup = BeautifulSoup(html, "html.parser") if html else None
    if not soup: return []
    jobs = []
    # Intentar cards de trabajo primero
    cards = soup.select("article, div.job, li.job-listing, div.vacancy")
    if cards:
        for card in cards[:20]:
            a = card.find("a", href=True)
            if not a: continue
            title = a.get_text(strip=True)
            href  = a["href"]
            if not href.startswith("http"):
                href = "https://www.saltwaterrecruitment.com" + href
            result = score_job(title, card.get_text(" ", strip=True))
            if result["passes"]:
                jobs.append({"title": title, "url": href, "source": "Saltwater Recruitment",
                             "tags": result["tags"], "warnings": result["warnings"]})
    else:
        # Fallback genérico
        jobs = _extract_jobs(soup, "https://www.saltwaterrecruitment.com",
                             "Saltwater Recruitment", ["job", "vacanc", "position", "role"])
    return jobs

def scrape_crewin():
    """Crewin.com — board global moderno de tripulación de yates"""
    html = get("https://www.crewin.com/jobs?role=engineer")
    soup = BeautifulSoup(html, "html.parser") if html else None
    if not soup: return []
    jobs = _extract_jobs(soup, "https://www.crewin.com",
                         "Crewin", ["job", "/position", "/role", "/vacancy"])
    if not jobs:
        # Algunos resultados de Crewin usan rutas con IDs numéricos
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            if re.search(r"/jobs?/\d+", href) and text and len(text) > 8:
                if not href.startswith("http"):
                    href = "https://www.crewin.com" + href
                result = score_job(text, a.parent.get_text(" ", strip=True) if a.parent else "")
                if result["passes"]:
                    jobs.append({"title": text, "url": href, "source": "Crewin",
                                 "tags": result["tags"], "warnings": result["warnings"]})
    return jobs[:15]

def scrape_faststream():
    """Faststream — agencia con portal dedicado de superyacht engineer jobs"""
    html = get("https://www.faststream.com/jobs/superyacht-jobs/?department=engineering")
    soup = BeautifulSoup(html, "html.parser") if html else None
    if not soup: return []
    return _extract_jobs(soup, "https://www.faststream.com",
                         "Faststream", ["job", "vacanc", "position", "role"])

def scrape_ypicrew():
    """YPI Crew — agencia grande, yates 30m+, página específica de engineer jobs"""
    html = get("https://www.ypicrew.com/find-a-job/?department=engineering")
    soup = BeautifulSoup(html, "html.parser") if html else None
    if not soup: return []
    return _extract_jobs(soup, "https://www.ypicrew.com",
                         "YPI Crew", ["job", "vacanc", "position"])

# ─── LINKEDIN VÍA RSS ──────────────────────────────────────────────────────────

def scrape_linkedin_rss():
    """
    Lee el feed RSS de LinkedIn Jobs generado por rss.app.
    Para activar:
      1. Ir a https://rss.app → New Feed
      2. Pegar: https://www.linkedin.com/jobs/search/?keywords=yacht+engineer&f_TPR=r86400
      3. Copiar la URL del feed generado
      4. En GitHub → Settings → Secrets → agregar:
         LINKEDIN_RSS_URL = https://rss.app/feeds/XXXXXXXXXXXXXXXX.xml
    """
    if not LINKEDIN_RSS_URL:
        return []

    xml_text = get(LINKEDIN_RSS_URL)
    if not xml_text:
        return []

    jobs = []
    try:
        root = ET.fromstring(xml_text)
        # RSS 2.0 estándar
        ns   = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)

        for item in items[:30]:
            # Título
            title_el = item.find("title")
            title    = title_el.text.strip() if title_el is not None and title_el.text else ""
            if not title or len(title) < 8:
                continue

            # URL
            link_el = item.find("link")
            href    = link_el.text.strip() if link_el is not None and link_el.text else ""
            if not href:
                href_el = item.find("{http://www.w3.org/2005/Atom}link")
                href    = href_el.get("href", "") if href_el is not None else ""
            if not href:
                continue

            # Descripción
            desc_el = item.find("description") or item.find("{http://www.w3.org/2005/Atom}summary")
            desc    = desc_el.text or "" if desc_el is not None else ""
            # Limpiar HTML del snippet de LinkedIn
            desc    = BeautifulSoup(desc, "html.parser").get_text(" ", strip=True)

            result = score_job(title, desc)
            if result["passes"]:
                jobs.append({"title": title, "url": href, "source": "LinkedIn",
                             "tags": result["tags"], "warnings": result["warnings"]})
    except ET.ParseError as e:
        print(f"  ⚠ Error parseando RSS de LinkedIn: {e}")

    return jobs[:15]

# ─── EMAIL ─────────────────────────────────────────────────────────────────────

def build_email(new_jobs):
    now   = datetime.datetime.utcnow().strftime("%d %b %Y — %H:%M UTC")
    slot  = "🌅 Mañana" if datetime.datetime.utcnow().hour < 12 else "🌆 Tarde"
    count = len(new_jobs)

    by_source = {}
    for j in new_jobs:
        by_source.setdefault(j["source"], []).append(j)

    rows = ""
    for src, jlist in by_source.items():
        rows += f"""
        <tr>
          <td colspan="2" style="background:#0f3460;color:#e2e8f0;padding:8px 18px;
              font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;">
            {src}
          </td>
        </tr>"""
        for j in jlist:
            badges = " ".join(
                f'<span style="display:inline-block;background:#ebf8ff;color:#2b6cb0;'
                f'border:1px solid #bee3f8;border-radius:4px;'
                f'padding:2px 8px;font-size:11px;margin:2px 2px 0 0;">{t}</span>'
                for t in j.get("tags", [])
            )
            warns = ""
            if j.get("warnings"):
                w = " &nbsp;·&nbsp; ".join(j["warnings"])
                warns = f'<div style="margin-top:5px;font-size:11px;color:#a0aec0;">{w}</div>'
            rows += f"""
        <tr>
          <td style="padding:13px 18px;border-bottom:1px solid #e2e8f0;vertical-align:top;">
            <a href="{j['url']}" style="color:#0f3460;font-weight:600;
               text-decoration:none;font-size:14px;">{j['title']}</a>
            <div style="margin-top:5px;">{badges}</div>
            {warns}
          </td>
          <td style="padding:13px 18px;border-bottom:1px solid #e2e8f0;
              text-align:right;vertical-align:middle;white-space:nowrap;">
            <a href="{j['url']}" style="background:#0f3460;color:white;
               padding:5px 14px;border-radius:5px;font-size:12px;
               text-decoration:none;font-weight:600;">Ver →</a>
          </td>
        </tr>"""

    counter_badge = (
        f'<div style="margin-left:auto;background:#1a4a7a;color:#90cdf4;'
        f'padding:6px 14px;border-radius:20px;font-size:13px;font-weight:700;">'
        f'{count} nueva{"s" if count!=1 else ""}</div>'
        if count > 0 else ""
    )

    empty = """
      <div style="padding:32px;text-align:center;color:#718096;">
        <div style="font-size:40px;margin-bottom:12px;">📭</div>
        <p style="margin:0;font-size:15px;font-weight:600;">Sin nuevas ofertas que cumplan todos los criterios.</p>
        <p style="margin:8px 0 0;font-size:13px;color:#a0aec0;">
          Algunos anuncios no especifican rotación o salario — vale la pena revisar los sitios directamente de vez en cuando.
        </p>
      </div>"""

    li_note = ' &nbsp;·&nbsp; <a href="https://www.linkedin.com/jobs/search/?keywords=yacht+engineer" style="color:#0f3460;">LinkedIn</a>' if LINKEDIN_RSS_URL else ""

    criteria = """
      <div style="margin-top:12px;padding-top:12px;border-top:1px solid #e2e8f0;
                  font-size:11px;color:#718096;line-height:2;">
        <strong>Filtros activos:</strong><br>
        ⚙️ Engineer / ETO &nbsp;·&nbsp;
        🔄 Con rotación (descarta "permanent / non-rotational") &nbsp;·&nbsp;
        📅 Inicio ~2 meses &nbsp;·&nbsp;
        💶 Salario ≥ 6.000 €/mes <span style="color:#a0aec0;">(si está especificado)</span>
      </div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f0f4f8;font-family:'Helvetica Neue',Arial,sans-serif;">
  <div style="max-width:660px;margin:32px auto;background:white;border-radius:12px;
              overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);">

    <div style="background:#0f3460;padding:24px 30px;">
      <div style="display:flex;align-items:center;gap:14px;">
        <span style="font-size:28px;">⚙️</span>
        <div>
          <h1 style="margin:0;color:white;font-size:18px;font-weight:700;">
            Yacht Engineer Jobs — {slot}
          </h1>
          <p style="margin:3px 0 0;color:#90cdf4;font-size:12px;">{now}</p>
        </div>
        {counter_badge}
      </div>
    </div>

    {'<table style="width:100%;border-collapse:collapse;">' + rows + '</table>' if count > 0 else empty}

    <div style="padding:18px 30px;background:#f7fafc;border-top:1px solid #e2e8f0;">
      <p style="margin:0;font-size:12px;color:#718096;">
        Fuentes:
        <a href="https://www.yotspot.com/jobs/?department=engineer" style="color:#0f3460;">Yotspot</a> ·
        <a href="https://www.crewnetwork.com" style="color:#0f3460;">Crew Network</a> ·
        <a href="https://www.bluewateryachting.com" style="color:#0f3460;">Bluewater</a> ·
        <a href="https://www.findacrew.com" style="color:#0f3460;">Find a Crew</a> ·
        <a href="https://www.yacrew.com" style="color:#0f3460;">YaCrew</a> ·
        <a href="https://www.saltwaterrecruitment.com" style="color:#0f3460;">Saltwater</a> ·
        <a href="https://www.crewin.com" style="color:#0f3460;">Crewin</a> ·
        <a href="https://www.faststream.com" style="color:#0f3460;">Faststream</a> ·
        <a href="https://www.ypicrew.com" style="color:#0f3460;">YPI Crew</a>{li_note}
      </p>
      {criteria}
    </div>
  </div>
</body></html>"""


def send_email(new_jobs):
    count   = len(new_jobs)
    subject = (f"⚙️ {count} oferta{'s' if count!=1 else ''} | Engineer – Rotación ≥6K€"
               if count > 0
               else "⚙️ Sin novedades | Yacht Engineer Jobs")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(build_email(new_jobs), "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_PASS)
        smtp.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
    print(f"✅ Email enviado: {subject}")


# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("🔍 Filtros: Engineer | Rotación | ~2 meses | ≥6.000€/mes")
    seen = load_seen()
    all_jobs = []

    scrapers = [
        scrape_yotspot, scrape_crewnetwork, scrape_bluewateryachting,
        scrape_findacrew, scrape_yacrew,
        # Nuevas fuentes
        scrape_saltwater, scrape_crewin, scrape_faststream, scrape_ypicrew,
        # LinkedIn (solo activo si LINKEDIN_RSS_URL está configurado)
        scrape_linkedin_rss,
    ]

    for fn in scrapers:
        name = fn.__name__.replace("scrape_", "")
        print(f"  → {name}... ", end="")
        try:
            found = fn()
            print(f"{len(found)} match(es)")
            all_jobs.extend(found)
        except Exception as e:
            print(f"ERROR: {e}")

    new_jobs = []
    new_seen = set(seen)
    for j in all_jobs:
        jid = job_id(j["title"], j["url"])
        if jid not in seen:
            new_jobs.append(j)
            new_seen.add(jid)

    print(f"\n📋 Nuevas ofertas que cumplen criterios: {len(new_jobs)}")
    send_email(new_jobs)
    save_seen(new_seen)


if __name__ == "__main__":
    main()
