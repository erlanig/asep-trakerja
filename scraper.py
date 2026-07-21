import re
import time
from datetime import datetime, date
from curl_cffi import requests
from bs4 import BeautifulSoup

# ==========================================
# FUNGSI BANTUAN UMUM
# ==========================================
def _parse_iso_date(iso_string: str) -> str:
    if not iso_string:
        return date.today().isoformat()
    try:
        return datetime.fromisoformat(iso_string.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return date.today().isoformat()


def _text_or_none(el):
    return el.get_text(strip=True) if el else None


def _parse_gaji_dari_teks(teks: str) -> str | None:
    """
    Cari pola gaji dalam teks (mendukung IDR, Rp, format dengan titik/koma).
    """
    # Format "IDR 5.000.000 - 10.000.000" atau "Rp5.000.000 - Rp10.000.000"
    pola_range = re.search(r'(?:IDR|Rp)\s?[\d.,]+\s*-\s*(?:IDR|Rp)?\s?[\d.,]+', teks, re.IGNORECASE)
    if pola_range:
        return pola_range.group().strip()
    # Format tunggal "IDR 10.000.000" atau "Rp10.000.000"
    pola_tunggal = re.search(r'(?:IDR|Rp)\s?[\d.,]+', teks, re.IGNORECASE)
    if pola_tunggal:
        return pola_tunggal.group().strip()
    return None


# ==========================================
# SCRAPER: KARIR.COM (API)
# ==========================================
KARIR_BASE_API = "https://gateway2-beta.karir.com"
KARIR_BASE_JOB_URL = "https://karir.com/opportunities/{id}"

KARIR_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://karir.com",
    "Referer": "https://karir.com/",
}

def _format_gaji_karir(item: dict) -> str | None:
    lower = item.get("salary_lower")
    upper = item.get("salary_upper")
    if lower and upper:
        return f"Rp{lower:,.0f} - Rp{upper:,.0f}".replace(",", ".")
    return None

def get_job_detail_karir(opportunity_id: int) -> str:
    url = f"{KARIR_BASE_API}/v1/opportunity/detail"
    body = {"opportunity_id": opportunity_id, "language": "id"}
    try:
        resp = requests.post(url, headers=KARIR_HEADERS, json=body, timeout=10, impersonate="chrome110")
        resp.raise_for_status()
        data = resp.json().get("data", {})
        return data.get("job_description") or data.get("description") or ""
    except Exception as e:
        print(f"⚠️ Gagal fetch detail Karir.com untuk ID {opportunity_id}: {e}")
        return ""

def scrape_karir_com(limit: int = 10, offset: int = 0) -> list[dict]:
    print("Mencari lowongan di Karir.com...")
    hasil = []
    url = f"{KARIR_BASE_API}/v2/search/opportunities"
    body = {
        "job_function_ids": [],
        "limit": limit,
        "offset": offset,
        "sort_order": "newest",
        "is_opportunity": True,
    }

    try:
        resp = requests.post(url, headers=KARIR_HEADERS, json=body, timeout=15, impersonate="chrome110")
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        print(f"❌ Gagal fetch API karir.com: {e}")
        return hasil

    opportunities = payload.get("data", {}).get("opportunities", [])

    for item in opportunities:
        job_id = item.get("id")
        judul = item.get("job_position")
        if not judul or not job_id:
            continue

        perusahaan = item.get("company_name")
        deskripsi_lengkap = get_job_detail_karir(job_id)
        time.sleep(0.5)

        hasil.append({
            "judul": judul,
            "perusahaan": perusahaan or "Perusahaan dirahasiakan",
            "lokasi": item.get("description") or "",
            "tipe_kerja": "full-time",
            "kategori": None,
            "deskripsi": deskripsi_lengkap,
            "gaji": _format_gaji_karir(item),
            "sumber_platform": "karir.com",
            "sumber_url": KARIR_BASE_JOB_URL.format(id=job_id),
            "tanggal_post": _parse_iso_date(item.get("posted_at")),
        })

    return hasil


