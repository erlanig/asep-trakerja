"""
Pipeline utama: scrape -> filter/rangkum via OpenAI -> simpan DB -> kirim ke Telegram.

Jalankan manual untuk test:
    python main.py

Jalankan otomatis via cron jam 15:00 setiap hari (lihat README.md untuk setup cron).
"""

import os
from dotenv import load_dotenv

import db
import scraper
import ai_filter
import telegram_sender

load_dotenv()

JUMLAH_PER_HARI = int(os.getenv("JUMLAH_LOWONGAN_PER_HARI", 10))


def main():
    print("=" * 60)
    print("🚀 Mulai pipeline bot lowongan")
    print("=" * 60)

    # 1. Scraping data mentah dari semua sumber aktif
    print("\n[1/4] Scraping data lowongan...")
    data_mentah = scraper.scrape_semua_sumber()
    print(f"    → Ditemukan {len(data_mentah)} lowongan mentah")

    if not data_mentah:
        print("⚠️  Tidak ada data hasil scraping. Pipeline dihentikan.")
        return

    # 2. Filter & rangkum pakai OpenAI, pilih top-N terbaik
    print("\n[2/4] Memproses & memfilter dengan OpenAI...")
    lowongan_terpilih = ai_filter.filter_dan_rangkum(data_mentah, jumlah=JUMLAH_PER_HARI)
    print(f"    → {len(lowongan_terpilih)} lowongan terpilih setelah filter")

    if not lowongan_terpilih:
        print("⚠️  Tidak ada lowongan lolos filter. Pipeline dihentikan.")
        return

    # 3. Simpan ke database (otomatis skip duplikat via sumber_url)
    print("\n[3/4] Menyimpan ke database...")
    berhasil_simpan = 0
    for lo in lowongan_terpilih:
        if db.simpan_lowongan(lo):
            berhasil_simpan += 1
    print(f"    → {berhasil_simpan} lowongan baru disimpan (sisanya kemungkinan duplikat)")

    # 4. Ambil lowongan yang belum terkirim dari DB, kirim ke semua grup aktif
    print("\n[4/4] Mengirim ke grup Telegram...")
    lowongan_untuk_kirim = db.ambil_lowongan_belum_kirim(limit=JUMLAH_PER_HARI)

    if not lowongan_untuk_kirim:
        print("⚠️  Tidak ada lowongan berstatus 'belum' terkirim untuk dikirim.")
        return

    grup_list = db.ambil_grup_aktif()
    if not grup_list:
        print("⚠️  Tidak ada grup aktif terdaftar di tabel grup_telegram.")
        return

    id_terkirim = [lo["id"] for lo in lowongan_untuk_kirim]

    for grup in grup_list:
        sukses = telegram_sender.kirim_ke_grup(
            chat_id=grup["chat_id"],
            lowongan_list=lowongan_untuk_kirim,
        )
        status = "sukses" if sukses else "gagal"
        db.catat_log_pengiriman(
            grup_id=grup["id"],
            jumlah=len(lowongan_untuk_kirim),
            status=status,
            keterangan=f"Kirim ke {grup['nama_grup']}",
        )
        print(f"    → {grup['nama_grup']}: {status}")

    # Tandai semua lowongan yang baru dikirim sebagai 'terkirim'
    db.tandai_terkirim(id_terkirim)

    print("\n✅ Pipeline selesai.")


if __name__ == "__main__":
    main()