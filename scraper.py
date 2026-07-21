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
# SCRAPER: GLINTS.COM (API)
# ==========================================
GLINTS_API_URL = "https://glints.com/api/v2-alc/graphql"

GLINTS_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Origin": "https://glints.com",
    "Referer": "https://glints.com/id/opportunities/jobs/explore",
    "x-glints-country-code": "ID",
}

GRAPHQL_QUERY = """
query searchJobsV3($data: JobSearchConditionInput!) {
  searchJobsV3(data: $data) {
    jobsInPage {
      id
      title
      createdAt
      updatedAt
      type
      company { name }
      city { name }
      salaries {
        minAmount
        maxAmount
        CurrencyCode
      }
    }
  }
}
"""

def _format_gaji_glints(salaries: list) -> str | None:
    if not salaries:
        return None
    gaji = salaries[0]
    min_amt = gaji.get("minAmount")
    max_amt = gaji.get("maxAmount")
    currency = gaji.get("CurrencyCode", "IDR")

    if min_amt and max_amt:
        return f"{currency} {min_amt:,.0f} - {max_amt:,.0f}".replace(",", ".")
    return None

def scrape_glints(keyword: str = "", page: int = 1, page_size: int = 10) -> list[dict]:
    print("Mencari lowongan di Glints...")
    hasil = []
    variables = {
        "data": {
            "CountryCode": "ID",
            "includeExternalJobs": True,
            "pageSize": page_size,
            "page": page
        }
    }
    if keyword:
        variables["data"]["keyword"] = keyword

    payload = {
        "operationName": "searchJobsV3",
        "variables": variables,
        "query": GRAPHQL_QUERY
    }

    session = requests.Session(impersonate="chrome120")

    try:
        session.get("https://glints.com/id/opportunities/jobs/explore", timeout=15)
        time.sleep(1)
        resp = session.post(GLINTS_API_URL, headers=GLINTS_HEADERS, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"❌ Gagal fetch API Glints: {e}")
        return hasil

    jobs = data.get("data", {}).get("searchJobsV3", {}).get("jobsInPage", [])

    for item in jobs:
        job_id = item.get("id")
        judul = item.get("title")
        if not judul or not job_id:
            continue

        perusahaan = item.get("company", {}).get("name") or "Perusahaan dirahasiakan"
        lokasi = item.get("city", {}).get("name") or "Indonesia"

        hasil.append({
            "judul": judul,
            "perusahaan": perusahaan,
            "lokasi": lokasi,
            "tipe_kerja": item.get("type", "").lower(),
            "kategori": None,
            "deskripsi": "",
            "gaji": _format_gaji_glints(item.get("salaries", [])),
            "sumber_platform": "glints",
            "sumber_url": f"https://glints.com/id/opportunities/jobs/{job_id}",
            "tanggal_post": _parse_iso_date(item.get("updatedAt") or item.get("createdAt")),
        })

    return hasil


# ==========================================
# LAPIS HTML/CSS SELECTOR — untuk situs tanpa API publik
# ==========================================
# Dipakai untuk LinkedIn, Kalibrr, dan situs sejenis yang lebih mudah/stabil
# di-scrape lewat HTML + CSS selector daripada lewat API tersembunyi.
#
# CATATAN LINKEDIN:
# LinkedIn punya endpoint publik (tanpa login) yang dipakai halaman "guest" job search,
# mengembalikan potongan HTML berisi daftar lowongan. Selector di bawah adalah pola umum
# yang dipakai halaman tersebut, tapi bisa berubah sewaktu-waktu tanpa pemberitahuan,
# dan LinkedIn membatasi rate/kadang memblokir IP yang scraping terlalu sering — jadi
# pakai time.sleep() antar request dan jangan set limit terlalu besar. ToS LinkedIn juga
# secara eksplisit melarang scraping otomatis, jadi ini murni "bisa secara teknis",
# risiko pemblokiran akun/IP tetap ada.
#
# CATATAN KALIBRR:
# Selector di bawah masih PLACEHOLDER karena saya tidak punya akses jaringan ke
# kalibrr.com dari sandbox ini. Isi sendiri lewat debug_page_structure() di bagian
# paling bawah file ini (Inspect Element di browser Anda), lalu update dictionary ini.

