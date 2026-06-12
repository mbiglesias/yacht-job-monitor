#!/usr/bin/env python3
"""
Yacht Engineer Job Monitor
Filtros: Engineer/ETO | Rotación | Inicio ~2 meses | Salario ≥6.000€ | Solo yates
17 fuentes monitoreadas.
"""

import os, re, json, hashlib, smtplib, datetime, requests, calendar
import xml.etree.ElementTree as ET
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from bs4 import BeautifulSoup
from pathlib import Path

# ─── CONFIG ────────────────────────────────────────────────────────────────────
GMAIL_USER       = os.environ["GMAIL_USER"]
GMAIL_PASS       = os.environ["GMAIL_PASS"]
EMAIL_TO         = os.environ["EMAIL_TO"]
LINKEDIN_RSS_URL = os.environ.get("LINKEDIN_RSS_URL", "")

SALARY_MIN_EUR = 6000

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

EXCLUDE_VESSEL_KEYWORDS = [
    "bulk carrier", "bulk vessel", "bulker",
    "general cargo", "general cargo vessel", "general cargo ship",
    "container ship", "container vessel", "containership",
    "cargo ship", "cargo vessel", "freighter",
    "ro-ro", "roro", "roll-on roll-off",
    "dwt", "vessel's type", "vessel type:",
    "tanker", "oil tanker", "chemical tanker", "lng tanker", "lpg tanker",
    "crude oil", "vlcc", "ulcc", "aframax", "suezmax", "panamax",
    "product tanker", "crude carrier",
    "offshore vessel", "offshore platform", "offshore support",
    "drilling rig", "drillship", "jack-up", "jackup",
    "fpso", "fsru", "fso", "ahts", "psv",
    "tug", "tugboat", "towing vessel",
    "dredger", "dredging vessel",
    "ferry", "passenger ferry", "cruise ship", "cruise liner",
    "research vessel", "survey vessel",
    "naval vessel", "military vessel",
    "merchant vessel", "merchant ship", "merchant navy",
    "shipmanagement", "ship management", "ship manager",
    "manning agency",
    "eurocrew", "columbia shipmanagement", "wallem", "v.ships", "v ships",
    "bernhard schulte", "thome group", "thome ship", "anglo-eastern", "anglo eastern",
    "fleet management", "danaos", "costamare", "starbulk", "diana shipping",
    "gmdss", "coc class 2", "coc class 1",
    "voyage duration", "voyage contract",
    "embarkation port", "joining port", "port of joining",
    "контейнеровоз", "балкер", "танкер", "суховантаж", "наливн",
    "msc crewing", "msc crewing services",
    "maersk", "cma cgm", "hapag-lloyd", "evergreen",
]
YACHT_CONFIRM_KEYWORDS = [
    "yacht", "superyacht", "super yacht", "motor yacht", "sailing yacht",
    "private yacht", "luxury yacht", "megayacht", "mega yacht",
    "myacht", "m/y", "s/y",
]

ROTATION_KEYWORDS = [
    "rotation", "rotational", "on/off", "on / off", "schedule",
    "2 on 2 off", "2:2", "2/2", "3 on 3 off", "3:3", "3/3",
    "4 on 4 off", "4:4", "4/4", "1 on 1 off", "rotary", "roster",
    "leave:", "days leave", "days off", "days on",
    "60 days", "90 days", "30 days", "45 days",
    "contract length", "contract period", "contract duration",
]
EXCLUDE_ROTATION_KEYWORDS = [
    "non-rotational", "nonrotational",
    "permanent contract", "permanent position", "permanent role",
    "full time permanent", "live aboard", "live-aboard", "liveaboard",
    "no rotation", "not rotational", "without rotation",
    "perm contract", "perm position",
    "/ permanent", ": permanent", "type: permanent",
    "contract type: permanent", "employment type: permanent",
    "contract: permanent", "job type: permanent",
    "job type:permanent", "type:permanent",
]
# _PERMANENT_RE no se usa directamente — _has_permanent() maneja toda la lógica
_PERMANENT_RE = re.compile(r'\bpermanent\b', re.IGNORECASE)

