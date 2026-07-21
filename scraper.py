import time
import requests
from datetime import datetime, date

BASE_API = "https://gateway2-beta.karir.com"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
    "Origin": "https://karir.com",
    "Referer": "https://karir.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
}

# [DIPERBARUI] Pola URL asli web karir.com
BASE_JOB_URL = "https://karir.com/opportunities/{id}"


def _format_tanggal(iso_string: str) -> str:
    if not iso_string:
        return date.today().isoformat()
    try:
        return datetime.fromisoformat(iso_string.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return date.today().isoformat()


def _format_gaji(item: dict) -> str | None:
    lower = item.get("salary_lower")
    upper = item.get("salary_upper")
    if lower and upper:
        return f"Rp{lower:,.0f} - Rp{upper:,.0f}".replace(",", ".")
    return None


def get_job_detail(opportunity_id: int) -> str:
    """
    [BARU] Mengambil deskripsi lengkap menggunakan endpoint detail yang ditemukan.
    """
    url = f"{BASE_API}/v1/opportunity/detail"
    body = {
        "opportunity_id": opportunity_id,
        "language": "id"
    }
    
    try:
        resp = requests.post(url, headers=HEADERS, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        
        # Mengambil isi deskripsi. 
        # (Jika struktur JSON berbeda, ubah key "job_description" sesuai hasil inspect browser)
        deskripsi = data.get("job_description") or data.get("description") or ""
        return deskripsi
        
    except Exception as e:
        print(f"⚠️ Gagal fetch detail untuk ID {opportunity_id}: {e}")
        return ""


def scrape_karir_com(job_function_ids: list[int] | None = None,
                     limit: int = 20, offset: int = 0) -> list[dict]:
    """
    Ambil daftar lowongan dari API POST karir.com.
    """
    hasil = []
    url = f"{BASE_API}/v2/search/opportunities"
    body = {
        "job_function_ids": job_function_ids or [],
        "limit": limit,
        "offset": offset,
        "sort_order": "newest",
        "is_opportunity": True,
    }

    try:
        resp = requests.post(url, headers=HEADERS, json=body, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as e:
        print(f"❌ Gagal fetch API karir.com: {e}")
        return hasil
    except ValueError as e:
        print(f"❌ Response bukan JSON valid: {e}")
        return hasil

    opportunities = payload.get("data", {}).get("opportunities", [])

    for item in opportunities:
        job_id = item.get("id")
        judul = item.get("job_position")
        perusahaan = item.get("company_name")

        if not judul or not job_id:
            continue

        # (Opsional) Mengambil detail deskripsi pekerjaan
        # Jika proses scraping terasa terlalu lama karena butuh request 1-1, 
        # Anda bisa menonaktifkan baris ini atau memanggilnya secara asynchronous.
        deskripsi_lengkap = get_job_detail(job_id)
        time.sleep(0.5) # Jeda agar tidak terkena rate limit dari server

        hasil.append({
            "judul": judul,
            "perusahaan": perusahaan or "Perusahaan dirahasiakan",
            "lokasi": item.get("description") or "", 
            "tipe_kerja": "full-time",
            "kategori": None,
            "deskripsi": deskripsi_lengkap, 
            "gaji": _format_gaji(item),
            "sumber_platform": "karir.com",
            "sumber_url": BASE_JOB_URL.format(id=job_id), # [DIPERBARUI]
            "tanggal_post": _format_tanggal(item.get("posted_at")),
        })

    return hasil


def scrape_semua_sumber() -> list[dict]:
    semua = []
    semua.extend(scrape_karir_com(limit=10)) # Limit diperkecil untuk testing
    return semua


if __name__ == "__main__":
    data = scrape_karir_com(limit=5)
    print(f"\nDitemukan {len(data)} lowongan:\n")
    for d in data:
        print(f"  - {d['judul']} @ {d['perusahaan']} ({d['lokasi']})")
        print(f"    Gaji: {d['gaji']}")
        print(f"    Link: {d['sumber_url']}")
        print("-" * 50)