# ==========================================
# SCRAPER: GLINTS.COM (HTML SELECTOR, TANPA API)
# ==========================================
GLINTS_JOB_LINK_RE = re.compile(r'/opportunities/jobs/([a-zA-Z0-9-]+)')

def scrape_glints(keyword: str = "", location: str = "Indonesia", limit: int = 10) -> list[dict]:
    """
    Scrape Glints dari halaman pencarian publik (HTML) tanpa GraphQL API.
    URL: https://glints.com/id/opportunities/jobs/explore?keyword=...&country=ID&locationName=...
    """
    print("Mencari lowongan di Glints (HTML scraping)...")
    hasil = []
    base_url = "https://glints.com/id/opportunities/jobs/explore"
    params = {
        "keyword": keyword,
        "country": "ID",
        "locationName": location,
    }
    # Hanya sertakan parameter yang tidak kosong
    query_string = "&".join(f"{k}={v.replace(' ', '%20')}" for k, v in params.items() if v)
    url = f"{base_url}?{query_string}" if query_string else base_url

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    try:
        resp = requests.get(url, headers=headers, impersonate="chrome124", timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"❌ Gagal fetch halaman Glints: {e}")
        return hasil

    soup = BeautifulSoup(resp.text, "html.parser")

    # Cari semua link lowongan (href mengandung '/opportunities/jobs/')
    links = soup.find_all("a", href=GLINTS_JOB_LINK_RE)
    seen_ids = set()

    for a in links:
        href = a["href"]
        m = GLINTS_JOB_LINK_RE.search(href)
        if not m:
            continue
        job_id = m.group(1)
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)

        # Judul: teks dari elemen <a> itu sendiri atau child terdekat
        judul = a.get_text(strip=True)
        if not judul:
            # Coba ambil dari elemen dengan class tertentu di sekitar
            judul_el = a.find_previous("h3") or a.find_next("h3")
            judul = _text_or_none(judul_el)
        if not judul:
            continue

        # Cari container (naik beberapa level) untuk mendapatkan informasi perusahaan, lokasi, gaji
        container = a
        for _ in range(5):
            container = container.find_parent()
            if container is None:
                break
            teks_container = container.get_text(" ", strip=True)
            if len(teks_container) > 50:   # container yang cukup besar
                break

        teks_kartu = teks_container if container else ""

        # Perusahaan: biasanya ada di elemen dengan class 'company' atau setelah judul
        perusahaan = "Tidak diketahui"
        company_el = container.find("span", class_=re.compile(r"company", re.I)) if container else None
        if not company_el:
            # Coba ambil dari teks setelah judul, biasanya pola "di PT ..."
            match_company = re.search(r'(?:di|at)\s+([A-Za-z0-9\s&.]+)', teks_kartu)
            if match_company:
                perusahaan = match_company.group(1).strip()
        else:
            perusahaan = _text_or_none(company_el) or perusahaan

        # Lokasi: cari elemen dengan class 'location' atau teks setelah ikon lokasi
        lokasi = "Indonesia"
        loc_el = container.find("span", class_=re.compile(r"location", re.I)) if container else None
        if loc_el:
            lokasi = _text_or_none(loc_el) or lokasi
        else:
            # Coba dari teks: pola "Jakarta", "Bandung", dll. (kota umum)
            cities = ["Jakarta", "Bandung", "Surabaya", "Yogyakarta", "Tangerang", "Remote"]
            for city in cities:
                if city.lower() in teks_kartu.lower():
                    lokasi = city
                    break

        # Gaji dari teks
        gaji = _parse_gaji_dari_teks(teks_kartu)

        # Tipe kerja: cari kata kunci di teks
        tipe = "unknown"
        if any(kata in teks_kartu.lower() for kata in ["full-time", "full time", "penuh waktu"]):
            tipe = "full-time"
        elif any(kata in teks_kartu.lower() for kata in ["part-time", "part time", "paruh waktu"]):
            tipe = "part-time"
        elif any(kata in teks_kartu.lower() for kata in ["contract", "kontrak"]):
            tipe = "kontrak"
        elif "intern" in teks_kartu.lower() or "magang" in teks_kartu.lower():
            tipe = "magang"

        sumber_url = f"https://glints.com{href}" if href.startswith("/") else href

        hasil.append({
            "judul": judul,
            "perusahaan": perusahaan,
            "lokasi": lokasi,
            "tipe_kerja": tipe,
            "kategori": None,
            "deskripsi": "",
            "gaji": gaji,
            "sumber_platform": "glints",
            "sumber_url": sumber_url,
            "tanggal_post": date.today().isoformat(),
        })

        if len(hasil) >= limit:
            break

    if not hasil:
        print("⚠️ 0 lowongan Glints ditemukan. Struktur halaman mungkin berubah. "
              "Jalankan debug_page_structure(url) untuk inspeksi ulang.")

    return hasil