def _has_permanent(text: str) -> bool:
    """True si el texto confirma contrato permanente (no rotacional)."""
    t = text.lower()
    # Primero eliminar "non-permanent" y "non permanent" del texto
    # para que no hagan falso positivo
    t_clean = re.sub(r'\bnon[-\s]permanent\b', 'NONPERM', t)
    if any(kw in t_clean for kw in EXCLUDE_ROTATION_KEYWORDS):
        return True
    return bool(re.search(r'\bpermanent\b', t_clean))

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

# ─── SALARY PARSER ─────────────────────────────────────────────────────────────

# FIX: parsear salario SOLO si hay keyword de salario cercana, 
# evita leer job IDs, esloras, años de experiencia como salario.
_SALARY_CONTEXT_RE = re.compile(
    r'(?:salary|salario|pay|wage|compensation|remuneration|package|'
    r'earning|income|rate|renumeration)\s*[:\-]?\s*',
    re.IGNORECASE
)

def _parse_salary_eur(text: str):
    """
    Parsea salario del texto. 
    Prioriza texto cercano a keywords de salario para evitar falsos positivos.
    """
    text_norm = text.replace(",", "")
    text_norm = re.sub(r'(\d+)\s*k\b', lambda m: str(int(m.group(1)) * 1000),
                       text_norm, flags=re.IGNORECASE)

    def to_eur(val, currency):
        if currency == "usd":  val = int(val * 0.92)
        elif currency == "gbp": val = int(val * 1.17)
        if val > 30000: val = val // 12
        return val

    # Extraer ventana de contexto alrededor de keywords de salario (más confiable)
    salary_windows = []
    for m in _SALARY_CONTEXT_RE.finditer(text_norm):
        start = m.start()
        window = text_norm[start:start + 60]   # 60 chars después del keyword
        salary_windows.append(window)

    # Si no hay keywords de salario, usar texto completo pero con patterns más estrictos
    search_texts = salary_windows if salary_windows else [text_norm]

    found = []
    range_patterns = [
        (r'(?:EUR|€|euros?)\s*(\d{4,6})\s*[-–]\s*(\d{4,6})',  "eur"),
        (r'(\d{4,6})\s*[-–]\s*(\d{4,6})\s*(?:EUR|€|euros?)',  "eur"),
        (r'(?:USD|\$)\s*(\d{4,6})\s*[-–]\s*(\d{4,6})',        "usd"),
        (r'(\d{4,6})\s*[-–]\s*(\d{4,6})\s*(?:USD|\$)',        "usd"),
        (r'(?:GBP|£)\s*(\d{4,6})\s*[-–]\s*(\d{4,6})',        "gbp"),
        (r'(\d{4,6})\s*[-–]\s*(\d{4,6})\s*(?:GBP|£)',        "gbp"),
    ]
    single_patterns = [
        (r'(?:EUR|€|euros?)\s*(\d{4,6})\+?',  "eur"),
        (r'(\d{4,6})\+?\s*(?:EUR|€|euros?)',   "eur"),
        (r'(?:USD|\$)\s*(\d{4,6})\+?',         "usd"),
        (r'(\d{4,6})\+?\s*(?:USD|\$)',         "usd"),
        (r'(?:GBP|£)\s*(\d{4,6})\+?',         "gbp"),
        (r'(\d{4,6})\+?\s*(?:GBP|£)',         "gbp"),
    ]

    for search_text in search_texts:
        range_positions = set()
        for pat, currency in range_patterns:
            for m in re.finditer(pat, search_text, re.IGNORECASE):
                hi = to_eur(int(m.group(2)), currency)
                found.append(hi)
                range_positions.update(range(m.start(), m.end()))
        for pat, currency in single_patterns:
            for m in re.finditer(pat, search_text, re.IGNORECASE):
                if any(p in range_positions for p in range(m.start(), m.end())):
                    continue
                found.append(to_eur(int(m.group(1)), currency))

    return max(found) if found else None

# ─── DATE PARSER ───────────────────────────────────────────────────────────────

_MONTHS = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
    "january":1,"february":2,"march":3,"april":4,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
    "ene":1,"mar":3,"abr":4,"ago":8,"dic":12,
    "enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,
    "julio":7,"agosto":8,"septiembre":9,"octubre":10,"noviembre":11,"diciembre":12,
}

