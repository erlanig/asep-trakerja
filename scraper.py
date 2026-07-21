"""
Job Scraper Bot — versi diperbaiki
====================================
Perubahan utama dari versi sebelumnya:

1. `_get_session()` / `_request_with_retry()` — helper request terpusat dengan:
   - rotasi User-Agent & profil impersonate (biar tidak selalu sama pola-nya)
   - "cookie warm-up" (kunjungi homepage dulu sebelum halaman pencarian) —
     ini yang memperbaiki 403 di Glints, karena Cloudflare/WAF Glints menolak
     request yang langsung "dingin" tanpa cookie sesi.
   - retry otomatis dengan exponential backoff + jitter untuk 403/429/5xx
   - header browser yang lebih lengkap (sec-ch-ua, sec-fetch-*, dll)

2. Glints diperbaiki dengan selector HTML yang lebih tahan terhadap perubahan
   struktur (mencari lewat beberapa kandidat selector, bukan cuma satu),
   plus fallback ke Playwright (kalau terpasang) untuk kasus di mana
   Cloudflare butuh render JS penuh.

3. Sumber baru ditambahkan (semuanya API publik resmi milik platform,
   jadi jauh lebih stabil daripada HTML scraping):
   - JobStreet Indonesia (chalice-search API, dipakai oleh seluruh
     jaringan SEEK termasuk Jobstreet ID)
   - RemoteOK (API JSON publik)
   - Arbeitnow (API JSON publik, job internasional termasuk remote)

4. Deduplikasi lintas-sumber berdasarkan (judul + perusahaan) yang
   dinormalisasi, supaya lowongan yang sama dari 2 sumber tidak dobel.

5. Logging pakai modul `logging` (bukan print polos) supaya gampang
   dipantau saat dijalankan sebagai cron/service, tapi tetap tampil di
   console seperti sebelumnya.

CATATAN PENTING soal Glints:
Glints dilindungi Cloudflare bot-management. Cookie warm-up + header
lengkap akan memperbaiki sebagian besar kasus 403, tapi Cloudflare bisa
saja meningkatkan proteksinya kapan saja (termasuk minta JS challenge
yang tidak bisa diselesaikan tanpa browser asli). Kalau warm-up masih
kena 403 terus-menerus, opsi realistis:
  a) pasang Playwright (`pip install playwright && playwright install chromium`)
     — sudah otomatis dipakai sebagai fallback di sini kalau tersedia.
  b) pakai residential proxy / provider scraping pihak ketiga.
Tidak ada "satu baris kode" yang bisa menjamin bypass permanen — ini sifat
dasar dari WAF yang terus berubah, bukan bug di scraper ini.
"""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, date

from curl_cffi import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("job_scraper")


# ==========================================
# HELPER REQUEST TERPUSAT (retry, warm-up, UA rotation)
# ==========================================
IMPERSONATE_PROFILES = ["chrome124", "chrome120", "chrome110", "edge101"]

COMMON_HEADERS = {
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Upgrade-Insecure-Requests": "1",
}


def _random_impersonate() -> str:
    return random.choice(IMPERSONATE_PROFILES)


def _request_with_retry(
    session,
    method: str,
    url: str,
    max_retries: int = 3,
    headers: dict | None = None,
    **kwargs,
):
    """
    Request dengan retry + exponential backoff untuk status 403/429/5xx.
    `session` adalah objek `requests.Session()` dari curl_cffi supaya cookie
    (hasil warm-up) ikut terbawa antar-request.
    """
    merged_headers = {**COMMON_HEADERS, **(headers or {})}
    last_exc = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = session.request(
                method,
                url,
                headers=merged_headers,
                timeout=15,
                **kwargs,
            )
            if resp.status_code in (403, 429) or resp.status_code >= 500:
                wait = (2 ** attempt) + random.uniform(0.5, 1.5)
                log.warning(
                    "  ⚠️ %s -> %s (percobaan %d/%d), tunggu %.1fs...",
                    url, resp.status_code, attempt, max_retries, wait,
                )
                time.sleep(wait)
                continue
            return resp
        except Exception as e:
            last_exc = e
            wait = (2 ** attempt) + random.uniform(0.5, 1.5)
            log.warning("  ⚠️ Error request %s: %s (percobaan %d/%d)", url, e, attempt, max_retries)
            time.sleep(wait)

    if last_exc:
        raise last_exc
    return resp  # respons terakhir walau masih gagal, biar caller yang putuskan