# ==========================================
# LAPIS HTML/CSS SELECTOR — untuk situs lain
# ==========================================
SITE_CONFIGS = {
    "linkedin": {
        "search_url": (
            "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
            "?keywords={keyword}&location={location}&start={start}"
        ),
        "impersonate": "chrome124",
        "needs_browser": False,
        "selector_job_card": "div.base-card",
        "selector_title": "h3.base-search-card__title",
        "selector_company": "h4.base-search-card__subtitle",
        "selector_location": "span.job-search-card__location",
        "selector_link": "a.base-card__full-link",
        "link_prefix": "",
    },
}

# ==========================================
# SCRAPER: KALIBRR.ID (HTML)
# ==========================================
KALIBRR_BASE = "https://www.kalibrr.id"
KALIBRR_JOB_LINK_RE = re.compile(r'^(/c/[^/]+/jobs/(\d+)/[^?]+)$')

def _parse_gaji_kalibrr(text: str) -> str | None:
    match = re.search(r'(IDR\s?[\d.,]+\s*-\s*IDR\s?[\d.,]+\s*/?\s*month)', text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(r'(IDR\s?[\d.,]+\s*/?\s*month)', text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None

def scrape_kalibrr(path: str = "/home/all-jobs", limit: int = 10) -> list[dict]:
    print("Mencari lowongan di Kalibrr...")
    hasil = []
    url = f"{KALIBRR_BASE}{path}"

    try:
        resp = requests.get(url, impersonate="chrome124", timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"❌ Gagal fetch Kalibrr: {e}")
        return hasil

    soup = BeautifulSoup(resp.text, "html.parser")
    seen_ids = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        path_only = href
        for base in ("https://www.kalibrr.id", "https://www.kalibrr.com"):
            if href.startswith(base):
                path_only = href[len(base):]
                break

        m = KALIBRR_JOB_LINK_RE.match(path_only)
        if not m:
            continue

        job_id = m.group(2)
        if job_id in seen_ids:
            continue

        judul = a.get_text(strip=True)
        if not judul:
            continue
        seen_ids.add(job_id)

        perusahaan = "Tidak diketahui"
        next_a = a.find_next("a", href=True)
        if next_a:
            teks = next_a.get_text(strip=True)
            if teks:
                perusahaan = teks

        container = a
        card_text = ""
        for _ in range(6):
            container = container.find_parent()
            if container is None:
                break
            card_text = container.get_text(" ", strip=True)
            if len(card_text) > 100:
                break

        lokasi_match = re.search(r'([A-Za-zÀ-ÿ .\'-]+,\s*Indonesia)', card_text)
        lokasi = lokasi_match.group(1).strip() if lokasi_match else "Indonesia"

        gaji = _parse_gaji_kalibrr(card_text)

        if "FULL_TIME" in card_text:
            tipe = "full-time"
        elif "PART_TIME" in card_text:
            tipe = "part-time"
        elif "CONTRACTOR" in card_text:
            tipe = "kontrak"
        else:
            tipe = "unknown"

        hasil.append({
            "judul": judul,
            "perusahaan": perusahaan,
            "lokasi": lokasi,
            "tipe_kerja": tipe,
            "kategori": None,
            "deskripsi": "",
            "gaji": gaji,
            "sumber_platform": "kalibrr",
            "sumber_url": f"{KALIBRR_BASE}{m.group(1)}",
            "tanggal_post": date.today().isoformat(),
        })

        if len(hasil) >= limit:
            break

    if not hasil:
        print("⚠️ 0 lowongan Kalibrr ditemukan. Struktur mungkin berubah.")

    return hasil


# ==========================================
# SCRAPER: DEALLS.COM (HTML)
# ==========================================
DEALLS_BASE = "https://dealls.com"
DEALLS_JOB_LINK_RE = re.compile(r'^(/loker/([a-z0-9-]+)~([a-z0-9-]+))$')

def _parse_gaji_dealls(text: str) -> str | None:
    match = re.search(r'(Rp\s?[\d.,]+\s*-\s*Rp\s?[\d.,]+)', text)
    if match:
        return match.group(1).strip()
    match = re.search(r'(Rp\s?[\d.,]+)', text)
    if match and "Negotiable" not in text:
        return match.group(1).strip()
    return None

def scrape_dealls(path: str = "/loker/populer/loker-software-engineer-jakarta", limit: int = 10) -> list[dict]:
    print("Mencari lowongan di Dealls...")
    hasil = []
    url = f"{DEALLS_BASE}{path}"

    try:
        resp = requests.get(url, impersonate="chrome124", timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"❌ Gagal fetch Dealls: {e}")
        return hasil

    soup = BeautifulSoup(resp.text, "html.parser")
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        path_only = href
        if href.startswith(DEALLS_BASE):
            path_only = href[len(DEALLS_BASE):]

        m = DEALLS_JOB_LINK_RE.match(path_only)
        if not m:
            continue

        if path_only in seen:
            continue
        seen.add(path_only)

        slug_judul = m.group(2).replace("-", " ").title()
        slug_perusahaan = m.group(3).replace("-", " ").title()

        full_text = a.get_text(" ", strip=True)

        tipe = "unknown"
        if "Penuh waktu" in full_text:
            tipe = "full-time"
        elif "Paruh waktu" in full_text:
            tipe = "part-time"
        elif "Kontrak" in full_text:
            tipe = "kontrak"
        elif "Magang" in full_text:
            tipe = "magang"

        lokasi_match = re.search(r'•\s*([A-Za-zÀ-ÿ .\'-]+?)(?:Min\.|Rp|Negotiable|$)', full_text)
        lokasi = lokasi_match.group(1).strip() if lokasi_match else "Indonesia"

        gaji = _parse_gaji_dealls(full_text)

        hasil.append({
            "judul": slug_judul,
            "perusahaan": slug_perusahaan,
            "lokasi": lokasi,
            "tipe_kerja": tipe,
            "kategori": None,
            "deskripsi": "",
            "gaji": gaji,
            "sumber_platform": "dealls",
            "sumber_url": f"{DEALLS_BASE}{path_only}",
            "tanggal_post": date.today().isoformat(),
        })

        if len(hasil) >= limit:
            break

    if not hasil:
        print("⚠️ 0 lowongan Dealls ditemukan. Path mungkin tidak valid.")

    return hasil


# ==========================================
# SCRAPER: LINKEDIN (guest API HTML)
# ==========================================
def scrape_linkedin(keyword: str = "software engineer", location: str = "Indonesia",
                     limit: int = 10) -> list[dict]:
    print("Mencari lowongan di LinkedIn...")
    hasil = []
    cfg = SITE_CONFIGS["linkedin"]

    url = cfg["search_url"].format(
        keyword=keyword.replace(" ", "%20"),
        location=location.replace(" ", "%20"),
        start=0,
    )

    try:
        resp = requests.get(url, impersonate=cfg["impersonate"], timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"❌ Gagal fetch LinkedIn: {e}")
        return hasil

    soup = BeautifulSoup(resp.text, "html.parser")
    cards = soup.select(cfg["selector_job_card"])[:limit]

    if not cards:
        print("⚠️ 0 job card LinkedIn ditemukan — struktur mungkin berubah atau diblokir.")
        return hasil

    for card in cards:
        judul = _text_or_none(card.select_one(cfg["selector_title"]))
        if not judul:
            continue

        perusahaan = _text_or_none(card.select_one(cfg["selector_company"])) or "Tidak diketahui"
        lokasi = _text_or_none(card.select_one(cfg["selector_location"])) or location

        link_el = card.select_one(cfg["selector_link"])
        url_lowongan = link_el.get("href").split("?")[0] if link_el and link_el.get("href") else None

        hasil.append({
            "judul": judul,
            "perusahaan": perusahaan,
            "lokasi": lokasi,
            "tipe_kerja": "unknown",
            "kategori": None,
            "deskripsi": "",
            "gaji": None,
            "sumber_platform": "linkedin",
            "sumber_url": url_lowongan,
            "tanggal_post": date.today().isoformat(),
        })

    return hasil


# ==========================================
# SCRAPER: TALENTICS (MULTI‑URL + FALLBACK LINKEDIN)
# ==========================================
TALENTICS_CAREER_URLS = [
    "https://talentics.id/careers",
    "https://talentics.id/karir",
    "https://talentics.id/jobs",
    "https://talentics.recruitee.com",
]

def scrape_talentics_direct(url: str, limit: int = 10) -> list[dict]:
    """
    Mencoba scrape halaman karir Talentics dari URL langsung.
    """
    print(f"  Mencoba URL: {url}")
    try:
        resp = requests.get(url, impersonate="chrome124", timeout=10)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        # Cari link yang mengandung kata kunci lowongan
        links = soup.find_all("a", href=True)
        results = []
        for a in links:
            href = a["href"]
            teks = a.get_text(strip=True)
            # Heuristik: link menuju halaman detail lowongan, biasanya mengandung kata "job", "career", "loker"
            if not teks or len(teks) < 5:
                continue
            if any(k in href.lower() for k in ["job", "career", "loker", "position"]):
                full_url = href if href.startswith("http") else url.rstrip("/") + "/" + href.lstrip("/")
                # Ambil informasi sekitar: lokasi, gaji, tipe
                container = a.find_parent("div") or a.find_parent("li")
                teks_card = container.get_text(" ", strip=True) if container else teks

                lokasi = "Tidak diketahui"
                loc_match = re.search(r'(Jakarta|Bandung|Surabaya|Yogyakarta|Remote|Indonesia)', teks_card, re.I)
                if loc_match:
                    lokasi = loc_match.group(1)

                gaji = _parse_gaji_dari_teks(teks_card)
                tipe = "unknown"
                if "full" in teks_card.lower():
                    tipe = "full-time"
                elif "part" in teks_card.lower():
                    tipe = "part-time"
                elif "contract" in teks_card.lower() or "kontrak" in teks_card.lower():
                    tipe = "kontrak"
                elif "intern" in teks_card.lower() or "magang" in teks_card.lower():
                    tipe = "magang"

                results.append({
                    "judul": teks,
                    "perusahaan": "Talentics",
                    "lokasi": lokasi,
                    "tipe_kerja": tipe,
                    "kategori": None,
                    "deskripsi": "",
                    "gaji": gaji,
                    "sumber_platform": "talentics",
                    "sumber_url": full_url,
                    "tanggal_post": date.today().isoformat(),
                })
                if len(results) >= limit:
                    break
        return results
    except Exception:
        return []


def scrape_talentics(limit: int = 10) -> list[dict]:
    print("Mencari lowongan Talentics...")
    # 1. Coba semua URL langsung
    for url in TALENTICS_CAREER_URLS:
        hasil = scrape_talentics_direct(url, limit)
        if hasil:
            print(f"  Berhasil dari {url}")
            return hasil
    # 2. Fallback ke LinkedIn
    print("  Tidak ditemukan halaman karir langsung. Fallback ke LinkedIn...")
    linkedin_jobs = scrape_linkedin(keyword="Talentics", location="Indonesia", limit=limit*2)
    talentics_jobs = [j for j in linkedin_jobs if "talentics" in j["perusahaan"].lower()]
    if not talentics_jobs:
        print("  ⚠️ Tidak ada lowongan Talentics di LinkedIn saat ini.")
    else:
        # Ubah platform agar tetap tercatat sebagai talentics (opsional)
        for job in talentics_jobs:
            job["sumber_platform"] = "talentics"
    return talentics_jobs[:limit]


# ==========================================
# GENERIC HTML / PLAYWRIGHT SCRAPER (TETAP ADA)
# ==========================================
def generic_html_scraper(site_key: str, limit: int = 10, **format_kwargs) -> list[dict]:
    cfg = SITE_CONFIGS.get(site_key)
    if not cfg:
        raise ValueError(f"Config untuk '{site_key}' belum ada di SITE_CONFIGS")

    print(f"Mencari lowongan di {site_key} (HTML scraping)...")
    hasil = []
    search_url = cfg["search_url"].format(**format_kwargs) if format_kwargs else cfg["search_url"]

    try:
        resp = requests.get(search_url, impersonate=cfg.get("impersonate", "chrome124"), timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"❌ Gagal fetch halaman {site_key}: {e}")
        return hasil

    soup = BeautifulSoup(resp.text, "html.parser")
    cards = soup.select(cfg["selector_job_card"])[:limit]

    if not cards:
        print(f"⚠️ 0 job card untuk '{site_key}'. Coba periksa selector.")
        return hasil

    for card in cards:
        judul = _text_or_none(card.select_one(cfg["selector_title"]))
        if not judul:
            continue
        perusahaan = _text_or_none(card.select_one(cfg["selector_company"])) or "Tidak diketahui"
        lokasi = _text_or_none(card.select_one(cfg["selector_location"])) or ""
        link_el = card.select_one(cfg["selector_link"])
        href = link_el.get("href") if link_el else None
        url_lengkap = (cfg.get("link_prefix", "") + href) if href and href.startswith("/") else href

        hasil.append({
            "judul": judul,
            "perusahaan": perusahaan,
            "lokasi": lokasi,
            "tipe_kerja": "unknown",
            "kategori": None,
            "deskripsi": "",
            "gaji": None,
            "sumber_platform": site_key,
            "sumber_url": url_lengkap,
            "tanggal_post": date.today().isoformat(),
        })

    return hasil


def generic_playwright_scraper(site_key: str, limit: int = 10, wait_selector: str | None = None, **format_kwargs) -> list[dict]:
    from playwright.sync_api import sync_playwright

    cfg = SITE_CONFIGS.get(site_key)
    if not cfg:
        raise ValueError(f"Config untuk '{site_key}' belum ada di SITE_CONFIGS")

    print(f"Mencari lowongan di {site_key} (Playwright)...")
    hasil = []
    search_url = cfg["search_url"].format(**format_kwargs) if format_kwargs else cfg["search_url"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(search_url, timeout=30000)
            page.wait_for_selector(wait_selector or cfg["selector_job_card"], timeout=15000)
            html = page.content()
        except Exception as e:
            print(f"❌ Gagal render {site_key}: {e}")
            browser.close()
            return hasil
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(cfg["selector_job_card"])[:limit]

    for card in cards:
        judul = _text_or_none(card.select_one(cfg["selector_title"]))
        if not judul:
            continue
        perusahaan = _text_or_none(card.select_one(cfg["selector_company"])) or "Tidak diketahui"
        lokasi = _text_or_none(card.select_one(cfg["selector_location"])) or ""
        link_el = card.select_one(cfg["selector_link"])
        href = link_el.get("href") if link_el else None
        url_lengkap = (cfg.get("link_prefix", "") + href) if href and href.startswith("/") else href

        hasil.append({
            "judul": judul,
            "perusahaan": perusahaan,
            "lokasi": lokasi,
            "tipe_kerja": "unknown",
            "kategori": None,
            "deskripsi": "",
            "gaji": None,
            "sumber_platform": site_key,
            "sumber_url": url_lengkap,
            "tanggal_post": date.today().isoformat(),
        })

    return hasil


def debug_page_structure(url: str, use_browser: bool = False, save_to: str = "debug_page.html"):
    if use_browser:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=30000)
            page.wait_for_timeout(3000)
            html = page.content()
            browser.close()
    else:
        resp = requests.get(url, impersonate="chrome124", timeout=15)
        html = resp.text

    with open(save_to, "w", encoding="utf-8") as f:
        f.write(html)

    soup = BeautifulSoup(html, "html.parser")
    print(f"✅ HTML disimpan ke: {save_to} ({len(html)} karakter)")
    print(f"   Jumlah tag <a>: {len(soup.find_all('a'))}, tag <div>: {len(soup.find_all('div'))}")
    print("   Buka file di atas dan cari pola job card secara manual (Ctrl+F judul lowongan yang kamu tahu).")


# ==========================================
# AGGREGATOR UTAMA
# ==========================================
def scrape_semua_sumber(limit_per_sumber: int = 5, keyword_linkedin: str = "software engineer") -> list[dict]:
    semua_lowongan = []

    def safe_extend(scraper_fn, *args, **kwargs):
        try:
            res = scraper_fn(*args, **kwargs)
            semua_lowongan.extend(res)
            print(f"    → {len(res)} lowongan dari {scraper_fn.__name__}")
        except Exception as e:
            print(f"❌ Error di {scraper_fn.__name__}: {e}")

    safe_extend(scrape_karir_com, limit=limit_per_sumber)
    time.sleep(1)

    safe_extend(scrape_glints, keyword="", limit=limit_per_sumber)   # keyword kosong dapat semua
    time.sleep(1)

    safe_extend(scrape_linkedin, keyword=keyword_linkedin, limit=limit_per_sumber)
    time.sleep(1)

    safe_extend(scrape_kalibrr, limit=limit_per_sumber)
    time.sleep(1)

    safe_extend(scrape_dealls, limit=limit_per_sumber)
    time.sleep(1)

    safe_extend(scrape_talentics, limit=limit_per_sumber)
    time.sleep(1)

    # Opsional: Jobstreet, Indeed, dll.
    # safe_extend(generic_html_scraper, "jobstreet", limit=limit_per_sumber, keyword="software engineer")
    # time.sleep(2)
    # safe_extend(generic_playwright_scraper, "indeed", limit=limit_per_sumber, keyword="software engineer", location="Indonesia")

    return semua_lowongan


if __name__ == "__main__":
    data_gabungan = scrape_semua_sumber(limit_per_sumber=5)

    print(f"\n✅ Total Ditemukan: {len(data_gabungan)} lowongan dari berbagai sumber:\n")
    for d in data_gabungan:
        print(f"[{d['sumber_platform'].upper()}] {d['judul']} @ {d['perusahaan']} ({d['lokasi']})")
        print(f"  Tipe Kerja : {d['tipe_kerja'].replace('_', ' ').title()}")
        print(f"  Gaji       : {d['gaji']}")
        print(f"  Tanggal    : {d['tanggal_post']}")
        print(f"  Link       : {d['sumber_url']}")
        print("-" * 60)