def _parse_date(s: str):
    if not s: return None
    s = s.strip()
    try:
        m = re.search(r'(\d{4})-(\d{2})-(\d{2})', s)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 2020 <= y <= 2030 and 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{d:02d} {list(calendar.month_abbr)[mo]} {y}"
        m = re.search(r'(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})', s)
        if m:
            d, mon_str, y = int(m.group(1)), m.group(2).lower()[:3], int(m.group(3))
            mo = _MONTHS.get(mon_str)
            if mo and 2020 <= y <= 2030:
                return f"{d:02d} {list(calendar.month_abbr)[mo]} {y}"
        m = re.search(r'([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})', s)
        if m:
            mon_str, d, y = m.group(1).lower()[:3], int(m.group(2)), int(m.group(3))
            mo = _MONTHS.get(mon_str)
            if mo and 2020 <= y <= 2030:
                return f"{d:02d} {list(calendar.month_abbr)[mo]} {y}"
    except Exception:
        pass
    return None

def _parse_date_from_text(text: str):
    for pattern in [
        r'(?:posted|published|listed|added|date)[:\s]+(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})',
        r'(?:posted|published|listed|added|date)[:\s]+([A-Za-z]+\s+\d{1,2},?\s+\d{4})',
        r'(?:posted|published|listed|added|date)[:\s]+(\d{4}-\d{2}-\d{2})',
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            result = _parse_date(m.group(1))
            if result: return result
    return None

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

def fetch_job_detail(url: str) -> dict:
    html = get(url, timeout=10)
    if not html:
        return {"text": "", "posted": None}
    soup = BeautifulSoup(html, "html.parser")
    posted = None
    # 1. Meta tags
    for meta_name in ["article:published_time", "datePublished", "date", "publish_date"]:
        tag = soup.find("meta", property=meta_name) or soup.find("meta", attrs={"name": meta_name})
        if tag and tag.get("content"):
            posted = _parse_date(tag["content"])
            if posted: break
    # 2. Time elements
    if not posted:
        for el in soup.find_all(["time", "span", "div", "p", "td", "li"]):
            dt = el.get("datetime") or el.get("data-date") or el.get("data-published")
            if dt:
                posted = _parse_date(dt)
                if posted: break
            text_el = el.get_text(strip=True).lower()
            if any(kw in text_el for kw in ["posted:", "published:", "date posted",
                                              "date published", "listed:", "added:"]):
                posted = _parse_date(el.get_text(strip=True))
                if posted: break
    for tag in soup(["nav", "footer", "script", "style", "header"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)[:5000]
    if not posted:
        posted = _parse_date_from_text(text)
    return {"text": text, "posted": posted}

# ─── FILTRO CENTRAL ────────────────────────────────────────────────────────────

def score_job(title: str, description: str = "", job_url: str = "") -> dict:
    text = (title + " " + description).lower()
    posted_date = None

    # Rol de ingeniero
    is_engineer = any(kw in text for kw in ENGINEER_KEYWORDS)
    is_excluded = any(kw in text for kw in EXCLUDE_ROLE_KEYWORDS)
    if not is_engineer or (is_excluded and not any(kw in title.lower() for kw in ENGINEER_KEYWORDS)):
        return {"passes": False}

    # Descarte duro: permanente desde el card
    if _has_permanent(text):
        return {"passes": False}

    # Entrar al detalle para info completa
    if job_url:
        detail = fetch_job_detail(job_url)
        detail_text = detail.get("text", "")
        posted_date = detail.get("posted")
        if detail_text:
            detail_lower = detail_text.lower()
            if _has_permanent(detail_lower):
                return {"passes": False}
            text = text + " " + detail_lower

    # Buque comercial
    is_commercial = any(kw in text for kw in EXCLUDE_VESSEL_KEYWORDS)
    is_yacht      = any(kw in text for kw in YACHT_CONFIRM_KEYWORDS)
    if is_commercial and not is_yacht:
        return {"passes": False}

    tags     = ["⚙️ Engineer"]
    warnings = []

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
            return {"passes": False}    # salario explícito pero bajo → DESCARTAR siempre
    else:
        warnings.append("⚠️ Salario no especificado")

    return {
        "passes":   True,
        "tags":     tags,
        "warnings": warnings,
        "salary":   salary,
        "posted":   posted_date,
    }

# ─── HELPER GENÉRICO ───────────────────────────────────────────────────────────

# Slugs de departamento de My Crew Kit que NO son anuncios individuales
_MCK_DEPT_SLUGS = {
    "engineer", "eto", "captain", "mate", "bosun", "steward",
    "stewardess", "chef", "purser", "deck", "interior", "exterior",
}

def _is_job_url(href: str, source: str) -> bool:
    """True si la URL parece ser un anuncio individual, no una página de listado."""
    if source == "My Crew Kit":
        m = re.search(r"/superyacht-jobs/([^/?#]+)/?$", href)
        if m and m.group(1).lower() in _MCK_DEPT_SLUGS:
            return False   # es la página del departamento, no un anuncio
    return True

def _extract_jobs(soup, base_url, source, href_filters, title_min=8, max_results=20):
    jobs = []
    seen_hrefs = set()
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]
        if not text or len(text) < title_min:
            continue
        if any(kw in href.lower() for kw in href_filters):
            if not href.startswith("http"):
                href = base_url + href
            if href in seen_hrefs:
                continue
            if not _is_job_url(href, source):
                continue
            seen_hrefs.add(href)
            desc   = a.parent.get_text(" ", strip=True) if a.parent else ""
            result = score_job(text, desc, job_url=href)
            if result["passes"]:
                jobs.append({
                    "title":    text,
                    "url":      href,
                    "source":   source,
                    "tags":     result["tags"],
                    "warnings": result["warnings"],
                    "posted":   result.get("posted"),
                })
            if len(jobs) >= max_results:
                break
    return jobs

# ─── PAGINATION HELPER ─────────────────────────────────────────────────────────

def _paginate(url_fn, source, max_pages=5):
    """
    Itera páginas de un sitio hasta que no haya más resultados o se llegue a max_pages.
    url_fn(page) devuelve la URL para esa página (1-indexed).
    """
    all_jobs = []
    for page in range(1, max_pages + 1):
        url = url_fn(page)
        html = get(url)
        if not html: break
        soup = BeautifulSoup(html, "html.parser")
        # Detectar si hay contenido útil
        links = [a for a in soup.find_all("a", href=True)
                 if any(kw in a["href"].lower() for kw in ["job", "vacanc", "position"])]
        if not links: break
        jobs = _extract_jobs(soup, url.split("/jobs")[0] if "/jobs" in url else url,
                             source, ["job", "vacanc", "position", "role", "crew"])
        if not jobs: break
        all_jobs.extend(jobs)
    return all_jobs

# ─── SCRAPERS ──────────────────────────────────────────────────────────────────

def scrape_yotspot():
    all_jobs = []
    for page in range(1, 6):
        html = get(f"https://www.yotspot.com/jobs/?department=engineer&page={page}")
        soup = BeautifulSoup(html, "html.parser") if html else None
        if not soup: break
        cards = soup.select("div.job-listing, article.job-card, div.listing-item, li.job")
        if not cards:
            cards = [a.parent for a in soup.select("a[href*='/job/']")]
        if not cards: break
        found_this_page = 0
        for card in cards:
            a = card.find("a", href=True)
            if not a: continue
            title = a.get_text(strip=True) or card.get_text(strip=True)[:100]
            href  = a["href"]
            if not href.startswith("http"):
                href = "https://www.yotspot.com" + href
            result = score_job(title, card.get_text(" ", strip=True), job_url=href)
            if result["passes"]:
                all_jobs.append({"title": title, "url": href, "source": "Yotspot",
                                 "tags": result["tags"], "warnings": result["warnings"],
                                 "posted": result.get("posted")})
                found_this_page += 1
        if found_this_page == 0: break
    return all_jobs

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

def scrape_saltwater():
    html = get("https://www.saltwaterrecruitment.com/jobs/?category=engineering")
    soup = BeautifulSoup(html, "html.parser") if html else None
    if not soup: return []
    return _extract_jobs(soup, "https://www.saltwaterrecruitment.com",
                         "Saltwater Recruitment", ["job", "vacanc", "position", "role"])

def scrape_crewin():
    html = get("https://www.crewin.com/jobs?role=engineer")
    soup = BeautifulSoup(html, "html.parser") if html else None
    if not soup: return []
    jobs = _extract_jobs(soup, "https://www.crewin.com",
                         "Crewin", ["job", "/position", "/role", "/vacancy"])
    if not jobs:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            if re.search(r"/jobs?/\d+", href) and text and len(text) > 8:
                if not href.startswith("http"):
                    href = "https://www.crewin.com" + href
                result = score_job(text, a.parent.get_text(" ", strip=True) if a.parent else "", job_url=href)
                if result["passes"]:
                    jobs.append({"title": text, "url": href, "source": "Crewin",
                                 "tags": result["tags"], "warnings": result["warnings"],
                                 "posted": result.get("posted")})
    return jobs[:20]

def scrape_faststream():
    html = get("https://www.faststream.com/jobs/superyacht-jobs/?department=engineering")
    soup = BeautifulSoup(html, "html.parser") if html else None
    if not soup: return []
    return _extract_jobs(soup, "https://www.faststream.com",
                         "Faststream", ["job", "vacanc", "position", "role"])

def scrape_ypicrew():
    html = get("https://www.ypicrew.com/find-a-job/?department=engineering")
    soup = BeautifulSoup(html, "html.parser") if html else None
    if not soup: return []
    return _extract_jobs(soup, "https://www.ypicrew.com",
                         "YPI Crew", ["job", "vacanc", "position"])

def scrape_bespokecrew():
    all_jobs = []
    for page in range(1, 4):
        url = f"https://www.bespokecrew.com/jobs/?department=engineering&paged={page}"
        html = get(url)
        soup = BeautifulSoup(html, "html.parser") if html else None
        if not soup: break
        jobs = _extract_jobs(soup, "https://www.bespokecrew.com",
                             "Bespoke Crew", ["/job/", "/jobs/", "/vacancy/", "/position/"])
        if not jobs: break
        all_jobs.extend(jobs)
    return all_jobs

def scrape_wilsonhalligan():
    html = get("https://www.wilsonhalligan.com/our-current-roles/")
    soup = BeautifulSoup(html, "html.parser") if html else None
    if not soup: return []
    return _extract_jobs(soup, "https://www.wilsonhalligan.com",
                         "Wilsonhalligan", ["/job/", "/vacancy/", "/role/", "/position/"])

def scrape_quaycrew():
    html = get("https://jobs.quaygroup.com/sectors/4/yacht-engineering-jobs.aspx")
    soup = BeautifulSoup(html, "html.parser") if html else None
    if not soup: return []
    return _extract_jobs(soup, "https://jobs.quaygroup.com",
                         "Quay Crew", ["/job/", "/jobs/", "/vacancy/", "/position/", ".aspx"])

def scrape_mycrewkit():
    html = get("https://mycrewkit.com/superyacht-jobs/engineer/")
    soup = BeautifulSoup(html, "html.parser") if html else None
    if not soup: return []
    jobs = []
    seen_hrefs = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        # FIX: regex más estricto — requiere slug no-vacío que NO sea solo el departamento
        if not re.search(r"/superyacht-jobs/[^/]{3,}/?$|/job/\d+", href):
            continue
        if not text or len(text) < 6:
            continue
        if not href.startswith("http"):
            href = "https://mycrewkit.com" + href
        if href in seen_hrefs:
            continue
        if not _is_job_url(href, "My Crew Kit"):
            continue
        seen_hrefs.add(href)
        parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
        result = score_job(text, parent_text, job_url=href)
        if result["passes"]:
            jobs.append({"title": text, "url": href, "source": "My Crew Kit",
                         "tags": result["tags"], "warnings": result["warnings"],
                         "posted": result.get("posted")})
    return jobs[:20]

def scrape_northropjohnson():
    html = get("https://crew.northropandjohnson.com/crew-jobs/")
    soup = BeautifulSoup(html, "html.parser") if html else None
    if not soup: return []
    return _extract_jobs(soup, "https://crew.northropandjohnson.com",
                         "Northrop & Johnson", ["job", "vacanc", "position", "crew"])

def scrape_xelvin():
    html = get("https://www.xelvin.nl/vacatures/?sector=jacht-scheepsbouw&zoekterm=engineer")
    soup = BeautifulSoup(html, "html.parser") if html else None
    if not soup: return []
    jobs = _extract_jobs(soup, "https://www.xelvin.nl",
                         "Xelvin", ["/vacature/", "/vacancy/", "/job/", "/jobs/"])
    if not jobs:
        html2 = get("https://www.xelvin.nl/en/vacancies/?sector=yacht-shipbuilding")
        soup2 = BeautifulSoup(html2, "html.parser") if html2 else None
        if soup2:
            jobs = _extract_jobs(soup2, "https://www.xelvin.nl",
                                 "Xelvin", ["/vacature/", "/vacancy/", "/job/"])
    return jobs

def _scrape_telegram(channel: str, source_label: str):
    url  = f"https://t.me/s/{channel}"
    html = get(url)
    if not html: return []
    soup  = BeautifulSoup(html, "html.parser")
    jobs  = []
    messages = soup.select("div.tgme_widget_message_wrap")
    if not messages:
        messages = soup.select("div.tgme_widget_message_text")
    for msg in messages[-40:]:
        text_el = msg.select_one("div.tgme_widget_message_text") or msg
        full_text = text_el.get_text(" ", strip=True)
        if len(full_text) < 20: continue
        lines = [l.strip() for l in full_text.splitlines() if l.strip()]
        title = lines[0][:120] if lines else full_text[:80]
        msg_link = msg.select_one("a.tgme_widget_message_date")
        href = msg_link["href"] if msg_link and msg_link.has_attr("href") else url
        tg_posted = None
        time_el = msg.select_one("time")
        if time_el and time_el.get("datetime"):
            tg_posted = _parse_date(time_el["datetime"])
        result = score_job(title, full_text)
        if result["passes"]:
            jobs.append({"title": title, "url": href, "source": source_label,
                         "tags": result["tags"], "warnings": result["warnings"],
                         "posted": tg_posted or result.get("posted")})
    return jobs[:10]

def scrape_telegram_seamenjob():
    return _scrape_telegram("seamenjob", "Telegram: SeamenJob")

def scrape_telegram_marinepublic():
    return _scrape_telegram("marinepublic_com", "Telegram: MarinePublic")

def scrape_linkedin_rss():
    if not LINKEDIN_RSS_URL: return []
    xml_text = get(LINKEDIN_RSS_URL)
    if not xml_text: return []
    jobs = []
    try:
        root = ET.fromstring(xml_text)
        ns   = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)
        for item in items[:30]:
            title_el = item.find("title")
            title    = title_el.text.strip() if title_el is not None and title_el.text else ""
            if not title or len(title) < 8: continue
            link_el = item.find("link")
            href    = link_el.text.strip() if link_el is not None and link_el.text else ""
            if not href:
                href_el = item.find("{http://www.w3.org/2005/Atom}link")
                href    = href_el.get("href", "") if href_el is not None else ""
            if not href: continue
            desc_el = item.find("description") or item.find("{http://www.w3.org/2005/Atom}summary")
            desc    = desc_el.text or "" if desc_el is not None else ""
            desc    = BeautifulSoup(desc, "html.parser").get_text(" ", strip=True)
            result  = score_job(title, desc)
            if result["passes"]:
                jobs.append({"title": title, "url": href, "source": "LinkedIn",
                             "tags": result["tags"], "warnings": result["warnings"],
                             "posted": result.get("posted")})
    except ET.ParseError as e:
        print(f"  ⚠ Error parseando RSS: {e}")
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
        rows += f'<tr><td colspan="2" style="background:#0f3460;color:#e2e8f0;padding:8px 18px;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;">{src}</td></tr>'
        for j in jlist:
            badges = " ".join(
                f'<span style="display:inline-block;background:#ebf8ff;color:#2b6cb0;border:1px solid #bee3f8;border-radius:4px;padding:2px 8px;font-size:11px;margin:2px 2px 0 0;">{t}</span>'
                for t in j.get("tags", [])
            )
            warns = ""
            if j.get("warnings"):
                w = " &nbsp;·&nbsp; ".join(j["warnings"])
                warns = f'<div style="margin-top:5px;font-size:11px;color:#a0aec0;">{w}</div>'
            posted_html = f'<div style="margin-top:4px;font-size:11px;color:#805ad5;">📆 {j["posted"]}</div>' if j.get("posted") else ""
            url = j["url"]
            rows += f'''<tr><td style="padding:13px 18px;border-bottom:1px solid #e2e8f0;vertical-align:top;">
              <a href="{url}" style="color:#0f3460;font-weight:600;text-decoration:none;font-size:14px;">{j["title"]}</a>
              <div style="margin-top:5px;">{badges}</div>{warns}{posted_html}
            </td><td style="padding:13px 18px;border-bottom:1px solid #e2e8f0;text-align:right;vertical-align:middle;white-space:nowrap;">
              <a href="{url}" style="background:#0f3460;color:white;padding:5px 14px;border-radius:5px;font-size:12px;text-decoration:none;font-weight:600;">Ver →</a>
            </td></tr>'''
    counter = f'<div style="margin-left:auto;background:#1a4a7a;color:#90cdf4;padding:6px 14px;border-radius:20px;font-size:13px;font-weight:700;">{count} nueva{"s" if count!=1 else ""}</div>' if count > 0 else ""
    empty = '<div style="padding:32px;text-align:center;color:#718096;"><div style="font-size:40px;margin-bottom:12px;">📭</div><p style="margin:0;font-size:15px;font-weight:600;">Sin nuevas ofertas.</p></div>'
    li_note = f' &nbsp;·&nbsp; <a href="https://www.linkedin.com/jobs/search/?keywords=yacht+engineer" style="color:#0f3460;">LinkedIn</a>' if LINKEDIN_RSS_URL else ""
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f0f4f8;font-family:'Helvetica Neue',Arial,sans-serif;">
<div style="max-width:660px;margin:32px auto;background:white;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);">
<div style="background:#0f3460;padding:24px 30px;">
  <div style="display:flex;align-items:center;gap:14px;">
    <span style="font-size:28px;">⚙️</span>
    <div><h1 style="margin:0;color:white;font-size:18px;font-weight:700;">Yacht Engineer Jobs — {slot}</h1>
    <p style="margin:3px 0 0;color:#90cdf4;font-size:12px;">{now}</p></div>
    {counter}
  </div>
