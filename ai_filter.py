"""
Modul untuk memproses data mentah hasil scraping menggunakan OpenAI:
- Membersihkan & merapikan teks
- Mengkategorikan lowongan
- Memilih lowongan terbaik tanpa membuang terlalu banyak data
"""

import os
import json
import math
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Ukuran per-batch ke OpenAI. Dulu 1 batch besar (~100 item) rawan bikin
# output JSON terpotong (max_tokens kehabisan) -> JSONDecodeError -> SEMUA
# item di batch itu hilang. Dengan chunk lebih kecil, risiko output
# terpotong jauh berkurang, dan kalau 1 chunk gagal, chunk lain tetap aman.
UKURAN_CHUNK = 20

# Estimasi token output per item (judul+deskripsi ringkas+field lain).
# Dipakai untuk menghitung max_tokens per chunk secara dinamis supaya
# tidak mepet/terpotong.
TOKEN_PER_ITEM_ESTIMASI = 220
TOKEN_BUFFER = 500

FIELD_WAJIB = [
    "judul", "perusahaan", "lokasi", "tipe_kerja",
    "kategori", "deskripsi", "gaji",
    "sumber_platform", "sumber_url", "tanggal_post",
]


def _pangkas_deskripsi(lowongan_mentah: list[dict]) -> list[dict]:
    """Pangkas deskripsi supaya payload & biaya token tetap wajar."""
    hasil = []
    for lo in lowongan_mentah:
        lo_ringkas = dict(lo)
        if lo_ringkas.get("deskripsi"):
            lo_ringkas["deskripsi"] = lo_ringkas["deskripsi"][:500]
        hasil.append(lo_ringkas)
    return hasil


def _lengkapi_field(item: dict) -> dict:
    """Pastikan semua field wajib ada, isi default kalau model lupa."""
    for f in FIELD_WAJIB:
        if f not in item or item[f] in ("", None):
            item[f] = "Tidak disebutkan"
    return item


def _proses_chunk(chunk: list[dict]) -> list[dict]:
    """
    Kirim satu chunk ke OpenAI untuk dibersihkan.
    Kalau chunk ini gagal (error API / JSON tidak valid), kembalikan list
    kosong HANYA untuk chunk ini -- bukan menghapus seluruh hasil.
    """
    if not chunk:
        return []

    input_data = json.dumps(chunk, ensure_ascii=False)

    system_prompt = """
Bersihkan data lowongan kerja hasil scraping.

Tugas:
- Rapikan judul, perusahaan, lokasi, dan deskripsi.
- Tentukan kategori pekerjaan.
- HANYA hapus data yang benar-benar tidak valid:
  - judul kosong
  - perusahaan kosong
  - duplikat sama persis
  - bukan lowongan kerja

Jangan hapus karena:
- gaji kosong
- lokasi kosong
- deskripsi kosong
- "terlihat kurang menarik" / bukan yang "terbaik"

Ini adalah tugas MEMBERSIHKAN, bukan menyeleksi/meranking. Pertahankan
SEMUA lowongan yang valid, jangan buang item hanya karena kamu menganggap
ada item lain yang lebih bagus.

Jika data kosong gunakan null atau "Tidak disebutkan".

PENTING soal tipe_kerja "magang": kalau judul/deskripsi menyebut magang,
internship, PKL, praktik kerja, trainee, atau apprentice, tipe_kerja WAJIB
diisi "magang" -- jangan diubah jadi "full-time"/"part-time"/"kontrak" hanya
karena platform asal tidak melabelinya secara eksplisit. Jangan buang
lowongan magang; perlakukan setara dengan lowongan reguler lainnya.

Output JSON object:
{"lowongan":[...]}

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

    max_tokens_chunk = len(chunk) * TOKEN_PER_ITEM_ESTIMASI + TOKEN_BUFFER

    response = None
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": input_data},
            ],
            temperature=0.2,
            max_tokens=max_tokens_chunk,
            response_format={"type": "json_object"},
        )

        finish_reason = response.choices[0].finish_reason
        hasil_teks = response.choices[0].message.content.strip()

        if finish_reason == "length":
            print(
                f"⚠️ Output OpenAI terpotong (max_tokens={max_tokens_chunk}) "
                f"untuk chunk berisi {len(chunk)} item. Pertimbangkan "
                f"memperkecil UKURAN_CHUNK."
            )

        hasil = json.loads(hasil_teks)

        if isinstance(hasil, dict):
            hasil = (
                hasil.get("lowongan")
                or hasil.get("data")
                or hasil.get("jobs")
                or []
            )

        if not isinstance(hasil, list):
            print(f"⚠️ Format hasil tidak terduga untuk 1 chunk, dilewati: {type(hasil)}")
            return []

        return [_lengkapi_field(item) for item in hasil if isinstance(item, dict)]

    except json.JSONDecodeError as e:
        print(f"❌ JSON OpenAI tidak valid untuk 1 chunk ({len(chunk)} item): {e}")
        if response:
            print(response.choices[0].message.content[:500])
        return []

    except Exception as e:
        print(f"❌ Error OpenAI untuk 1 chunk ({len(chunk)} item): {e}")
        return []


def bersihkan_dan_rangkum(
    lowongan_mentah: list[dict],
    jumlah: int | None = None,
) -> list[dict]:
    """
    Membersihkan data lowongan hasil scraping menggunakan OpenAI.

    jumlah:
        None (default) -> kembalikan SEMUA lowongan valid, tidak dipotong.
        int             -> batasi hasil akhir maksimal N item (dipilih
                            secara berurutan dari hasil yang sudah bersih,
                            bukan lewat instruksi "ranking terbaik" ke LLM,
                            supaya tidak ada bias pembuangan data valid).
    """

    if not lowongan_mentah:
        return []

    lowongan_dipangkas = _pangkas_deskripsi(lowongan_mentah)

    jumlah_chunk = math.ceil(len(lowongan_dipangkas) / UKURAN_CHUNK)
    hasil_gabungan: list[dict] = []

    for i in range(jumlah_chunk):
        chunk = lowongan_dipangkas[i * UKURAN_CHUNK : (i + 1) * UKURAN_CHUNK]
        hasil_chunk = _proses_chunk(chunk)
        if not hasil_chunk and chunk:
            print(
                f"⚠️ Chunk {i + 1}/{jumlah_chunk} ({len(chunk)} item) gagal "
                f"diproses dan dilewati -- item lain tetap diproses."
            )
        hasil_gabungan.extend(hasil_chunk)

    if jumlah is not None and jumlah > 0:
        return hasil_gabungan[:jumlah]

    return hasil_gabungan


# Alias agar script lama tetap berjalan
# main.py yang memanggil filter_dan_rangkum tidak perlu diubah
filter_dan_rangkum = bersihkan_dan_rangkum