"""
Pipeline utama: scrape -> filter/rangkum via OpenAI -> simpan DB -> kirim ke Telegram.

Versi ini memisahkan lowongan REGULER dan MAGANG sepanjang pipeline:
  1. Scraping mengumpulkan pool lebih besar (sampai ~100 lowongan gabungan
     sekali jalan, lihat scraper.scrape_semua_sumber) dan sudah melakukan
     klasifikasi ulang tipe_kerja + filter rentang tanggal posting (default
     30 hari terakhir — lihat catatan lengkap di scraper.py soal kenapa
     filter deadline "tidak tutup hari ini" tidak bisa dijamin dari sisi
     scraping publik).
  2. Data mentah dipisah jadi dua kelompok (reguler vs magang) SEBELUM
     dikirim ke OpenAI, supaya magang tidak "tenggelam" kalau AI cuma
     mengambil top-N dari kolam campuran.
  3. Masing-masing kelompok difilter/dirangkum terpisah dengan kuota
     sendiri (JUMLAH_LOWONGAN_PER_HARI untuk reguler, JUMLAH_MAGANG_PER_HARI
     untuk magang).
  4. Disimpan ke DB seperti biasa (dedup otomatis via UNIQUE sumber_url).
  5. Dikirim ke tiap grup Telegram sebagai maksimal 2 pesan terpisah:
     "Info Lowongan Kerja Hari Ini" dan "Info Lowongan Magang Hari Ini",
     ke topic TOPIC_ID_INFO_LOWONGAN (bukan topic General).
     Kalau salah satu kelompok kosong, pesan untuk kelompok itu dilewati.
     Tiap pesan otomatis dipecah lagi kalau kepanjangan (lihat
     telegram_sender.kirim_ke_grup).

Jalankan manual untuk test:
    python main.py

Jalankan otomatis via cron jam 15:30 setiap hari (lihat README.md untuk setup cron).
"""

import os
from dotenv import load_dotenv

import db
import scraper
import ai_filter
import telegram_sender

load_dotenv()

# Kuota lowongan yang mau dikirim per hari, dipisah per kelompok. Total
# gabungan default: 70 + 30 = 100. Bisa diatur ulang lewat .env tanpa ubah
# kode.
JUMLAH_REGULER_PER_HARI = int(os.getenv("JUMLAH_LOWONGAN_PER_HARI", 70))
JUMLAH_MAGANG_PER_HARI = int(os.getenv("JUMLAH_MAGANG_PER_HARI", 30))

# Berapa banyak lowongan mentah yang diminta scraper PER SUMBER. Sumber ada
# ~18, jadi limit_per_sumber=40 + limit_magang_per_sumber=20 sudah cukup
# untuk menghasilkan pool ratusan lowongan mentah sebelum dedup & filter AI.
LIMIT_PER_SUMBER = int(os.getenv("SCRAPER_LIMIT_PER_SUMBER", 40))
LIMIT_MAGANG_PER_SUMBER = int(os.getenv("SCRAPER_LIMIT_MAGANG_PER_SUMBER", 20))

# Rentang tanggal posting yang diambil (hari terakhir). Lihat catatan di
# scraper._dalam_rentang_hari soal kenapa ini dipakai sebagai proxy untuk
# "belum tutup", karena kebanyakan sumber publik tidak expose tanggal tutup.
HARI_RENTANG = int(os.getenv("SCRAPER_HARI_RENTANG", 30))

# Topic tujuan di grup Telegram untuk pengiriman lowongan.
# 6 = topic "Informasi Lowongan Kerja" di grup Komunitas Trakerja.
# NB: ini konstanta global (satu topic untuk semua grup di grup_list).
# Kalau nanti ada grup lain dengan topic berbeda, idealnya kolom topic_id
# ditambahkan ke tabel grup_telegram dan dibaca per-grup di sini.
TOPIC_ID_INFO_LOWONGAN = int(os.getenv("TELEGRAM_TOPIC_ID_INFO_LOWONGAN", 6))