</div>
{'<table style="width:100%;border-collapse:collapse;">' + rows + '</table>' if count > 0 else empty}
<div style="padding:18px 30px;background:#f7fafc;border-top:1px solid #e2e8f0;">
  <p style="margin:0;font-size:12px;color:#718096;">
    <a href="https://www.yotspot.com/jobs/?department=engineer" style="color:#0f3460;">Yotspot</a> ·
    <a href="https://www.crewnetwork.com" style="color:#0f3460;">Crew Network</a> ·
    <a href="https://www.bluewateryachting.com" style="color:#0f3460;">Bluewater</a> ·
    <a href="https://www.findacrew.com" style="color:#0f3460;">Find a Crew</a> ·
    <a href="https://www.yacrew.com" style="color:#0f3460;">YaCrew</a> ·
    <a href="https://www.saltwaterrecruitment.com" style="color:#0f3460;">Saltwater</a> ·
    <a href="https://www.crewin.com" style="color:#0f3460;">Crewin</a> ·
    <a href="https://www.faststream.com" style="color:#0f3460;">Faststream</a> ·
    <a href="https://www.ypicrew.com" style="color:#0f3460;">YPI Crew</a> ·
    <a href="https://mycrewkit.com/superyacht-jobs/engineer/" style="color:#0f3460;">My Crew Kit</a> ·
    <a href="https://www.bespokecrew.com/jobs/" style="color:#0f3460;">Bespoke Crew</a> ·
    <a href="https://www.wilsonhalligan.com/our-current-roles/" style="color:#0f3460;">Wilsonhalligan</a> ·
    <a href="https://jobs.quaygroup.com/sectors/4/yacht-engineering-jobs.aspx" style="color:#0f3460;">Quay Crew</a> ·
    <a href="https://crew.northropandjohnson.com/crew-jobs/" style="color:#0f3460;">Northrop &amp; Johnson</a> ·
    <a href="https://www.xelvin.nl/vacatures/?sector=jacht-scheepsbouw" style="color:#0f3460;">Xelvin</a> ·
    <a href="https://t.me/seamenjob" style="color:#0f3460;">Telegram SeamenJob</a> ·
    <a href="https://t.me/marinepublic_com" style="color:#0f3460;">Telegram MarinePublic</a>{li_note}
  </p>
  <div style="margin-top:10px;font-size:11px;color:#718096;line-height:1.8;">
    <strong>Filtros:</strong> ⚙️ Engineer/ETO · 🔄 Rotación · 📅 Inicio ~2 meses · 💶 ≥6.000€/mes · 🚢 Solo yates
  </div>
