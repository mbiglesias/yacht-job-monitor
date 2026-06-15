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

    # Usar el MÍNIMO de los valores encontrados en salary context.
    # Razón: si hay valores de distintos jobs en la misma página (contaminación),
    # el salario real del job actual es el más bajo (los otros son de otros anuncios).
    # Esto puede subestimar en rangos "6500-8000" pero es más seguro que sobreestimar.
    if salary_windows:
        return min(found) if found else None
    
    # Sin keywords de salario: no reportar
    return None

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
    # Strip common prefixes like "Posted: ", "Published: "
    s = re.sub(r'^(?:posted|published|listed|added|date)[:\s]+', '', s, flags=re.IGNORECASE).strip()
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

def _parse_relative_date(s: str):
    """Convierte 'X days ago', 'X weeks ago', 'yesterday', 'today' a fecha absoluta."""
    if not s: return None
    t = s.lower().strip()
    today = datetime.datetime.utcnow()
    m = re.search(r'(\d+)\s+day', t)
    if m:
        d = today - datetime.timedelta(days=int(m.group(1)))
        return f"{d.day:02d} {list(calendar.month_abbr)[d.month]} {d.year}"
    m = re.search(r'(\d+)\s+week', t)
    if m:
        d = today - datetime.timedelta(weeks=int(m.group(1)))
        return f"{d.day:02d} {list(calendar.month_abbr)[d.month]} {d.year}"
    m = re.search(r'(\d+)\s+month', t)
    if m:
        d = today - datetime.timedelta(days=int(m.group(1)) * 30)
        return f"{d.day:02d} {list(calendar.month_abbr)[d.month]} {d.year}"
    if "yesterday" in t:
        d = today - datetime.timedelta(days=1)
        return f"{d.day:02d} {list(calendar.month_abbr)[d.month]} {d.year}"
    if "today" in t or "just now" in t or "hours ago" in t or "minutes ago" in t:
        return f"{today.day:02d} {list(calendar.month_abbr)[today.month]} {today.year}"
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

    # 1. Meta tags (más confiables)
    for meta_name in ["article:published_time", "datePublished", "date", "publish_date"]:
        tag = soup.find("meta", property=meta_name) or soup.find("meta", attrs={"name": meta_name})
        if tag and tag.get("content"):
            posted = _parse_date(tag["content"])
            if posted: break

    # 2. Elementos <time>
    if not posted:
        for el in soup.find_all("time"):
            dt = el.get("datetime") or el.get_text(strip=True)
            if dt:
                posted = _parse_date(dt) or _parse_relative_date(dt)
                if posted: break

    # 3. data-date / data-published
    if not posted:
        for el in soup.find_all(attrs={"data-date": True}):
            posted = _parse_date(el["data-date"]) or _parse_relative_date(el["data-date"])
            if posted: break
        for el in soup.find_all(attrs={"data-published": True}):
            posted = _parse_date(el["data-published"]) or _parse_relative_date(el["data-published"])
            if posted: break

    # 4. Texto cerca de keywords de fecha
    if not posted:
        for el in soup.find_all(["span", "div", "p", "td", "li", "abbr", "small"]):
            raw = el.get_text(strip=True)
            raw_lo = raw.lower()
            if any(kw in raw_lo for kw in ["posted:", "published:", "date posted",
                                            "date published", "listed:", "added:"]):
                posted = _parse_date(raw) or _parse_relative_date(raw)
                if posted: break
            if re.search(r'\b\d+\s+(?:day|week|month)s?\s+ago\b', raw_lo):
                posted = _parse_relative_date(raw)
                if posted: break
            if raw_lo in ("today", "yesterday"):
                posted = _parse_relative_date(raw)
                if posted: break

    for tag in soup(["nav", "footer", "script", "style", "header"]):
        tag.decompose()
    full_text = soup.get_text(" ", strip=True)

    if not posted:
        posted = _parse_date_from_text(full_text[:2000])
    if not posted:
        m = re.search(r'\b(\d+\s+(?:day|week|month)s?\s+ago|yesterday|today)\b',
                      full_text[:2000], re.IGNORECASE)
        if m:
            posted = _parse_relative_date(m.group(1))

    # IMPORTANTE: limitar el texto a los primeros 2000 chars
    # Evita que jobs adyacentes en la página contaminen el salario de este job
    text = full_text[:2000]

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
            return {"passes": False}
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

