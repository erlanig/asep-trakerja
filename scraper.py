"""
Fetcher lowongan kerja dari karir.com, menggunakan API internal mereka
(ditemukan & dikonfirmasi lewat DevTools > Network > XHR).

Endpoint: POST https://gateway2-beta.karir.com/v2/search/opportunities
Tidak butuh API key/auth - cukup header Origin & Referer yang meniru browser asli.

CATATAN PENTING:
- Ini API internal (tidak resmi/tidak didokumentasikan publik), bisa berubah
  sewaktu-waktu tanpa pemberitahuan dari karir.com. Selalu monitor.
- `sumber_url` di bawah PERLU DIVERIFIKASI - kirim contoh URL lowongan asli
  (hasil klik di browser) untuk konfirmasi pola-nya sudah benar.
- job_function_ids mengacu ke daftar kategori dari endpoint
  /v1/master_job_functions (contoh: 14 = "Layanan Pelanggan").
  Kosongkan list ini (None/[]) untuk coba ambil dari SEMUA kategori sekaligus -
  kalau ternyata API mewajibkan minimal 1 kategori, perlu looping per kategori.
- Tetap pakai jeda antar request, jangan spam endpoint ini.
"""

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
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
}

# TODO: verifikasi pola URL ini dengan buka 1 lowongan asli di browser, kirim contohnya.
BASE_JOB_URL = "https://www.karir.com/lowongan-kerja/{id}"


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


def scrape_karir_com(job_function_ids: list[int] | None = None,
                      limit: int = 20, offset: int = 0) -> list[dict]:
    """
    Ambil daftar lowongan dari API POST karir.com.

    job_function_ids: list ID kategori (lihat master_job_functions.py untuk daftar
                       lengkap). None/[] = coba ambil tanpa filter kategori dulu.
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

        hasil.append({
            "judul": judul,
            "perusahaan": perusahaan or "Perusahaan dirahasiakan",
            "lokasi": item.get("description") or "",  # field "description" API ini isinya lokasi
            "tipe_kerja": "full-time",
            "kategori": None,
            "deskripsi": "",
            "gaji": _format_gaji(item),
            "sumber_platform": "karir.com",
            "sumber_url": BASE_JOB_URL.format(id=job_id),
            "tanggal_post": _format_tanggal(item.get("posted_at")),
        })

    return hasil


def scrape_semua_sumber() -> list[dict]:
    """
    Panggil semua fetcher sumber yang aktif, gabungkan hasilnya.
    """
    semua = []

    semua.extend(scrape_karir_com(limit=20))
    time.sleep(1)

    return semua


if __name__ == "__main__":
    # Test cepat manual: python scraper.py
    data = scrape_karir_com(limit=10)
    print(f"Ditemukan {len(data)} lowongan:")
    for d in data:
        print(f"  - {d['judul']} @ {d['perusahaan']} ({d['lokasi']}) | gaji: {d['gaji']} -> {d['sumber_url']}")