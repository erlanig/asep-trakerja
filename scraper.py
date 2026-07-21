"""
Fetcher lowongan kerja dari karir.com, menggunakan API internal mereka
(ditemukan lewat DevTools > Network > XHR saat browsing search-lowongan).

API ini JAUH lebih stabil daripada scraping HTML/Playwright:
- Tidak perlu render browser (lebih cepat & ringan)
- Tidak bergantung pada class CSS yang bisa berubah sewaktu-waktu
- Data sudah terstruktur rapi (JSON)

Endpoint yang dipakai:
- GET https://gateway2-beta.karir.com/v2/search/opportunities  -> daftar lowongan
- GET https://gateway2-beta.karir.com/v1/master_job_functions  -> daftar kategori pekerjaan

CATATAN PENTING:
- Ini API internal (bukan API publik resmi/didokumentasikan), jadi TIDAK ADA jaminan
  stabil selamanya - karir.com bisa mengubah struktur/parameter kapan saja tanpa
  pemberitahuan. Selalu monitor apakah masih berjalan normal.
- `sumber_url` di bawah ini pola-nya PERLU DIVERIFIKASI. Field JSON API tidak
  menyertakan URL detail lowongan langsung, cuma `id`. Buka satu lowongan di
  browser, cek URL aslinya, lalu sesuaikan `BASE_JOB_URL` di bawah kalau beda.
- Tetap gunakan rate limiting/jeda antar request, jangan spam endpoint ini.
"""

import time
import requests
from datetime import datetime, date

BASE_API = "https://gateway2-beta.karir.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

# TODO: verifikasi pola URL ini dengan buka 1 lowongan asli di browser.
# Contoh tebakan awal - GANTI kalau ternyata beda:
BASE_JOB_URL = "https://www.karir.com/lowongan-kerja/{id}"


def _format_tanggal(iso_string: str) -> str:
    """Konversi '2026-07-20T06:40:00Z' -> '2026-07-20'"""
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
    return None  # "LABEL_COMPETITIVE_SALARY" dll -> biar OpenAI yang tangani nanti


def scrape_karir_com(kata_kunci: str = "", halaman: int = 1, per_halaman: int = 20) -> list[dict]:
    """
    Ambil daftar lowongan dari API pencarian karir.com.
    """
    hasil = []
    url = f"{BASE_API}/v2/search/opportunities"
    params = {
        "q": kata_kunci,
        "page": halaman,
        "per_page": per_halaman,
    }

    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
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
            "lokasi": item.get("description") or "",  # field "description" di API ini isinya lokasi
            "tipe_kerja": "full-time",  # API tidak selalu kasih tipe kerja eksplisit di list view
            "kategori": None,  # akan diisi otomatis oleh OpenAI nanti
            "deskripsi": "",   # list endpoint tidak kasih deskripsi lengkap, cuma judul+lokasi
            "gaji": _format_gaji(item),
            "sumber_platform": "karir.com",
            "sumber_url": BASE_JOB_URL.format(id=job_id),
            "tanggal_post": _format_tanggal(item.get("posted_at")),
        })

    return hasil


def scrape_semua_sumber() -> list[dict]:
    """
    Panggil semua fetcher sumber yang aktif, gabungkan hasilnya.
    Tambahkan fetcher lain di sini kalau nanti ketemu API tersembunyi
    dari Glints/Kalibrr juga (JAUH lebih baik daripada Playwright).
    """
    semua = []

    semua.extend(scrape_karir_com())
    time.sleep(1)  # jeda antar request, tetap sopan ke server mereka

    return semua


if __name__ == "__main__":
    # Test cepat manual: python scraper.py
    data = scrape_karir_com()
    print(f"Ditemukan {len(data)} lowongan:")
    for d in data[:5]:
        print(f"  - {d['judul']} @ {d['perusahaan']} ({d['lokasi']}) -> {d['sumber_url']}")