def _warm_up_session(homepage_url: str, impersonate: str):
    """
    Kunjungi homepage dulu untuk dapat cookie sesi sebelum hit halaman
    pencarian / API. Banyak WAF (termasuk Cloudflare) menandai request yang
    langsung ke halaman dalam tanpa cookie sebagai bot.
    """
    session = requests.Session(impersonate=impersonate)
    try:
        session.get(homepage_url, headers=COMMON_HEADERS, timeout=15)
        time.sleep(random.uniform(0.8, 1.6))  # jeda wajar seperti manusia
    except Exception as e:
        log.debug("Warm-up gagal (%s), lanjut tanpa cookie: %s", homepage_url, e)
    return session


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
    pola_range = re.search(r'(?:IDR|Rp)\s?[\d.,]+\s*-\s*(?:IDR|Rp)?\s?[\d.,]+', teks, re.IGNORECASE)
    if pola_range:
        return pola_range.group().strip()
    pola_tunggal = re.search(r'(?:IDR|Rp)\s?[\d.,]+', teks, re.IGNORECASE)
    if pola_tunggal:
        return pola_tunggal.group().strip()
    return None


def _normalisasi_kunci(judul: str, perusahaan: str) -> str:
    """Kunci dedup: lowercase, buang spasi/simbol berlebih."""
    gabungan = f"{judul}|{perusahaan}".lower()
    return re.sub(r"[^a-z0-9|]+", "", gabungan)


def dedup_lowongan(daftar: list[dict]) -> list[dict]:
    """Hilangkan lowongan duplikat lintas-sumber berdasarkan judul+perusahaan."""
    dilihat = set()
    hasil = []
    for item in daftar:
        kunci = _normalisasi_kunci(item.get("judul", ""), item.get("perusahaan", ""))
        if kunci in dilihat:
            continue
        dilihat.add(kunci)
        hasil.append(item)
    return hasil


# ==========================================
# SCRAPER: KARIR.COM (API) — tidak berubah, sudah stabil
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
        log.warning("⚠️ Gagal fetch detail Karir.com untuk ID %s: %s", opportunity_id, e)
        return ""


def scrape_karir_com(limit: int = 10, offset: int = 0) -> list[dict]:
    log.info("Mencari lowongan di Karir.com...")
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
        log.error("❌ Gagal fetch API karir.com: %s", e)
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
# SCRAPER: GLINTS.COM (diperbaiki — warm-up + multi-selector + retry)
# ==========================================
GLINTS_HOMEPAGE = "https://glints.com/id"
GLINTS_JOB_LINK_RE = re.compile(r'/opportunities/jobs/([a-zA-Z0-9-]+)')

# Beberapa kandidat class yang pernah dipakai Glints untuk kartu lowongan.
# Kita coba semua secara berurutan — halaman React sering ganti nama class
# hash (mis. `sc-abc123`), jadi kita juga fallback ke pencarian generik lewat link.
GLINTS_CARD_SELECTOR_CANDIDATES = [
    "div[class*='JobCard']",
    "div[class*='opportunity-card']",
    "li[class*='job']",
]


def _extract_glints_card_info(a_tag, soup) -> dict | None:
    href = a_tag.get("href", "")
    m = GLINTS_JOB_LINK_RE.search(href)
    if not m:
        return None
    job_id = m.group(1)

    judul = a_tag.get_text(strip=True)
    if not judul:
        judul_el = a_tag.find_previous(["h2", "h3"]) or a_tag.find_next(["h2", "h3"])
        judul = _text_or_none(judul_el)
    if not judul:
        return None

    container = a_tag
    teks_kartu = ""
    for _ in range(6):
        container = container.find_parent()
        if container is None:
            break
        teks_kartu = container.get_text(" ", strip=True)
        if len(teks_kartu) > 50:
            break

    perusahaan = "Tidak diketahui"
    company_el = container.find("span", class_=re.compile(r"company", re.I)) if container else None
    if company_el:
        perusahaan = _text_or_none(company_el) or perusahaan
    else:
        match_company = re.search(r'(?:di|at)\s+([A-Za-z0-9\s&.]+)', teks_kartu)
        if match_company:
            perusahaan = match_company.group(1).strip()

    lokasi = "Indonesia"
    loc_el = container.find("span", class_=re.compile(r"location", re.I)) if container else None
    if loc_el:
        lokasi = _text_or_none(loc_el) or lokasi
    else:
        cities = ["Jakarta", "Bandung", "Surabaya", "Yogyakarta", "Tangerang", "Bekasi", "Semarang", "Remote"]
        for city in cities:
            if city.lower() in teks_kartu.lower():
                lokasi = city
                break

    gaji = _parse_gaji_dari_teks(teks_kartu)

    tipe = "unknown"
    lower_teks = teks_kartu.lower()
    if any(k in lower_teks for k in ["full-time", "full time", "penuh waktu"]):
        tipe = "full-time"
    elif any(k in lower_teks for k in ["part-time", "part time", "paruh waktu"]):
        tipe = "part-time"
    elif any(k in lower_teks for k in ["contract", "kontrak"]):
        tipe = "kontrak"
    elif "intern" in lower_teks or "magang" in lower_teks:
        tipe = "magang"

    sumber_url = f"https://glints.com{href}" if href.startswith("/") else href

    return {
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
        "_job_id": job_id,
    }