def _parse_text_jobs(text: str, source: str, page_url: str, max_results: int = 15) -> list:
    """
    Extrae ofertas de trabajo del texto plano de una página JS-rendered.
    Usado cuando el HTML tiene el contenido en texto pero los links están en JS.
    La URL de la oferta apunta a la página del listado (no individual).
    """
    # Patrones de líneas que NO son títulos de jobs (navegación, stats, headers)
    NAV_PATTERNS = [
        r'^\d+\s+contract', r'^engineering\s+\d+', r'^deck\s+\d+',
        r'^interior\s+\d+', r'^galley\s+\d+', r'sort by', r'filter',
        r'results found', r'^\d+\s+jobs', r'per page', r'relevance',
        r'department', r'distance', r'^position\s', r'^contract type',
        r'^salary range', r'^location\s*:', r'cookie', r'privacy policy',
        r'terms of', r'sign in', r'log in', r'register', r'subscribe',
    ]
    ROLE_KWS = [
        'chief engineer', 'sole engineer', '1st engineer', '2nd engineer',
        '3rd engineer', 'junior engineer', 'relief engineer', 'eto',
        'electro-technical', 'electrotechnical', 'engineer'
    ]

    def is_nav(line):
        t = line.lower().strip()
        return any(re.search(p, t) for p in NAV_PATTERNS)

    def is_job_title(line):
        t = line.lower().strip()
        if not any(kw in t for kw in ROLE_KWS): return False
        if is_nav(line): return False
        if len(line) < 6 or len(line) > 150: return False
        if t in ['engineering', 'engineer', 'eto']: return False
        return True

    lines = [l.strip() for l in text.split('\n') if l.strip()]
    jobs = []
    seen_titles = set()

    for i, line in enumerate(lines):
        if not is_job_title(line): continue
        if line in seen_titles: continue
        seen_titles.add(line)

        # Collect context: current line + next 4 lines
        context = ' '.join(lines[i:i+5])

        result = score_job(line, context)
        if result['passes']:
            # Use page URL + anchor for dedup (can't get individual job URLs)
            slug = re.sub(r'[^a-z0-9]+', '-', line.lower()).strip('-')[:60]
            url = f"{page_url}#{slug}"
            jobs.append({
                'title':    line,
                'url':      url,
                'source':   source,
                'tags':     result['tags'],
                'warnings': result['warnings'],
                'posted':   result.get('posted'),
            })
            if len(jobs) >= max_results:
                break

    return jobs


def scrape_bluewateryachting():
    # Log: 74KB, links son /yachts-for-sale — jobs en texto del body
    url = "https://www.bluewateryachting.com/crew-placement/yacht-crew/jobs"
    html = get(url)
    if not html: return []
    soup = BeautifulSoup(html, "html.parser")
    # Primero intentar links normales
    jobs = _extract_jobs(soup, "https://www.bluewateryachting.com",
                         "Bluewater Yachting", ["/job/", "/jobs/", "crew-placement/yacht-crew/jobs/"])
    if jobs: return jobs
    # Fallback: parsear texto del body
    for tag in soup(["nav", "footer", "script", "style", "header"]):
        tag.decompose()
    text = soup.get_text('\n', strip=True)
    return _parse_text_jobs(text, "Bluewater Yachting", url)


