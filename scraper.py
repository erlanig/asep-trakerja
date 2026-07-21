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

CATATAN PENTING soal Glints & JobStreet (kalau masih 403 terus-menerus):
Kalau warm-up + retry + header lengkap MASIH kena 403 terus-menerus di
kedua situs ini, penyebabnya kemungkinan besar BUKAN lagi soal cookie
atau header — kemungkinan besar IP server tempat kode ini dijalankan
(VPS, cloud compute, sandbox) sudah masuk daftar blokir Cloudflare/WAF
di level jaringan (banyak platform besar blok seluruh range IP
datacenter/cloud, terlepas dari header apa pun yang dikirim). Ini bukan
sesuatu yang bisa diperbaiki lewat kode saja. Opsi realistis:
  a) jalankan dari IP residensial (laptop/rumah) untuk tes — kalau di
     laptop berhasil tapi di server tidak, itu konfirmasi block di level IP.
  b) pasang Playwright (`pip install playwright && playwright install chromium`)
     — otomatis dipakai sebagai fallback untuk Glints kalau tersedia, tapi
     kalau block-nya di level IP, Playwright pun tidak akan menembus.
  c) pakai residential/mobile proxy, atau layanan scraping pihak ketiga
     yang rotasi IP otomatis.
Karena dua sumber ini rawan diblokir di level infrastruktur, versi ini
menambah banyak sumber API publik lain (lihat di bawah) yang jauh lebih
stabil dijalankan dari server/cloud.

6. Sumber tambahan (update kedua) — API/RSS publik yang tidak dilindungi
   Cloudflare-tier WAF seperti Glints/JobStreet:
   - Remotive, Jobicy, Himalayas — API JSON remote-job publik
   - We Work Remotely — RSS feed publik
   - Greenhouse & Lever — API job-board publik yang dipakai banyak startup
     untuk menampilkan lowongan mereka sendiri di careers page (generic
     scraper — tinggal isi daftar company slug/board token untuk "tembus"
     langsung ke web company masing-masing, sesuai permintaan awal)

7. LinkedIn sekarang paginasi (bukan cuma ~10 kartu di 1 halaman) dan
   punya limit independen dari `limit_per_sumber`, karena sebelumnya
   hasilnya selalu mentok di angka kecil walau limit dinaikkan.
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
# KLASIFIKASI MAGANG & FILTER RENTANG TANGGAL
# ==========================================
# Kata kunci ini dicek di judul + kategori + deskripsi (bukan cuma judul),
# karena banyak platform (mis. Karir.com, LinkedIn, RemoteOK) tidak punya
# field tipe_kerja yang eksplisit "intern"/"magang" — labelnya kadang cuma
# muncul di badan deskripsi ("posisi magang", "internship program", dst).
_KATA_KUNCI_MAGANG = [
    "magang", "internship", "intern ", " intern", "pkl", "praktik kerja",
    "kerja praktik", "kerja praktek", "trainee", "management trainee",
    "apprentice", "apprenticeship", "co-op", "student program",
]

HARI_RENTANG_DEFAULT = 30  # ambil lowongan yang di-post dalam N hari terakhir


def _teks_mengandung_magang(*potongan_teks: str | None) -> bool:
    gabungan = " ".join(t for t in potongan_teks if t).lower()
    return any(k in gabungan for k in _KATA_KUNCI_MAGANG)


def klasifikasi_ulang_tipe_kerja(item: dict) -> dict:
    """
    Jaring pengaman: kalau tipe_kerja platform asli tidak menyebut magang
    padahal teksnya jelas soal magang/internship/PKL/trainee, timpa jadi
    'magang'. Ini dijalankan untuk SEMUA sumber (bukan cuma yang sudah
    punya deteksi magang bawaan), supaya klasifikasi konsisten lintas
    platform sebelum data dipisah jadi 2 pesan (reguler vs magang).
    """
    if item.get("tipe_kerja") == "magang":
        return item
    if _teks_mengandung_magang(item.get("judul"), item.get("kategori"), item.get("deskripsi")):
        item["tipe_kerja"] = "magang"
    return item


