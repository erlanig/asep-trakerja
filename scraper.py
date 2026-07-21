import time
from datetime import datetime, date
import uuid

# 1. GANTI IMPORT REQUESTS
# Hapus: import requests
# Gunakan curl_cffi:
from curl_cffi import requests

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

# ==========================================
# SCRAPER: KARIR.COM
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
        # 2. TAMBAHKAN IMPERSONATE DI SINI
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
        # 3. TAMBAHKAN IMPERSONATE DI SINI
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
# SCRAPER: GLINTS.COM
# ==========================================
GLINTS_API_URL = "https://glints.com/api/v2-alc/graphql"

# Generate device_id palsu agar Glints mengira ini sesi user baru
dummy_device_id = str(uuid.uuid4())

GLINTS_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "id,en-US;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
    "Origin": "https://glints.com",
    "Referer": "https://glints.com/id/opportunities/jobs/explore",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "x-glints-country-code": "ID",
    # Header keamanan browser (sangat penting untuk bypass WAF)
    "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    # Kirimkan device_id palsu
    "Cookie": f"device_id={dummy_device_id};"
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

    try:
        # Gunakan impersonate versi yang lebih baru (chrome120) agar sinkron dengan Header sec-ch-ua di atas
        resp = requests.post(GLINTS_API_URL, headers=GLINTS_HEADERS, json=payload, timeout=15, impersonate="chrome120")
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
# AGGREGATOR UTAMA
# ==========================================
def scrape_semua_sumber(limit_per_sumber: int = 5) -> list[dict]:
    semua_lowongan = []
    
    data_karir = scrape_karir_com(limit=limit_per_sumber)
    semua_lowongan.extend(data_karir)
    
    time.sleep(1)

    data_glints = scrape_glints(page_size=limit_per_sumber)
    semua_lowongan.extend(data_glints)

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