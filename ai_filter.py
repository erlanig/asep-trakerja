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


def bersihkan_dan_rangkum(
    lowongan_mentah: list[dict],
    jumlah: int = 10
) -> list[dict]:
    """
    Membersihkan data lowongan hasil scraping menggunakan OpenAI.
    """

    if not lowongan_mentah:
        return []

    input_data = json.dumps(
        lowongan_mentah,
        ensure_ascii=False
    )

    system_prompt = f"""
Bersihkan data lowongan kerja hasil scraping.

Tugas:
- Rapikan judul, perusahaan, lokasi, dan deskripsi.
- Tentukan kategori pekerjaan.
- Hapus hanya data tidak valid:
  - judul kosong
  - perusahaan kosong
  - duplikat sama persis
  - bukan lowongan kerja

Jangan hapus karena:
- gaji kosong
- lokasi kosong
- deskripsi kosong

Jika data kosong gunakan null atau "Tidak disebutkan".

Ambil maksimal {jumlah} lowongan terbaik.
Pertahankan sebanyak mungkin data valid.

Output JSON object:
{{"lowongan":[...]}}

Setiap item wajib memiliki:
judul, perusahaan, lokasi, tipe_kerja,
kategori, deskripsi, gaji,
sumber_platform, sumber_url, tanggal_post

Kategori:
IT & Software, Marketing,
Finance & Accounting, Customer Service,
Human Resources, Sales, Operations,
Design, Lainnya

tipe_kerja:
full-time, part-time, remote, kontrak, magang
"""

    response = None

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

        # Format JSON object {"lowongan":[]}
        if isinstance(hasil, dict):
            hasil = (
                hasil.get("lowongan")
                or hasil.get("data")
                or hasil.get("jobs")
                or []
            )

        if not isinstance(hasil, list):
            return []

        return hasil[:jumlah]

    except json.JSONDecodeError as e:
        print(f"❌ JSON OpenAI tidak valid: {e}")

        if response:
            print(
                response.choices[0]
                .message.content[:500]
            )

        return []

    except Exception as e:
        print(f"❌ Error OpenAI: {e}")
        return []


# Alias agar script lama tetap berjalan
# main.py yang memanggil filter_dan_rangkum tidak perlu diubah
filter_dan_rangkum = bersihkan_dan_rangkum