def scrape_saltwater():
    # Log: 657KB, links son /about-us/ — jobs en texto del body
    url = "https://www.saltwaterrecruitment.com/jobs/?category=engineering"
    html = get(url)
    if not html: return []
    soup = BeautifulSoup(html, "html.parser")
    jobs = _extract_jobs(soup, "https://www.saltwaterrecruitment.com",
                         "Saltwater Recruitment",
                         ["/job/", "/jobs/", "/vacancy/", "/vacancies/", "/role/"])
    if jobs: return jobs
    for tag in soup(["nav", "footer", "script", "style", "header"]):
        tag.decompose()
    text = soup.get_text('\n', strip=True)
    return _parse_text_jobs(text, "Saltwater Recruitment", url)


def scrape_faststream():
    # Log: 998KB, links son /register/ /login/ — jobs en texto
    url = "https://www.faststream.com/jobs/superyacht-jobs/?department=engineering"
    html = get(url)
    if not html: return []
    soup = BeautifulSoup(html, "html.parser")
    jobs = _extract_jobs(soup, "https://www.faststream.com",
                         "Faststream", ["/job/", "/superyacht-jobs/"])
    if jobs: return jobs
    for tag in soup(["nav", "footer", "script", "style", "header"]):
        tag.decompose()
    text = soup.get_text('\n', strip=True)
    return _parse_text_jobs(text, "Faststream", url)


def scrape_crewnetwork():
    # Log: 184KB, links son navegación — jobs en texto del body
    url = "https://www.crewnetwork.com/looking-for-a-job/"
    html = get(url)
    if not html: return []
    soup = BeautifulSoup(html, "html.parser")
    jobs = _extract_jobs(soup, "https://www.crewnetwork.com",
                         "Crew Network", ["/job/", "/vacancy/", "/vacancies/"])
    if jobs: return jobs
    for tag in soup(["nav", "footer", "script", "style", "header"]):
        tag.decompose()
    text = soup.get_text('\n', strip=True)
    return _parse_text_jobs(text, "Crew Network", url)



def scrape_findacrew():
    # 404 — URL cambió. Probar variantes
    for url in [
        "https://www.findacrew.net/en/jobs/search?q=engineer",
        "https://www.findacrew.net/en/jobs",
        "https://www.findacrew.com/en/jobs",
    ]:
        html = get(url)
        if not html: continue
        soup = BeautifulSoup(html, "html.parser")
        jobs = _extract_jobs(soup, url.split("/en/")[0],
                             "Find a Crew", ["/job/", "/jobs/", "/en/jobs/"])
        if jobs: return jobs
    return []

def scrape_yacrew():
    # URL correcta: /engineering-yacht-jobs/
    # También scrapear páginas de agencias específicas en YaCrew
    all_jobs = []
    urls = [
        "https://www.yacrew.com/engineering-yacht-jobs/",
        "https://www.yacrew.com/jobs/wilsonhalligan/",   # 44 jobs activos de Wilsonhalligan
        "https://www.yacrew.com/jobs/saltwater/",
        "https://www.yacrew.com/jobs/bespoke-crew/",
    ]
    seen = set()
    for url in urls:
        html = get(url)
        if not html: continue
        soup = BeautifulSoup(html, "html.parser")
        found = _extract_jobs(soup, "https://www.yacrew.com",
                             "YaCrew", ["/job", "/vacancy", "/vacanc", "/jobs/"])
        for j in found:
            if j["url"] not in seen:
                seen.add(j["url"])
                all_jobs.append(j)
    return all_jobs[:30]

def scrape_crewin():
    # SSL error — skip SSL verification
    try:
        import requests as _req
        r = _req.get("https://www.crewin.com/jobs", headers=HEADERS, timeout=10, verify=False)
        if r.ok:
            soup = BeautifulSoup(r.text, "html.parser")
            return _extract_jobs(soup, "https://www.crewin.com",
                                 "Crewin", ["/job/", "/jobs/", "/vacancy/", "/role/"])
    except Exception:
        pass
    return []

