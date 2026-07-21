"""
Modul untuk memproses data mentah hasil scraping menggunakan OpenAI:
- Membersihkan & merapikan teks
- Mengkategorikan lowongan
- Memilih lowongan terbaik tanpa membuang terlalu banyak data
"""

import os
import json
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")


def bersihkan_dan_rangkum(lowongan_mentah: list[dict], jumlah: int = 10) -> list[dict]:
    """
    Membersihkan data lowongan hasil scraping menggunakan OpenAI.

    Output:
    - Judul dan deskripsi lebih rapi
    - Tambahan kategori pekerjaan
    - Duplikat dibuang
    - Data valid dipertahankan sebanyak mungkin
    """

    if not lowongan_mentah:
        return []

    input_data = json.dumps(lowongan_mentah, ensure_ascii=False)

    system_prompt = f"""
Bersihkan data lowongan kerja hasil scraping.

Tugas:
1. Rapikan judul, perusahaan, lokasi, dan deskripsi.
2. Tentukan kategori pekerjaan.
3. Hapus hanya:
   - judul kosong
   - perusahaan kosong/tidak jelas
   - duplikat sama persis
   - bukan lowongan kerja

Jangan hapus data hanya karena:
- gaji kosong
- lokasi kosong
- deskripsi kosong

Gunakan null atau "Tidak disebutkan" jika data tidak tersedia.
Pilih maksimal {jumlah} lowongan terbaik, tetapi pertahankan sebanyak mungkin data valid.

Output JSON array saja tanpa markdown.

Field wajib:
judul, perusahaan, lokasi, tipe_kerja, kategori,
deskripsi, gaji, sumber_platform,
sumber_url, tanggal_post

Kategori contoh:
IT & Software, Marketing, Finance & Accounting,
Customer Service, Human Resources, Sales,
Operations, Design, Lainnya

tipe_kerja hanya:
full-time, part-time, remote, kontrak, magang.
"""

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": input_data
                }
            ],
            temperature=0.2,
            response_format={
                "type": "json_object"
            }
        )

        hasil_teks = response.choices[0].message.content.strip()

        hasil = json.loads(hasil_teks)

        # Handle jika AI membungkus response
        if isinstance(hasil, dict):
            hasil = (
                hasil.get("data")
                or hasil.get("lowongan")
                or hasil.get("jobs")
                or []
            )

        if not isinstance(hasil, list):
            return []

        return hasil[:jumlah]

    except json.JSONDecodeError as e:
        print(f"❌ JSON OpenAI tidak valid: {e}")
        print(response.choices[0].message.content[:500])
        return []

    except Exception as e:
        print(f"❌ Error OpenAI: {e}")
        return []