import time
import requests
from datetime import datetime, date

# ==========================================
# FUNGSI BANTUAN UMUM
# ==========================================
def _parse_iso_date(iso_string: str) -> str:
    """Mengubah format waktu ISO menjadi string tanggal YYYY-MM-DD"""
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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
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
        resp = requests.post(url, headers=KARIR_HEADERS, json=body, timeout=10)
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
        resp = requests.post(url, headers=KARIR_HEADERS, json=body, timeout=15)
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
        # Opsional: Ambil detail deskripsi. Beri jeda agar tidak di-banned
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

GLINTS_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "id",
    "Content-Type": "application/json",
    "Origin": "https://glints.com",
    "Referer": "https://glints.com/id/opportunities/jobs/explore?country=ID&locationName=All%20Cities%2FProvinces",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "x-glints-country-code": "ID",
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    # INI BAGIAN PALING PENTING (Salinan dari -b atau Cookie di curl Anda)
    "Cookie": 'device_id=c51f7472-77c9-4ab9-9054-db56dceb1afb; glints_tracking_id=043e7f36-ff63-4dee-a6aa-98617331d53a; _gcl_au=1.1.908327477.1784602947; sessionFirstTouchPath=/id/opportunities/jobs/explore; sessionLastTouchPath=/id/opportunities/jobs/explore; currentJobID=064d973c-5732-4643-b903-ef65ae3c6d8b; sessionIsLastTouch=false;'
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
        resp = requests.post(GLINTS_API_URL, headers=GLINTS_HEADERS, json=payload, timeout=15)
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
            "deskripsi": "", # Glints list endpoint tidak memuat deskripsi lengkap
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
    """
    Panggil semua fetcher sumber yang aktif, gabungkan hasilnya.
    """
    semua_lowongan = []

    # 1. Ambil dari Karir.com
    data_karir = scrape_karir_com(limit=limit_per_sumber)
    semua_lowongan.extend(data_karir)
    
    # Jeda antar request platform
    time.sleep(1)

    # 2. Ambil dari Glints
    # Di glints kita pakai `page_size` yang fungsinya mirip dengan `limit`
    data_glints = scrape_glints(page_size=limit_per_sumber)
    semua_lowongan.extend(data_glints)

    return semua_lowongan


if __name__ == "__main__":
    # Jalankan aggregator (Ambil 5 dari karir, 5 dari glints)
    data_gabungan = scrape_semua_sumber(limit_per_sumber=5)
    
    print(f"\n✅ Total Ditemukan: {len(data_gabungan)} lowongan dari berbagai sumber:\n")
    for d in data_gabungan:
        print(f"[{d['sumber_platform'].upper()}] {d['judul']} @ {d['perusahaan']} ({d['lokasi']})")
        print(f"  Tipe Kerja : {d['tipe_kerja'].replace('_', ' ').title()}")
        print(f"  Gaji       : {d['gaji']}")
        print(f"  Tanggal    : {d['tanggal_post']}")
        print(f"  Link       : {d['sumber_url']}")
        print("-" * 60)