def scrape_ypicrew():
    # 404 — URL cambió
    for url in [
        "https://www.ypicrew.com/find-a-job/",
        "https://www.ypicrew.com/vacancies/",
        "https://www.ypicrew.com/jobs/",
    ]:
        html = get(url)
        if not html: continue
        soup = BeautifulSoup(html, "html.parser")
        jobs = _extract_jobs(soup, "https://www.ypicrew.com",
                             "YPI Crew", ["/job/", "/jobs/", "/vacancy/", "/find-a-job/"])
        if jobs: return jobs
    return []



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
    # Log: 213KB, links son /about-us/ /services/ — jobs cargan via JS
    # pero el texto contiene "Rotational Temporary Permanent Job Location Type"
    # Probar URL directa de job listings
    for url in [
        "https://www.wilsonhalligan.com/our-current-roles/",
        "https://www.wilsonhalligan.com/vacancies/",
        "https://www.wilsonhalligan.com/jobs/",
    ]:
        html = get(url)
        if not html: continue
        soup = BeautifulSoup(html, "html.parser")
        jobs = _extract_jobs(soup, "https://www.wilsonhalligan.com",
                             "Wilsonhalligan", ["/job/", "/vacancy/", "/role/", "/position/", "/our-current-roles/"])
        if jobs: return jobs
    return []

def scrape_quaycrew():
    # Log: 45KB, links incluyen /superyacht-jobs/crew-resources/ y jobs.quaygroup.com
    # Probar URL del job listing directa
    for url in [
        "https://jobs.quaygroup.com/sectors/4/yacht-engineering-jobs.aspx",
        "https://jobs.quaygroup.com/search/?q=engineer+yacht",
        "https://www.quaygroup.com/superyacht-jobs/",
    ]:
        html = get(url)
        if not html: continue
        soup = BeautifulSoup(html, "html.parser")
        jobs = _extract_jobs(soup, "https://jobs.quaygroup.com",
                             "Quay Crew", ["/job/", "/jobs/", "/vacancy/", "aspx", "/engineer"])
        if jobs: return jobs
    return []

def scrape_northropjohnson():
    # Log: 153KB, links son /yachts-for-sale/ /superyachts/ — URL incorrecta
    # Desde el log: links incluyen /crew/find-a-job/ — usar esa URL
    for url in [
        "https://www.northropandjohnson.com/crew/find-a-job/",
        "https://crew.northropandjohnson.com/crew-jobs/",
        "https://www.northropandjohnson.com/crew/",
    ]:
        html = get(url)
        if not html: continue
        soup = BeautifulSoup(html, "html.parser")
        jobs = _extract_jobs(soup, "https://www.northropandjohnson.com",
                             "Northrop & Johnson", ["/job/", "/jobs/", "/vacancy/", "/crew/find-a-job/"])
        if jobs: return jobs
    return []

def scrape_xelvin():
    # 404 en /en/vacancies/?sector=yacht-shipbuilding
    # Probar variantes
    for url in [
        "https://www.xelvin.nl/vacatures/?sector=jacht-scheepsbouw",
        "https://www.xelvin.nl/en/vacancies/",
        "https://www.xelvin.nl/en/vacancies/?sector=marine",
        "https://www.xelvin.nl/vacatures/",
    ]:
        html = get(url)
        if not html: continue
        soup = BeautifulSoup(html, "html.parser")
        jobs = _extract_jobs(soup, "https://www.xelvin.nl",
                             "Xelvin", ["/vacature/", "/vacancy/", "/job/", "/jobs/"])
        if jobs: return jobs
    return []

def scrape_crewseekers():
    """Crewseekers — plataforma veterana de crew para yates, gratis para buscar"""
    html = get("https://www.crewseekers.net/vacancy_list.cfm?type=motor")
    soup = BeautifulSoup(html, "html.parser") if html else None
    if not soup: return []
    return _extract_jobs(soup, "https://www.crewseekers.net",
                         "Crewseekers", ["/vacancy", "/job", "cfm?"])

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