def main():
    print("=" * 60)
    print("🚀 Mulai pipeline bot lowongan")
    print("=" * 60)

    # 1. Scraping data mentah dari semua sumber aktif (reguler + pass magang)
    print("\n[1/4] Scraping data lowongan...")
    data_mentah = scraper.scrape_semua_sumber(
        limit_per_sumber=LIMIT_PER_SUMBER,
        limit_magang_per_sumber=LIMIT_MAGANG_PER_SUMBER,
        hari_rentang=HARI_RENTANG,
    )
    mentah_reguler, mentah_magang = scraper.pisahkan_magang(data_mentah)
    print(f"    → Ditemukan {len(data_mentah)} lowongan mentah "
          f"({len(mentah_reguler)} reguler, {len(mentah_magang)} magang)")

    if not data_mentah:
        print("⚠️  Tidak ada data hasil scraping. Pipeline dihentikan.")
        return

    # 2. Filter & rangkum pakai OpenAI — reguler dan magang diproses TERPISAH
    #    supaya magang punya kuota sendiri dan tidak kalah bersaing di
    #    seleksi "top-N terbaik" versus lowongan reguler yang jumlahnya
    #    biasanya jauh lebih banyak.
    print("\n[2/4] Memproses & memfilter dengan OpenAI...")

    lowongan_reguler = ai_filter.filter_dan_rangkum(mentah_reguler, jumlah=JUMLAH_REGULER_PER_HARI) if mentah_reguler else []
    print(f"    → {len(lowongan_reguler)} lowongan reguler terpilih")

    lowongan_magang = ai_filter.filter_dan_rangkum(mentah_magang, jumlah=JUMLAH_MAGANG_PER_HARI) if mentah_magang else []
    # Jaring pengaman terakhir: pastikan hasil dari OpenAI benar-benar
    # bertanda "magang" (kalau AI meleset mengubah tipe_kerja).
    for lo in lowongan_magang:
        lo["tipe_kerja"] = "magang"
    print(f"    → {len(lowongan_magang)} lowongan magang terpilih")

    lowongan_terpilih = lowongan_reguler + lowongan_magang
    print(f"    → Total {len(lowongan_terpilih)} lowongan lolos filter")

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

    # 4. Ambil lowongan yang belum terkirim dari DB (dipisah per kelompok),
    #    lalu kirim ke semua grup aktif sebagai maksimal 2 pesan per grup,
    #    ke topic "Informasi Lowongan Kerja" (bukan topic General).
    print("\n[4/4] Mengirim ke grup Telegram...")

    reguler_untuk_kirim = db.ambil_lowongan_belum_kirim(limit=JUMLAH_REGULER_PER_HARI, tipe_kerja="!magang")
    magang_untuk_kirim = db.ambil_lowongan_belum_kirim(limit=JUMLAH_MAGANG_PER_HARI, tipe_kerja="magang")

    if not reguler_untuk_kirim and not magang_untuk_kirim:
        print("⚠️  Tidak ada lowongan berstatus 'belum' terkirim untuk dikirim.")
        return

    grup_list = db.ambil_grup_aktif()
    if not grup_list:
        print("⚠️  Tidak ada grup aktif terdaftar di tabel grup_telegram.")
        return

    id_terkirim = [lo["id"] for lo in reguler_untuk_kirim] + [lo["id"] for lo in magang_untuk_kirim]

    for grup in grup_list:
        hasil_per_kelompok = []

        if reguler_untuk_kirim:
            sukses_reguler = telegram_sender.kirim_ke_grup(
                chat_id=grup["chat_id"],
                lowongan_list=reguler_untuk_kirim,
                message_thread_id=TOPIC_ID_INFO_LOWONGAN,
                judul_pesan=telegram_sender.JUDUL_DEFAULT_REGULER,
            )
            hasil_per_kelompok.append(("reguler", len(reguler_untuk_kirim), sukses_reguler))

        if magang_untuk_kirim:
            sukses_magang = telegram_sender.kirim_ke_grup(
                chat_id=grup["chat_id"],
                lowongan_list=magang_untuk_kirim,
                message_thread_id=TOPIC_ID_INFO_LOWONGAN,
                judul_pesan=telegram_sender.JUDUL_DEFAULT_MAGANG,
            )
            hasil_per_kelompok.append(("magang", len(magang_untuk_kirim), sukses_magang))

        semua_sukses = all(sukses for _, _, sukses in hasil_per_kelompok)
        status = "sukses" if semua_sukses else "sebagian gagal"
        ringkasan = ", ".join(f"{label}: {jumlah}" for label, jumlah, _ in hasil_per_kelompok)

        db.catat_log_pengiriman(
            grup_id=grup["id"],
            jumlah=len(reguler_untuk_kirim) + len(magang_untuk_kirim),
            status=status,
            keterangan=f"Kirim ke {grup['nama_grup']} ({ringkasan})",
        )
        print(f"    → {grup['nama_grup']}: {status} ({ringkasan})")

    # Tandai semua lowongan yang baru dikirim sebagai 'terkirim'
    db.tandai_terkirim(id_terkirim)

    print("\n✅ Pipeline selesai.")


if __name__ == "__main__":
    main()