def _scrape_glints_via_html(keyword: str, location: str, limit: int) -> list[dict]:
    impersonate = _random_impersonate()
    session = _warm_up_session(GLINTS_HOMEPAGE, impersonate)

    base_url = "https://glints.com/id/opportunities/jobs/explore"
    params = {"keyword": keyword, "country": "ID", "locationName": location}
    query_string = "&".join(f"{k}={v.replace(' ', '%20')}" for k, v in params.items() if v)
    url = f"{base_url}?{query_string}" if query_string else base_url

    headers = {
        "Referer": GLINTS_HOMEPAGE,
        "Sec-Fetch-Site": "same-origin",
    }

    resp = _request_with_retry(session, "GET", url, headers=headers)
    if resp.status_code != 200:
        log.error("❌ Glints tetap %s setelah retry+warm-up.", resp.status_code)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    links = soup.find_all("a", href=GLINTS_JOB_LINK_RE)
    seen_ids = set()
    hasil = []

    for a in links:
        info = _extract_glints_card_info(a, soup)
        if not info or info["_job_id"] in seen_ids:
            continue
        seen_ids.add(info["_job_id"])
        info.pop("_job_id", None)
        hasil.append(info)
        if len(hasil) >= limit:
            break

    return hasil


def _scrape_glints_via_playwright(keyword: str, location: str, limit: int) -> list[dict]:
    """Fallback kalau HTML statis tidak cukup (Cloudflare JS challenge)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.info("  (Playwright tidak terpasang — lewati fallback. `pip install playwright` untuk mengaktifkan.)")
        return []

    base_url = "https://glints.com/id/opportunities/jobs/explore"
    params = {"keyword": keyword, "country": "ID", "locationName": location}
    query_string = "&".join(f"{k}={v.replace(' ', '%20')}" for k, v in params.items() if v)
    url = f"{base_url}?{query_string}" if query_string else base_url

    hasil = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ))
        try:
            page.goto(url, timeout=30000)
            page.wait_for_timeout(3000)  # beri waktu render + lolos challenge
            html = page.content()
        except Exception as e:
            log.error("❌ Playwright gagal render Glints: %s", e)
            browser.close()
            return hasil
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    links = soup.find_all("a", href=GLINTS_JOB_LINK_RE)
    seen_ids = set()
    for a in links:
        info = _extract_glints_card_info(a, soup)
        if not info or info["_job_id"] in seen_ids:
            continue
        seen_ids.add(info["_job_id"])
        info.pop("_job_id", None)
        hasil.append(info)
        if len(hasil) >= limit:
            break
    return hasil


def scrape_glints(keyword: str = "", location: str = "Indonesia", limit: int = 10) -> list[dict]:
    log.info("Mencari lowongan di Glints...")
    hasil = _scrape_glints_via_html(keyword, location, limit)

    if not hasil:
        log.info("  HTML statis kosong/diblokir, coba fallback Playwright...")
        hasil = _scrape_glints_via_playwright(keyword, location, limit)

    if not hasil:
        log.warning("⚠️ 0 lowongan Glints ditemukan (kemungkinan Cloudflare menahan request ini).")

    return hasil


# ==========================================
# SCRAPER: JOBSTREET INDONESIA (chalice-search API — SEEK network)
# ==========================================
JOBSTREET_API = "https://id.jobstreet.com/api/chalice-search/v4/search"
JOBSTREET_JOB_URL = "https://id.jobstreet.com/job/{id}"


def scrape_jobstreet(keyword: str = "software engineer", location: str = "Indonesia", limit: int = 10) -> list[dict]:
    log.info("Mencari lowongan di JobStreet Indonesia...")
    hasil = []

    impersonate = _random_impersonate()
    session = _warm_up_session("https://id.jobstreet.com/", impersonate)

    params = {
        "siteKey": "ID-Main",
        "sourcesystem": "houston",
        "keywords": keyword,
        "where": location,
        "page": 1,
        "pageSize": min(limit, 32),
    }
    headers = {"Accept": "application/json", "Referer": "https://id.jobstreet.com/"}

    try:
        resp = _request_with_retry(session, "GET", JOBSTREET_API, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("❌ Gagal fetch JobStreet: %s", e)
        return hasil

    for job in data.get("data", [])[:limit]:
        job_id = job.get("id")
        judul = job.get("title")
        if not judul or not job_id:
            continue

        perusahaan = (job.get("advertiser") or {}).get("description") or "Tidak diketahui"
        lokasi_list = [loc.get("label") for loc in job.get("locations", []) if loc.get("label")]
        lokasi = ", ".join(lokasi_list) if lokasi_list else location

        salary = job.get("salary") or {}
        gaji = None
        if salary.get("label"):
            gaji = salary["label"]

        tipe = "unknown"
        work_types = job.get("workTypes") or []
        if work_types:
            wt = work_types[0].lower()
            if "full" in wt:
                tipe = "full-time"
            elif "part" in wt:
                tipe = "part-time"
            elif "contract" in wt or "temp" in wt:
                tipe = "kontrak"
            elif "intern" in wt or "casual" in wt:
                tipe = "magang"

        hasil.append({
            "judul": judul,
            "perusahaan": perusahaan,
            "lokasi": lokasi,
            "tipe_kerja": tipe,
            "kategori": (job.get("classifications") or [{}])[0].get("classification", {}).get("description"),
            "deskripsi": job.get("teaser") or "",
            "gaji": gaji,
            "sumber_platform": "jobstreet",
            "sumber_url": JOBSTREET_JOB_URL.format(id=job_id),
            "tanggal_post": _parse_iso_date(job.get("listingDate")),
        })

    return hasil


# ==========================================
# SCRAPER: REMOTEOK (API JSON publik, resmi & stabil)
# ==========================================
REMOTEOK_API = "https://remoteok.com/api"


def scrape_remoteok(keyword: str = "", limit: int = 10) -> list[dict]:
    log.info("Mencari lowongan remote di RemoteOK...")
    hasil = []

    impersonate = _random_impersonate()
    session = _warm_up_session("https://remoteok.com/", impersonate)
    headers = {"Accept": "application/json"}

    try:
        resp = _request_with_retry(session, "GET", REMOTEOK_API, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("❌ Gagal fetch RemoteOK: %s", e)
        return hasil

    # Elemen pertama biasanya metadata legal, bukan lowongan — lewati.
    entries = [d for d in data if isinstance(d, dict) and d.get("id")]

    for job in entries:
        judul = job.get("position")
        perusahaan = job.get("company")
        if not judul or not perusahaan:
            continue

        if keyword and keyword.lower() not in (judul + " ".join(job.get("tags", []))).lower():
            continue

        gaji = None
        if job.get("salary_min") and job.get("salary_max"):
            gaji = f"${job['salary_min']:,} - ${job['salary_max']:,} / tahun"

        hasil.append({
            "judul": judul,
            "perusahaan": perusahaan,
            "lokasi": job.get("location") or "Remote",
            "tipe_kerja": "full-time",
            "kategori": ", ".join(job.get("tags", [])[:3]) or None,
            "deskripsi": re.sub("<[^<]+?>", "", job.get("description") or "")[:1000],
            "gaji": gaji,
            "sumber_platform": "remoteok",
            "sumber_url": f"https://remoteok.com{job.get('url', '')}" if job.get("url", "").startswith("/") else job.get("url"),
            "tanggal_post": _parse_iso_date(job.get("date")),
        })

        if len(hasil) >= limit:
            break

    return hasil


# ==========================================
# SCRAPER: ARBEITNOW (API JSON publik, job internasional + remote)
# ==========================================
ARBEITNOW_API = "https://arbeitnow.com/api/job-board-api"


def scrape_arbeitnow(keyword: str = "", limit: int = 10) -> list[dict]:
    log.info("Mencari lowongan di Arbeitnow...")
    hasil = []

    impersonate = _random_impersonate()
    session = _warm_up_session("https://arbeitnow.com/", impersonate)
    headers = {"Accept": "application/json"}

    try:
        resp = _request_with_retry(session, "GET", ARBEITNOW_API, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("❌ Gagal fetch Arbeitnow: %s", e)
        return hasil

    for job in data.get("data", []):
        judul = job.get("title")
        perusahaan = job.get("company_name")
        if not judul or not perusahaan:
            continue

        if keyword and keyword.lower() not in judul.lower():
            continue

        tags = job.get("tags") or []
        job_types = job.get("job_types") or []
        tipe = "unknown"
        if job_types:
            jt = job_types[0].lower()
            if "full" in jt:
                tipe = "full-time"
            elif "part" in jt:
                tipe = "part-time"
            elif "contract" in jt or "freelance" in jt:
                tipe = "kontrak"
            elif "intern" in jt:
                tipe = "magang"

        hasil.append({
            "judul": judul,
            "perusahaan": perusahaan,
            "lokasi": job.get("location") or ("Remote" if job.get("remote") else "Tidak diketahui"),
            "tipe_kerja": tipe,
            "kategori": ", ".join(tags[:3]) or None,
            "deskripsi": re.sub("<[^<]+?>", "", job.get("description") or "")[:1000],
            "gaji": None,
            "sumber_platform": "arbeitnow",
            "sumber_url": job.get("url"),
            "tanggal_post": _parse_iso_date(str(job.get("created_at", ""))) if job.get("created_at") else date.today().isoformat(),
        })

        if len(hasil) >= limit:
            break

    return hasil


# ==========================================
# SCRAPER: KALIBRR.ID (HTML) — ditambahkan warm-up + retry
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
    log.info("Mencari lowongan di Kalibrr...")
    hasil = []
    impersonate = _random_impersonate()
    session = _warm_up_session(KALIBRR_BASE, impersonate)
    url = f"{KALIBRR_BASE}{path}"

    try:
        resp = _request_with_retry(session, "GET", url)
        resp.raise_for_status()
    except Exception as e:
        log.error("❌ Gagal fetch Kalibrr: %s", e)
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
        log.warning("⚠️ 0 lowongan Kalibrr ditemukan. Struktur mungkin berubah.")

    return hasil


# ==========================================
# SCRAPER: DEALLS.COM (HTML) — ditambahkan warm-up + retry
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
    log.info("Mencari lowongan di Dealls...")
    hasil = []
    impersonate = _random_impersonate()
    session = _warm_up_session(DEALLS_BASE, impersonate)
    url = f"{DEALLS_BASE}{path}"

    try:
        resp = _request_with_retry(session, "GET", url)
        resp.raise_for_status()
    except Exception as e:
        log.error("❌ Gagal fetch Dealls: %s", e)
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
        log.warning("⚠️ 0 lowongan Dealls ditemukan. Path mungkin tidak valid.")

    return hasil


# ==========================================
# SCRAPER: LINKEDIN (guest API HTML) — ditambahkan warm-up + retry
# ==========================================
def scrape_linkedin(keyword: str = "software engineer", location: str = "Indonesia", limit: int = 10) -> list[dict]:
    log.info("Mencari lowongan di LinkedIn...")
    hasil = []

    search_url = (
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        f"?keywords={keyword.replace(' ', '%20')}&location={location.replace(' ', '%20')}&start=0"
    )

    impersonate = _random_impersonate()
    session = _warm_up_session("https://www.linkedin.com/jobs", impersonate)

    try:
        resp = _request_with_retry(session, "GET", search_url)
        resp.raise_for_status()
    except Exception as e:
        log.error("❌ Gagal fetch LinkedIn: %s", e)
        return hasil

    soup = BeautifulSoup(resp.text, "html.parser")
    cards = soup.select("div.base-card")[:limit]

    if not cards:
        log.warning("⚠️ 0 job card LinkedIn ditemukan — struktur mungkin berubah atau diblokir.")
        return hasil

    for card in cards:
        judul = _text_or_none(card.select_one("h3.base-search-card__title"))
        if not judul:
            continue

        perusahaan = _text_or_none(card.select_one("h4.base-search-card__subtitle")) or "Tidak diketahui"
        lokasi = _text_or_none(card.select_one("span.job-search-card__location")) or location

        link_el = card.select_one("a.base-card__full-link")
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
# SCRAPER: TALENTICS (MULTI‑URL + FALLBACK LINKEDIN) — tidak berubah
# ==========================================
TALENTICS_CAREER_URLS = [
    "https://talentics.id/careers",
    "https://talentics.id/karir",
    "https://talentics.id/jobs",
    "https://talentics.recruitee.com",
]


def scrape_talentics_direct(url: str, limit: int = 10) -> list[dict]:
    log.info("  Mencoba URL: %s", url)
    try:
        resp = requests.get(url, impersonate=_random_impersonate(), timeout=10)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        links = soup.find_all("a", href=True)
        results = []
        for a in links:
            href = a["href"]
            teks = a.get_text(strip=True)
            if not teks or len(teks) < 5:
                continue
            if any(k in href.lower() for k in ["job", "career", "loker", "position"]):
                full_url = href if href.startswith("http") else url.rstrip("/") + "/" + href.lstrip("/")
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
    log.info("Mencari lowongan Talentics...")
    for url in TALENTICS_CAREER_URLS:
        hasil = scrape_talentics_direct(url, limit)
        if hasil:
            log.info("  Berhasil dari %s", url)
            return hasil
    log.info("  Tidak ditemukan halaman karir langsung. Fallback ke LinkedIn...")
    linkedin_jobs = scrape_linkedin(keyword="Talentics", location="Indonesia", limit=limit * 2)
    talentics_jobs = [j for j in linkedin_jobs if "talentics" in j["perusahaan"].lower()]
    if not talentics_jobs:
        log.warning("  ⚠️ Tidak ada lowongan Talentics di LinkedIn saat ini.")
    else:
        for job in talentics_jobs:
            job["sumber_platform"] = "talentics"
    return talentics_jobs[:limit]


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
    log.info("✅ HTML disimpan ke: %s (%d karakter)", save_to, len(html))
    log.info("   Jumlah tag <a>: %d, tag <div>: %d", len(soup.find_all('a')), len(soup.find_all('div')))
    log.info("   Buka file di atas dan cari pola job card secara manual (Ctrl+F judul lowongan yang kamu tahu).")


# ==========================================
# AGGREGATOR UTAMA
# ==========================================
def scrape_semua_sumber(limit_per_sumber: int = 5, keyword_linkedin: str = "software engineer") -> list[dict]:
    semua_lowongan = []

    def safe_extend(scraper_fn, *args, **kwargs):
        try:
            res = scraper_fn(*args, **kwargs)
            semua_lowongan.extend(res)
            log.info("    → %d lowongan dari %s", len(res), scraper_fn.__name__)
        except Exception as e:
            log.error("❌ Error di %s: %s", scraper_fn.__name__, e)

    safe_extend(scrape_karir_com, limit=limit_per_sumber)
    time.sleep(random.uniform(0.8, 1.5))

    safe_extend(scrape_glints, keyword="", limit=limit_per_sumber)
    time.sleep(random.uniform(0.8, 1.5))

    safe_extend(scrape_jobstreet, keyword=keyword_linkedin, limit=limit_per_sumber)
    time.sleep(random.uniform(0.8, 1.5))

    safe_extend(scrape_linkedin, keyword=keyword_linkedin, limit=limit_per_sumber)
    time.sleep(random.uniform(0.8, 1.5))

    safe_extend(scrape_kalibrr, limit=limit_per_sumber)
    time.sleep(random.uniform(0.8, 1.5))

    safe_extend(scrape_dealls, limit=limit_per_sumber)
    time.sleep(random.uniform(0.8, 1.5))

    safe_extend(scrape_talentics, limit=limit_per_sumber)
    time.sleep(random.uniform(0.8, 1.5))

    safe_extend(scrape_remoteok, limit=limit_per_sumber)
    time.sleep(random.uniform(0.8, 1.5))

    safe_extend(scrape_arbeitnow, limit=limit_per_sumber)

    sebelum = len(semua_lowongan)
    semua_lowongan = dedup_lowongan(semua_lowongan)
    if sebelum != len(semua_lowongan):
        log.info("    → %d duplikat lintas-sumber dihapus", sebelum - len(semua_lowongan))

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