def _extract_json_jobs(html: str, base_url: str, source: str) -> list:
    """
    Fallback para sitios JS-rendered: extrae ofertas de JSON embebido en el HTML.
    Busca __NEXT_DATA__, window.__INITIAL_STATE__, y otros patrones comunes.
    """
    import json as _json
    jobs = []
    
    # 1. Next.js __NEXT_DATA__
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if m:
        try:
            data = _json.loads(m.group(1))
            # Buscar arrays que parezcan listas de jobs en cualquier nivel
            text = _json.dumps(data)
            # Si contiene keywords de engineer, vale explorarlo
            if any(kw in text.lower() for kw in ["engineer", "eto", "chief engineer"]):
                print(f"      → __NEXT_DATA__ encontrado, {len(text)} chars")
                print(f"        keys: {list(data.get('props',{}).get('pageProps',{}).keys())[:5]}")
        except Exception:
            pass

    # 2. Imprimir primeros 500 chars del body para debug
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    body_text = soup.get_text(" ", strip=True)[:300]
    print(f"      → body preview: {body_text[:200]}")
    
    # 3. Contar todos los links
    all_links = [a.get("href","") for a in BeautifulSoup(html, "html.parser").find_all("a", href=True)]
    print(f"      → total links: {len(all_links)}")
    if all_links:
        sample = [h for h in all_links if h and not h.startswith("#")][:5]
        print(f"      → sample links: {sample}")
    
    return jobs

def main():
    print("🔍 Filtros: Engineer | Rotación | ~2 meses | ≥6.000€/mes | Solo yates")
    seen = load_seen()
    all_jobs = []
    
    # URLs para debug de sitios que dan 0
    debug_urls = {
        "crewnetwork":     "https://www.crewnetwork.com/looking-for-a-job/",
        "bluewateryachting": "https://www.bluewateryachting.com/crew-placement/yacht-crew/jobs",
        "saltwater":       "https://www.saltwaterrecruitment.com/jobs/?category=engineering",
        "faststream":      "https://www.faststream.com/jobs/superyacht-jobs/?department=engineering",
        "wilsonhalligan":  "https://www.wilsonhalligan.com/our-current-roles/",
        "quaycrew":        "https://jobs.quaygroup.com/sectors/4/yacht-engineering-jobs.aspx",
        "northropjohnson": "https://crew.northropandjohnson.com/crew-jobs/",
    }
    
    scrapers = [
        scrape_yotspot, scrape_crewnetwork, scrape_bluewateryachting,
        scrape_findacrew, scrape_yacrew,
        scrape_saltwater, scrape_crewin, scrape_faststream, scrape_ypicrew,
        scrape_mycrewkit, scrape_bespokecrew, scrape_wilsonhalligan,
        scrape_quaycrew, scrape_northropjohnson, scrape_xelvin,
        scrape_crewseekers,
        scrape_telegram_seamenjob, scrape_telegram_marinepublic,
        scrape_linkedin_rss,
    ]
    for fn in scrapers:
        name = fn.__name__.replace("scrape_", "")
        print(f"  → {name}... ", end="", flush=True)
        try:
            found = fn()
            print(f"{len(found)} match(es)")
            for j in found[:2]:
                print(f"      ✓ {j['title'][:70]}")
                print(f"        tags={j.get('tags',[])} | posted={j.get('posted')}")
            # Si da 0 y tiene URL de debug, mostrar el HTML
            if not found and name in debug_urls:
                html = get(debug_urls[name])
                if html:
                    print(f"      → HTTP OK, {len(html)} bytes — analizando estructura...")
                    _extract_json_jobs(html, debug_urls[name], name)
                else:
                    print(f"      → fetch falló (403/404/timeout)")
            all_jobs.extend(found)
        except Exception as e:
            print(f"ERROR: {e}")
            import traceback; traceback.print_exc()

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
            KEEP_DAYS = 30
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