</div></div></body></html>"""

def send_email(new_jobs):
    count   = len(new_jobs)
    subject = (f"⚙️ {count} oferta{'s' if count!=1 else ''} | Engineer – Rotación ≥6K€"
               if count > 0 else "⚙️ Sin novedades | Yacht Engineer Jobs")
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
    print("🔍 Filtros: Engineer | Rotación | ~2 meses | ≥6.000€/mes | Solo yates")
    seen = load_seen()
    all_jobs = []
    scrapers = [
        scrape_yotspot, scrape_crewnetwork, scrape_bluewateryachting,
        scrape_findacrew, scrape_yacrew,
        scrape_saltwater, scrape_crewin, scrape_faststream, scrape_ypicrew,
        scrape_mycrewkit, scrape_bespokecrew, scrape_wilsonhalligan,
        scrape_quaycrew, scrape_northropjohnson, scrape_xelvin,
        scrape_telegram_seamenjob, scrape_telegram_marinepublic,
        scrape_linkedin_rss,
    ]
    for fn in scrapers:
        name = fn.__name__.replace("scrape_", "")
        print(f"  → {name}... ", end="", flush=True)
        try:
            found = fn()
            print(f"{len(found)} match(es)")
            all_jobs.extend(found)
        except Exception as e:
            print(f"ERROR: {e}")

    new_jobs = []
    new_seen = set(seen)
    now_iso  = datetime.datetime.utcnow().isoformat() + "Z"
    for j in all_jobs:
        jid = job_id(j["title"], j["url"])
        if jid not in seen:
            j["seen_at"] = now_iso
            new_jobs.append(j)
            new_seen.add(jid)

    print(f"\n📋 Nuevas ofertas: {len(new_jobs)}")
    send_email(new_jobs)
    save_seen(new_seen)

    # ── Actualizar docs/jobs.json para el viewer ──────────────────────────────
    # Guardar TODOS los jobs encontrados hoy (no solo los nuevos)
    # para que el viewer siempre tenga datos frescos aunque no se corra generate_viewer.py
    json_path = Path("docs/jobs.json")
    if json_path.parent.exists():
        try:
            KEEP_DAYS = 15
            cutoff    = datetime.datetime.utcnow() - datetime.timedelta(days=KEEP_DAYS)
            existing  = []
            if json_path.exists():
                data = json.loads(json_path.read_text(encoding="utf-8"))
                for j in data.get("jobs", []):
                    sat = j.get("seen_at", "")
                    if sat:
                        try:
                            if datetime.datetime.fromisoformat(sat.replace("Z","")) > cutoff:
                                existing.append(j)
                        except Exception:
                            pass
            # Combinar: all_jobs frescos + histórico sin solapamiento
            fresh_urls = {j["url"] for j in all_jobs}
            combined   = all_jobs + [j for j in existing if j["url"] not in fresh_urls]
            json_path.write_text(json.dumps(
                {"generated_at": now_iso, "total": len(combined), "jobs": combined},
                ensure_ascii=False
            ), encoding="utf-8")
            print(f"📦 jobs.json actualizado: {len(combined)} ofertas")
        except Exception as e:
            print(f"  ⚠ No se pudo actualizar jobs.json: {e}")

if __name__ == "__main__":
    main()