def _dalam_rentang_hari(tanggal_iso: str | None, hari: int = HARI_RENTANG_DEFAULT) -> bool:
    """
    True kalau tanggal_post ada di rentang [hari ini - hari, hari ini].
    Ini SENGAJA tidak mensyaratkan lowongan baru "hari ini" — banyak
    lowongan bagus tetap terbuka lebih dari sehari, jadi kita ambil semua
    yang di-post dalam N hari terakhir (default 30 hari) daripada cuma
    yang tanggal post-nya persis hari ini.

    Catatan soal tanggal tutup/deadline: sebagian besar sumber publik yang
    dipakai scraper ini (karir.com, Glints, JobStreet, Kalibrr, Dealls,
    LinkedIn guest API, RemoteOK, Arbeitnow, Remotive, Jobicy, Himalayas,
    WWR, Greenhouse, Lever) TIDAK mengekspos tanggal tutup lowongan di
    endpoint publik yang dipakai di sini — jadi filter "jangan yang tutup
    hari ini / minimal H+1" tidak bisa dijamin dari sisi scraping. Sebagai
    proxy yang realistis, kita pakai rentang tanggal POSTING (30 hari
    terakhir) supaya tidak mengambil lowongan basi yang kemungkinan besar
    sudah/hampir tutup. Kalau di masa depan sebuah sumber ternyata
    menyediakan field deadline, tinggal tambahkan pengecekan
    `deadline >= besok` di sini.
    """
    if not tanggal_iso:
        return True  # tidak ada info tanggal -> jangan buang, biarkan lolos
    try:
        tgl = datetime.fromisoformat(tanggal_iso).date()
    except ValueError:
        return True
    selisih = (date.today() - tgl).days
    return -1 <= selisih <= hari  # -1 supaya tanggal "besok" (zona waktu beda) tetap lolos


def pasca_proses(
    daftar: list[dict],
    hari_rentang: int = HARI_RENTANG_DEFAULT,
) -> list[dict]:
    """
    Tahap akhir sebelum data dikirim ke AI filter:
    1. Klasifikasi ulang tipe_kerja (deteksi magang yang terlewat).
    2. Buang lowongan yang tanggal post-nya di luar rentang N hari terakhir.
    3. Dedup ulang (klasifikasi ulang bisa saja menyatukan variasi judul).
    """
    diklasifikasi = [klasifikasi_ulang_tipe_kerja(item) for item in daftar]
    dalam_rentang = [
        item for item in diklasifikasi
        if _dalam_rentang_hari(item.get("tanggal_post"), hari_rentang)
    ]
    dibuang = len(diklasifikasi) - len(dalam_rentang)
    if dibuang:
        log.info("    → %d lowongan dibuang (di luar rentang %d hari terakhir)", dibuang, hari_rentang)
    return dedup_lowongan(dalam_rentang)


def pisahkan_magang(daftar: list[dict]) -> tuple[list[dict], list[dict]]:
    """Pisahkan satu daftar lowongan menjadi (reguler, magang)."""
    reguler = [item for item in daftar if item.get("tipe_kerja") != "magang"]
    magang = [item for item in daftar if item.get("tipe_kerja") == "magang"]
    return reguler, magang


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
    # "All Cities/Provinces" adalah value locationName default yang dipakai
    # UI Glints sendiri ketika tidak memilih kota spesifik (dikonfirmasi dari
    # URL asli situsnya) — beda dari "Indonesia" yang dipakai versi lama.
    params = {
        "keyword": keyword,
        "country": "ID",
        "locationName": location if location and location != "Indonesia" else "All Cities/Provinces",
    }
    query_string = "&".join(f"{k}={v.replace(' ', '%20').replace('/', '%2F')}" for k, v in params.items() if v)
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
    params = {
        "keyword": keyword,
        "country": "ID",
        "locationName": location if location and location != "Indonesia" else "All Cities/Provinces",
    }
    query_string = "&".join(f"{k}={v.replace(' ', '%20').replace('/', '%2F')}" for k, v in params.items() if v)
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
# SCRAPER: REMOTIVE (API JSON publik, remote-job aggregator)
# ==========================================
REMOTIVE_API = "https://remotive.com/api/remote-jobs"