SITE_CONFIGS = {
    "linkedin": {
        "search_url": (
            "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
            "?keywords={keyword}&location={location}&start={start}"
        ),
        "impersonate": "chrome120",
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
# SCRAPER: KALIBRR.ID (HTML, pola URL job — bukan CSS class)
# ==========================================
KALIBRR_BASE = "https://www.kalibrr.id"
KALIBRR_JOB_LINK_RE = re.compile(r'^(/c/[^/]+/jobs/(\d+)/[^?]+)$')


def scrape_kalibrr(path: str = "/home/all-jobs", limit: int = 10) -> list[dict]:
    print("Mencari lowongan di Kalibrr...")
    hasil = []
    url = f"{KALIBRR_BASE}{path}"

    try:
        resp = requests.get(url, impersonate="chrome120", timeout=15)
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

        # Nama perusahaan: anchor berikutnya setelah link judul biasanya link nama perusahaan
        perusahaan = "Tidak diketahui"
        next_a = a.find_next("a", href=True)
        if next_a:
            teks = next_a.get_text(strip=True)
            if teks:
                perusahaan = teks

        # Ambil teks blok sekitar untuk cari lokasi/gaji/tipe secara heuristik.
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

        gaji_match = re.search(r'(IDR[\d.,]+\s*-\s*IDR[\d.,]+\s*/\s*month)', card_text)
        gaji = gaji_match.group(1) if gaji_match else None

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
        print("⚠️ 0 lowongan Kalibrr ditemukan. Kemungkinan halaman berubah struktur, "
              "atau request diblokir. Jalankan debug_page_structure() untuk cek isi HTML mentahnya.")

    return hasil


# ==========================================
# SCRAPER: DEALLS.COM (HTML, pola URL job)
# ==========================================
DEALLS_BASE = "https://dealls.com"
DEALLS_JOB_LINK_RE = re.compile(r'^(/loker/([a-z0-9-]+)~([a-z0-9-]+))$')


def scrape_dealls(path: str = "/loker/populer/loker-software-engineer-jakarta", limit: int = 10) -> list[dict]:
    print("Mencari lowongan di Dealls...")
    hasil = []
    url = f"{DEALLS_BASE}{path}"

    try:
        resp = requests.get(url, impersonate="chrome120", timeout=15)
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

        gaji_match = re.search(r'(Rp\s?[\d.,]+\s*-\s*Rp\s?[\d.,]+)', full_text)
        gaji = gaji_match.group(1) if gaji_match else (None if "Negotiable" not in full_text else None)

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
        print("⚠️ 0 lowongan Dealls ditemukan. Cek apakah path pencarian masih valid "
              "atau jalankan debug_page_structure() untuk verifikasi ulang.")

    return hasil


# ==========================================
# SCRAPER: LINKEDIN (guest API HTML)
# ==========================================
def scrape_linkedin(keyword: str = "software engineer", location: str = "Indonesia",
                     limit: int = 10) -> list[dict]:
    """
    Scrape LinkedIn lewat endpoint publik "guest" (tanpa login) yang dipakai
    halaman pencarian lowongan versi non-login. Mengembalikan potongan HTML
    yang di-parse dengan CSS selector.
    """
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
        print("⚠️ 0 job card LinkedIn ditemukan — kemungkinan struktur HTML sudah berubah "
              "atau request diblokir. Jalankan debug_page_structure(url) untuk cek ulang.")
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
# GENERIC HTML / PLAYWRIGHT SCRAPER
# ==========================================
def generic_html_scraper(site_key: str, limit: int = 10) -> list[dict]:
    cfg = SITE_CONFIGS.get(site_key)
    if not cfg:
        raise ValueError(f"Config untuk '{site_key}' belum ada di SITE_CONFIGS")

    print(f"Mencari lowongan di {site_key} (HTML scraping)...")
    hasil = []

    try:
        resp = requests.get(cfg["search_url"], impersonate=cfg.get("impersonate", "chrome120"), timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"❌ Gagal fetch halaman {site_key}: {e}")
        return hasil

    soup = BeautifulSoup(resp.text, "html.parser")
    cards = soup.select(cfg["selector_job_card"])[:limit]

    if not cards:
        print(f"⚠️ 0 job card ditemukan untuk '{site_key}'. Selector mungkin salah/berubah, "
              f"atau situs ini render pakai JavaScript (coba generic_playwright_scraper()).")
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


def generic_playwright_scraper(site_key: str, limit: int = 10, wait_selector: str | None = None) -> list[dict]:
    from playwright.sync_api import sync_playwright

    cfg = SITE_CONFIGS.get(site_key)
    if not cfg:
        raise ValueError(f"Config untuk '{site_key}' belum ada di SITE_CONFIGS")

    print(f"Mencari lowongan di {site_key} (Playwright/browser render)...")
    hasil = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(cfg["search_url"], timeout=30000)
            page.wait_for_selector(wait_selector or cfg["selector_job_card"], timeout=15000)
            html = page.content()
        except Exception as e:
            print(f"❌ Gagal render halaman {site_key}: {e}")
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
        resp = requests.get(url, impersonate="chrome120", timeout=15)
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

    semua_lowongan.extend(scrape_karir_com(limit=limit_per_sumber))
    time.sleep(1)

    semua_lowongan.extend(scrape_glints(page_size=limit_per_sumber))
    time.sleep(1)

    semua_lowongan.extend(scrape_linkedin(keyword=keyword_linkedin, limit=limit_per_sumber))
    time.sleep(1)

    semua_lowongan.extend(scrape_kalibrr(limit=limit_per_sumber))

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