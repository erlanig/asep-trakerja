"""
Modul untuk memproses data mentah hasil scraping menggunakan OpenAI:
- Membersihkan & merapikan teks
- Mengkategorikan (kategori pekerjaan)
- Memilih top-N lowongan paling relevan/berkualitas per hari
"""

import os
import json
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")


def filter_dan_rangkum(lowongan_mentah: list[dict], jumlah: int = 10) -> list[dict]:
    """
    Kirim daftar lowongan mentah ke OpenAI, minta hasil:
    - dibersihkan teksnya (judul & deskripsi rapi)
    - dikategorikan (mis. IT, Marketing, Finance, Admin, dst)
    - dipilih {jumlah} lowongan terbaik/paling jelas & lengkap informasinya

    Return list of dict siap disimpan ke DB (field sama dengan skema tabel `lowongan`).
    """
    if not lowongan_mentah:
        return []

    # Kirim data mentah sebagai JSON ke model, minta output JSON juga
    input_data = json.dumps(lowongan_mentah, ensure_ascii=False)

    system_prompt = f"""
Kamu adalah asisten yang membantu merapikan data lowongan kerja hasil scraping.

Tugas kamu:
1. Bersihkan judul & deskripsi dari karakter aneh/typo/whitespace berlebih.
2. Tentukan kategori pekerjaan yang sesuai (contoh: "IT & Software", "Marketing",
   "Finance & Accounting", "Customer Service", "Human Resources", "Sales",
   "Operations", "Design", "Lainnya").
3. Buang entri yang datanya terlalu tidak lengkap (judul kosong / perusahaan tidak jelas).
4. Buang entri yang terlihat duplikat satu sama lain (judul & perusahaan sama).
5. Pilih maksimal {jumlah} lowongan TERBAIK (paling lengkap & jelas informasinya)
   dari data yang diberikan.

PENTING: Balas HANYA dengan JSON array yang valid, tanpa teks pembuka/penutup,
tanpa markdown code fence. Setiap elemen array harus punya field persis berikut:
judul, perusahaan, lokasi, tipe_kerja, kategori, deskripsi, gaji, sumber_platform,
sumber_url, tanggal_post.

Field tipe_kerja harus salah satu dari: "full-time", "part-time", "remote", "kontrak", "magang".
Field gaji boleh null kalau tidak ada informasinya.
Field deskripsi maksimal 2-3 kalimat ringkas.
"""

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": input_data},
        ],
        temperature=0.3,
    )

    hasil_teks = response.choices[0].message.content.strip()

    # Bersihkan andai model tetap membungkus dengan markdown fence
    if hasil_teks.startswith("```"):
        hasil_teks = hasil_teks.strip("`")
        if hasil_teks.startswith("json"):
            hasil_teks = hasil_teks[4:].strip()

    try:
        hasil = json.loads(hasil_teks)
        if isinstance(hasil, dict):
            # jaga-jaga kalau model bungkus dalam {"data": [...]}
            hasil = hasil.get("data") or hasil.get("lowongan") or []
        return hasil[:jumlah]
    except json.JSONDecodeError as e:
        print(f"❌ Gagal parse JSON dari OpenAI: {e}")
        print(f"Raw response: {hasil_teks[:500]}")
        return []