def scrape_remotive(keyword: str = "", limit: int = 10) -> list[dict]:
    log.info("Mencari lowongan di Remotive...")
    hasil = []
    session = _warm_up_session("https://remotive.com/", _random_impersonate())
    params = {"search": keyword} if keyword else {}

    try:
        resp = _request_with_retry(session, "GET", REMOTIVE_API, params=params, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("❌ Gagal fetch Remotive: %s", e)
        return hasil

    for job in data.get("jobs", [])[:limit]:
        hasil.append({
            "judul": job.get("title"),
            "perusahaan": job.get("company_name") or "Tidak diketahui",
            "lokasi": job.get("candidate_required_location") or "Remote",
            "tipe_kerja": (job.get("job_type") or "full_time").replace("_", "-"),
            "kategori": job.get("category"),
            "deskripsi": re.sub("<[^<]+?>", "", job.get("description") or "")[:1000],
            "gaji": job.get("salary") or None,
            "sumber_platform": "remotive",
            "sumber_url": job.get("url"),
            "tanggal_post": _parse_iso_date(job.get("publication_date")),
        })

    return hasil


# ==========================================
# SCRAPER: JOBICY (API JSON publik, remote-job aggregator)
# ==========================================
JOBICY_API = "https://jobicy.com/api/v2/remote-jobs"


def scrape_jobicy(keyword: str = "", limit: int = 10) -> list[dict]:
    """
    NB: skema JSON di sini mengikuti dokumentasi publik Jobicy — kalau nama
    field berubah di masa depan, cukup sesuaikan `.get(...)` di bawah (kode
    memakai `.get()` di semua tempat supaya tidak crash kalau ada field hilang).
    """
    log.info("Mencari lowongan di Jobicy...")
    hasil = []
    session = _warm_up_session("https://jobicy.com/", _random_impersonate())
    params = {"count": min(limit * 2, 50)}
    if keyword:
        params["tag"] = keyword

    try:
        resp = _request_with_retry(session, "GET", JOBICY_API, params=params, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("❌ Gagal fetch Jobicy: %s", e)
        return hasil

    for job in data.get("jobs", [])[:limit]:
        gaji = None
        if job.get("annualSalaryMin") or job.get("annualSalaryMax"):
            mata_uang = job.get("salaryCurrency", "")
            gaji = f"{mata_uang} {job.get('annualSalaryMin', '?')} - {job.get('annualSalaryMax', '?')} / tahun".strip()

        hasil.append({
            "judul": job.get("jobTitle"),
            "perusahaan": job.get("companyName") or "Tidak diketahui",
            "lokasi": job.get("jobGeo") or "Remote",
            "tipe_kerja": (job.get("jobType") or ["unknown"])[0] if isinstance(job.get("jobType"), list) else (job.get("jobType") or "unknown"),
            "kategori": job.get("jobIndustry", [None])[0] if isinstance(job.get("jobIndustry"), list) else job.get("jobIndustry"),
            "deskripsi": re.sub("<[^<]+?>", "", job.get("jobExcerpt") or "")[:1000],
            "gaji": gaji,
            "sumber_platform": "jobicy",
            "sumber_url": job.get("url"),
            "tanggal_post": _parse_iso_date(job.get("pubDate")),
        })

    return hasil


# ==========================================
# SCRAPER: HIMALAYAS (API JSON publik, remote-job aggregator)
# ==========================================
HIMALAYAS_API = "https://himalayas.app/jobs/api"


def scrape_himalayas(keyword: str = "", limit: int = 10) -> list[dict]:
    """NB sama seperti Jobicy: skema field best-effort, pakai `.get()` defensif."""
    log.info("Mencari lowongan di Himalayas...")
    hasil = []
    session = _warm_up_session("https://himalayas.app/", _random_impersonate())
    params = {"limit": min(limit * 2, 50)}

    try:
        resp = _request_with_retry(session, "GET", HIMALAYAS_API, params=params, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("❌ Gagal fetch Himalayas: %s", e)
        return hasil

    for job in data.get("jobs", [])[:limit * 2]:
        judul = job.get("title")
        perusahaan = job.get("companyName") or (job.get("company") or {}).get("name")
        if not judul or not perusahaan:
            continue

        if keyword and keyword.lower() not in judul.lower():
            continue

        lokasi_list = job.get("locationRestrictions") or []
        lokasi = ", ".join(lokasi_list) if lokasi_list else "Remote"

        gaji = None
        if job.get("minSalary") or job.get("maxSalary"):
            gaji = f"${job.get('minSalary', '?')} - ${job.get('maxSalary', '?')}"

        slug = job.get("guid") or job.get("slug") or ""
        hasil.append({
            "judul": judul,
            "perusahaan": perusahaan,
            "lokasi": lokasi,
            "tipe_kerja": (job.get("employmentType") or "unknown").lower().replace("_", "-"),
            "kategori": ", ".join(job.get("categories", [])[:3]) or None,
            "deskripsi": re.sub("<[^<]+?>", "", job.get("excerpt") or "")[:1000],
            "gaji": gaji,
            "sumber_platform": "himalayas",
            "sumber_url": f"https://himalayas.app/companies/{slug}" if slug else "https://himalayas.app/jobs",
            "tanggal_post": _parse_iso_date(job.get("pubDate")),
        })

        if len(hasil) >= limit:
            break

    return hasil


# ==========================================
# SCRAPER: WE WORK REMOTELY (RSS feed publik)
# ==========================================
WWR_RSS_URL = "https://weworkremotely.com/categories/remote-programming-jobs.rss"


def scrape_weworkremotely(limit: int = 10) -> list[dict]:
    log.info("Mencari lowongan di We Work Remotely...")
    hasil = []
    session = _warm_up_session("https://weworkremotely.com/", _random_impersonate())

    try:
        resp = _request_with_retry(session, "GET", WWR_RSS_URL, headers={"Accept": "application/rss+xml, application/xml"})
        resp.raise_for_status()
    except Exception as e:
        log.error("❌ Gagal fetch We Work Remotely: %s", e)
        return hasil

    try:
        soup = BeautifulSoup(resp.text, "xml")
    except Exception:
        soup = BeautifulSoup(resp.text, "html.parser")

    items = soup.find_all("item")[:limit]
    for item in items:
        judul_lengkap = _text_or_none(item.find("title")) or ""
        # Format judul RSS-nya biasanya "Nama Perusahaan: Judul Posisi"
        if ":" in judul_lengkap:
            perusahaan, judul = judul_lengkap.split(":", 1)
        else:
            perusahaan, judul = "Tidak diketahui", judul_lengkap

        link = _text_or_none(item.find("link"))
        deskripsi_html = _text_or_none(item.find("description")) or ""
        pub_date = _text_or_none(item.find("pubDate"))

        try:
            tanggal = datetime.strptime(pub_date, "%a, %d %b %Y %H:%M:%S %z").date().isoformat() if pub_date else date.today().isoformat()
        except ValueError:
            tanggal = date.today().isoformat()

        hasil.append({
            "judul": judul.strip(),
            "perusahaan": perusahaan.strip(),
            "lokasi": "Remote",
            "tipe_kerja": "unknown",
            "kategori": "Programming",
            "deskripsi": re.sub("<[^<]+?>", "", deskripsi_html)[:1000],
            "gaji": None,
            "sumber_platform": "weworkremotely",
            "sumber_url": link,
            "tanggal_post": tanggal,
        })

    return hasil


# ==========================================
# SCRAPER GENERIK: GREENHOUSE & LEVER (ATS publik yang dipakai banyak
# startup untuk memajang lowongan di careers page mereka sendiri)
# ==========================================
# Ini yang paling dekat dengan permintaan "tembus web company masing-masing":
# Greenhouse & Lever adalah penyedia ATS yang API job-board-nya memang publik
# dan tanpa proteksi bot berat (dirancang untuk ditempel di website company).
# Isi daftar di bawah dengan board_token/company_slug perusahaan yang kamu
# incar. Cara cari token/slug-nya: buka careers page perusahaan tsb → lihat
# Network tab (DevTools) saat halaman lowongan dimuat → cari request ke
# `boards-api.greenhouse.io/v1/boards/<TOKEN>/jobs` atau
# `api.lever.co/v0/postings/<SLUG>`. Dua contoh di bawah untuk ilustrasi
# format saja — verifikasi ulang token/slug-nya sebelum dipakai serius,
# karena bisa berbeda dari nama brand perusahaan.
GREENHOUSE_BOARD_TOKENS: list[str] = [
    # "xendit",
]
LEVER_COMPANY_SLUGS: list[str] = [
    # "carro",
]


def scrape_greenhouse(board_token: str, limit: int = 20) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
    hasil = []
    session = _warm_up_session("https://boards.greenhouse.io/", _random_impersonate())

    try:
        resp = _request_with_retry(session, "GET", url, params={"content": "true"}, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("❌ Gagal fetch Greenhouse (%s): %s", board_token, e)
        return hasil

    for job in data.get("jobs", [])[:limit]:
        lokasi = (job.get("location") or {}).get("name") or "Tidak diketahui"
        deskripsi = re.sub("<[^<]+?>", "", job.get("content") or "")[:1000]

        hasil.append({
            "judul": job.get("title"),
            "perusahaan": board_token.title(),
            "lokasi": lokasi,
            "tipe_kerja": "unknown",
            "kategori": (job.get("departments") or [{}])[0].get("name"),
            "deskripsi": deskripsi,
            "gaji": None,
            "sumber_platform": f"greenhouse:{board_token}",
            "sumber_url": job.get("absolute_url"),
            "tanggal_post": _parse_iso_date(job.get("updated_at")),
        })

    return hasil


def scrape_lever(company_slug: str, limit: int = 20) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{company_slug}"
    hasil = []
    session = _warm_up_session(f"https://jobs.lever.co/{company_slug}", _random_impersonate())

    try:
        resp = _request_with_retry(session, "GET", url, params={"mode": "json"}, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("❌ Gagal fetch Lever (%s): %s", company_slug, e)
        return hasil

    for job in data[:limit]:
        categories = job.get("categories") or {}
        deskripsi = re.sub("<[^<]+?>", "", job.get("descriptionPlain") or job.get("description") or "")[:1000]

        hasil.append({
            "judul": job.get("text"),
            "perusahaan": company_slug.title(),
            "lokasi": categories.get("location") or "Tidak diketahui",
            "tipe_kerja": (categories.get("commitment") or "unknown").lower().replace(" ", "-"),
            "kategori": categories.get("team"),
            "deskripsi": deskripsi,
            "gaji": None,
            "sumber_platform": f"lever:{company_slug}",
            "sumber_url": job.get("hostedUrl"),
            "tanggal_post": _parse_iso_date(str(job.get("createdAt", ""))) if job.get("createdAt") else date.today().isoformat(),
        })

    return hasil


def scrape_semua_ats(limit_per_company: int = 20) -> list[dict]:
    """Jalankan scrape_greenhouse/scrape_lever untuk semua token/slug terdaftar di atas."""
    hasil = []
    for token in GREENHOUSE_BOARD_TOKENS:
        hasil.extend(scrape_greenhouse(token, limit=limit_per_company))
        time.sleep(random.uniform(0.5, 1.0))
    for slug in LEVER_COMPANY_SLUGS:
        hasil.extend(scrape_lever(slug, limit=limit_per_company))
        time.sleep(random.uniform(0.5, 1.0))
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


def scrape_kalibrr(path: str = "/home/all-jobs", limit: int = 20, max_pages: int = 4) -> list[dict]:
    """
    Satu halaman Kalibrr biasanya cuma memuat belasan lowongan, makanya versi
    lama sering mentok di bawah 20 walau `limit` dinaikkan. Sekarang loop
    lewat `?page=N` sampai `limit` terpenuhi atau halaman berikutnya kosong.
    """
    log.info("Mencari lowongan di Kalibrr...")
    hasil = []
    impersonate = _random_impersonate()
    session = _warm_up_session(KALIBRR_BASE, impersonate)
    seen_ids = set()

    for page in range(1, max_pages + 1):
        if len(hasil) >= limit:
            break

        sep = "&" if "?" in path else "?"
        url = f"{KALIBRR_BASE}{path}{sep}page={page}" if page > 1 else f"{KALIBRR_BASE}{path}"

        try:
            resp = _request_with_retry(session, "GET", url)
            resp.raise_for_status()
        except Exception as e:
            log.error("❌ Gagal fetch Kalibrr (halaman %d): %s", page, e)
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        ditemukan_di_halaman_ini = 0

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
            ditemukan_di_halaman_ini += 1

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

        if ditemukan_di_halaman_ini == 0:
            break  # halaman berikutnya kosong, tidak ada gunanya lanjut

        time.sleep(random.uniform(0.6, 1.2))

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


# Satu path Dealls cuma memuat lowongan untuk 1 kategori pencarian, jadi
# untuk dapat ≥20 hasil kita gabungkan beberapa path populer sekaligus
# (bukan cuma "software engineer Jakarta" seperti versi lama).
DEALLS_SEARCH_PATHS = [
    "/loker/populer/loker-software-engineer-jakarta",
    "/loker/populer/loker-marketing-jakarta",
    "/loker/populer/loker-data-analyst-jakarta",
    "/loker/populer/loker-finance-jakarta",
    "/loker/populer/loker-admin-jakarta",
]

# Path khusus untuk kategori magang — mengikuti pola URL yang sama dengan
# DEALLS_SEARCH_PATHS di atas. Verifikasi ulang slug ini kalau Dealls
# mengubah struktur URL kategorinya.
DEALLS_SEARCH_PATHS_MAGANG = [
    "/loker/populer/loker-magang-jakarta",
    "/loker/populer/loker-magang-bandung",
    "/loker/populer/loker-internship-jakarta",
]


def _scrape_dealls_satu_path(path: str, session, limit: int) -> list[dict]:
    hasil = []
    url = f"{DEALLS_BASE}{path}"

    try:
        resp = _request_with_retry(session, "GET", url)
        resp.raise_for_status()
    except Exception as e:
        log.error("❌ Gagal fetch Dealls (%s): %s", path, e)
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

    return hasil


def scrape_dealls(path: str | None = None, limit: int = 20, daftar_path: list[str] | None = None) -> list[dict]:
    """
    `path`: kalau diisi manual, hanya fetch path itu (perilaku versi lama).
    `daftar_path`: override daftar path yang dipakai kalau `path` kosong
    (dipakai untuk fokus ke kategori magang lewat DEALLS_SEARCH_PATHS_MAGANG).
    Kalau keduanya None (default), scraper jalan lewat beberapa kategori
    populer sekaligus (DEALLS_SEARCH_PATHS) supaya bisa tembus `limit` yang
    lebih tinggi tanpa mentok di 1-2 lowongan seperti sebelumnya.
    """
    log.info("Mencari lowongan di Dealls...")
    impersonate = _random_impersonate()
    session = _warm_up_session(DEALLS_BASE, impersonate)

    if path:
        hasil = _scrape_dealls_satu_path(path, session, limit)
    else:
        hasil = []
        seen_url = set()
        for p in (daftar_path or DEALLS_SEARCH_PATHS):
            if len(hasil) >= limit:
                break
            for job in _scrape_dealls_satu_path(p, session, limit):
                if job["sumber_url"] in seen_url:
                    continue
                seen_url.add(job["sumber_url"])
                hasil.append(job)
                if len(hasil) >= limit:
                    break
            time.sleep(random.uniform(0.6, 1.2))

    if not hasil:
        log.warning("⚠️ 0 lowongan Dealls ditemukan. Path mungkin tidak valid.")

    return hasil


# ==========================================
# SCRAPER: LINKEDIN (guest API HTML) — ditambahkan warm-up + retry
# ==========================================
def scrape_linkedin(keyword: str = "software engineer", location: str = "Indonesia", limit: int = 25) -> list[dict]:
    """
    Guest API LinkedIn hanya mengembalikan ~10 kartu per halaman (parameter
    `start`), makanya versi lama selalu mentok sekitar 5-10 walau `limit`
    dinaikkan — itu bukan soal selector, tapi karena cuma minta 1 halaman.
    Versi ini paginasi (`start=0,10,20,...`) sampai `limit` terpenuhi atau
    halaman kosong / diblokir.
    """
    log.info("Mencari lowongan di LinkedIn...")
    hasil = []

    impersonate = _random_impersonate()
    session = _warm_up_session("https://www.linkedin.com/jobs", impersonate)

    start = 0
    halaman_kosong_berturut = 0
    while len(hasil) < limit and halaman_kosong_berturut < 2:
        search_url = (
            "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
            f"?keywords={keyword.replace(' ', '%20')}&location={location.replace(' ', '%20')}&start={start}"
        )
        try:
            resp = _request_with_retry(session, "GET", search_url, max_retries=2)
            resp.raise_for_status()
        except Exception as e:
            log.error("❌ Gagal fetch LinkedIn (start=%d): %s", start, e)
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("div.base-card")

        if not cards:
            halaman_kosong_berturut += 1
            start += 10
            continue
        halaman_kosong_berturut = 0

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

            if len(hasil) >= limit:
                break

        start += 10
        time.sleep(random.uniform(0.8, 1.6))  # jeda antar-halaman biar sopan

    if not hasil:
        log.warning("⚠️ 0 job card LinkedIn ditemukan — struktur mungkin berubah atau diblokir.")

    return hasil[:limit]


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


def scrape_talentics(limit: int = 20) -> list[dict]:
    """
    NB: Talentics di sini adalah halaman karir SATU perusahaan (bukan job
    board multi-perusahaan seperti sumber lain), jadi hasilnya dibatasi oleh
    berapa banyak posisi yang sedang benar-benar dibuka perusahaan tsb —
    menaikkan `limit` tidak akan memaksa jadi 20 kalau lowongan bukanya
    memang cuma segelintir. Kalau maksudmu "Talentics" itu nama job board
    lain yang beda, kasih tahu URL-nya dan saya sesuaikan.
    """
    log.info("Mencari lowongan Talentics...")
    for url in TALENTICS_CAREER_URLS:
        hasil = scrape_talentics_direct(url, limit)
        if hasil:
            log.info("  Berhasil dari %s (%d lowongan tersedia di halaman karir ini)", url, len(hasil))
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
# Kata kunci dipakai untuk pass kedua khusus magang di sumber-sumber yang
# mendukung pencarian keyword (JobStreet, LinkedIn, RemoteOK, Arbeitnow,
# Remotive, Jobicy, Himalayas). Ini penting karena keyword utama biasanya
# "software engineer" dkk yang jarang menangkap lowongan magang — tanpa
# pass kedua ini, magang cuma kebagian sisa dari deteksi teks pasif.
KEYWORD_MAGANG_DEFAULT = "magang internship"


def scrape_semua_sumber(
    limit_per_sumber: int = 40,
    limit_magang_per_sumber: int = 20,
    keyword_linkedin: str = "software engineer",
    keyword_magang: str = KEYWORD_MAGANG_DEFAULT,
    limits: dict | None = None,
    sertakan_ats: bool = True,
    sertakan_pass_magang: bool = True,
    hari_rentang: int = HARI_RENTANG_DEFAULT,
) -> list[dict]:
    """
    Mengumpulkan lowongan dari semua sumber, lalu:
      1. Menjalankan pass tambahan khusus keyword magang/internship di
         sumber-sumber yang mendukung pencarian keyword, supaya total pool
         cukup besar untuk menghasilkan ~100 lowongan gabungan (reguler +
         magang) sekali jalan — bukan cuma ~20an seperti versi lama.
      2. Klasifikasi ulang tipe_kerja (`pasca_proses`) supaya magang yang
         "nyelip" di sumber tanpa keyword magang tetap terdeteksi dari teks.
      3. Filter rentang tanggal posting (default 30 hari terakhir) supaya
         tidak mengambil lowongan basi — lihat catatan di `_dalam_rentang_hari`
         soal keterbatasan info tanggal tutup/deadline di sumber publik.
      4. Dedup akhir lintas-sumber & lintas-pass.

    `limits`: override limit per sumber untuk pass reguler, mis.
    {"linkedin": 60, "remotive": 25}. Sumber yang tidak disebut memakai
    `limit_per_sumber`.
    """
    limits = limits or {}
    semua_lowongan = []

    def safe_extend(scraper_fn, label, *args, **kwargs):
        try:
            res = scraper_fn(*args, **kwargs)
            semua_lowongan.extend(res)
            log.info("    → %d lowongan dari %s", len(res), label)
        except Exception as e:
            log.error("❌ Error di %s: %s", label, e)

    # ---------- PASS 1: sumber lowongan Indonesia (reguler) ----------
    safe_extend(scrape_karir_com, "karir.com", limit=limits.get("karir.com", limit_per_sumber))
    time.sleep(random.uniform(0.8, 1.5))

    safe_extend(scrape_glints, "glints", keyword="", limit=limits.get("glints", limit_per_sumber))
    time.sleep(random.uniform(0.8, 1.5))

    safe_extend(scrape_jobstreet, "jobstreet", keyword=keyword_linkedin, limit=limits.get("jobstreet", limit_per_sumber))
    time.sleep(random.uniform(0.8, 1.5))

    safe_extend(scrape_linkedin, "linkedin", keyword=keyword_linkedin, limit=limits.get("linkedin", max(limit_per_sumber, 40)))
    time.sleep(random.uniform(0.8, 1.5))

    safe_extend(scrape_kalibrr, "kalibrr", limit=limits.get("kalibrr", limit_per_sumber))
    time.sleep(random.uniform(0.8, 1.5))

    safe_extend(scrape_dealls, "dealls", limit=limits.get("dealls", limit_per_sumber))
    time.sleep(random.uniform(0.8, 1.5))

    safe_extend(scrape_talentics, "talentics", limit=limits.get("talentics", limit_per_sumber))
    time.sleep(random.uniform(0.8, 1.5))

    # ---------- PASS 1: sumber remote / internasional (reguler) ----------
    safe_extend(scrape_remoteok, "remoteok", limit=limits.get("remoteok", limit_per_sumber))
    time.sleep(random.uniform(0.8, 1.5))

    safe_extend(scrape_arbeitnow, "arbeitnow", limit=limits.get("arbeitnow", limit_per_sumber))
    time.sleep(random.uniform(0.8, 1.5))

    safe_extend(scrape_remotive, "remotive", limit=limits.get("remotive", limit_per_sumber))
    time.sleep(random.uniform(0.8, 1.5))

    safe_extend(scrape_jobicy, "jobicy", limit=limits.get("jobicy", limit_per_sumber))
    time.sleep(random.uniform(0.8, 1.5))

    safe_extend(scrape_himalayas, "himalayas", limit=limits.get("himalayas", limit_per_sumber))
    time.sleep(random.uniform(0.8, 1.5))

    safe_extend(scrape_weworkremotely, "weworkremotely", limit=limits.get("weworkremotely", limit_per_sumber))
    time.sleep(random.uniform(0.8, 1.5))

    # ---------- langsung dari careers page company (Greenhouse/Lever) ----------
    if sertakan_ats and (GREENHOUSE_BOARD_TOKENS or LEVER_COMPANY_SLUGS):
        safe_extend(scrape_semua_ats, "greenhouse+lever", limit_per_company=limits.get("ats_per_company", 20))

    # ---------- PASS 2: keyword magang/internship khusus ----------
    # Sumber yang tidak mendukung pencarian keyword (Karir.com, Kalibrr
    # generik, Talentics) dilewati di sini — magang dari sana tetap
    # tertangkap lewat deteksi teks di `pasca_proses`.
    if sertakan_pass_magang:
        log.info("  -- Pass tambahan: keyword magang/internship --")

        safe_extend(scrape_jobstreet, "jobstreet(magang)", keyword=keyword_magang, limit=limit_magang_per_sumber)
        time.sleep(random.uniform(0.8, 1.5))

        safe_extend(scrape_linkedin, "linkedin(magang)", keyword="magang internship", limit=limit_magang_per_sumber)
        time.sleep(random.uniform(0.8, 1.5))

        safe_extend(scrape_dealls, "dealls(magang)", daftar_path=DEALLS_SEARCH_PATHS_MAGANG, limit=limit_magang_per_sumber)
        time.sleep(random.uniform(0.8, 1.5))

        safe_extend(scrape_remoteok, "remoteok(magang)", keyword="intern", limit=limit_magang_per_sumber)
        time.sleep(random.uniform(0.8, 1.5))

        safe_extend(scrape_arbeitnow, "arbeitnow(magang)", keyword="intern", limit=limit_magang_per_sumber)
        time.sleep(random.uniform(0.8, 1.5))

        safe_extend(scrape_remotive, "remotive(magang)", keyword="intern", limit=limit_magang_per_sumber)
        time.sleep(random.uniform(0.8, 1.5))

        safe_extend(scrape_jobicy, "jobicy(magang)", keyword="internship", limit=limit_magang_per_sumber)
        time.sleep(random.uniform(0.8, 1.5))

        safe_extend(scrape_himalayas, "himalayas(magang)", keyword="intern", limit=limit_magang_per_sumber)
        time.sleep(random.uniform(0.8, 1.5))

    log.info("  Total mentah sebelum pasca-proses: %d", len(semua_lowongan))
    hasil_akhir = pasca_proses(semua_lowongan, hari_rentang=hari_rentang)
    log.info("  Total setelah dedup + filter tanggal + klasifikasi ulang: %d", len(hasil_akhir))

    return hasil_akhir


if __name__ == "__main__":
    data_gabungan = scrape_semua_sumber(limit_per_sumber=40, limit_magang_per_sumber=20)
    reguler, magang = pisahkan_magang(data_gabungan)

    print(f"\n✅ Total Ditemukan: {len(data_gabungan)} lowongan ({len(reguler)} reguler, {len(magang)} magang)\n")
    for d in data_gabungan:
        print(f"[{d['sumber_platform'].upper()}] {d['judul']} @ {d['perusahaan']} ({d['lokasi']})")
        print(f"  Tipe Kerja : {d['tipe_kerja'].replace('_', ' ').title()}")
        print(f"  Gaji       : {d['gaji']}")
        print(f"  Tanggal    : {d['tanggal_post']}")
        print(f"  Link       : {d['sumber_url']}")
        print("-" * 60)