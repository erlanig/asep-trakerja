"""
Modul untuk mengirim daftar lowongan ke grup Telegram via Bot API.
"""

import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


# Telegram membatasi pesan teks maksimal 4096 karakter. Dengan sampai ~100
# lowongan per kiriman, satu pesan besar pasti kelewat batas itu, jadi kita
# pakai batas aman lebih kecil (beri ruang untuk header/footer per chunk)
# dan pecah jadi beberapa pesan berurutan kalau perlu.
BATAS_AMAN_KARAKTER = 3500

JUDUL_DEFAULT_REGULER = "📋 *Info Lowongan Kerja Hari Ini*"
JUDUL_DEFAULT_MAGANG = "🎓 *Info Lowongan Magang Hari Ini*"


def _format_satu_item(nomor: int, lo: dict) -> str:
    gaji = lo.get("gaji") or "Tidak disebutkan"
    lokasi = lo.get("lokasi") or "Tidak disebutkan"
    tipe = lo.get("tipe_kerja") or "Tidak disebutkan"

    return (
        f"*{nomor}. {lo.get('judul', '-')}*\n"
        f"🏢 {lo.get('perusahaan', '-')}\n"
        f"📍 {lokasi} | 🕒 {tipe}\n"
        f"💰 {gaji}\n"
        f"🔗 [Lihat detail & lamar]({lo.get('sumber_url', '#')})\n"
    )


# --- BAGIAN YANG DIPERBARUI ---
FOOTER_PESAN = (
    "━━━━━━━━━━━━━━\n"
    "💡 *Tingkatkan Peluang Karirmu!*\n"
    "Kunjungi [www.trakerja.com](https://www.trakerja.com) untuk:\n"
    "✨ Job Tracking\n"
    "✨ Buat CV ATS\n"
    "✨ AI CV Analyzer\n"
    "✨ Cover Letter Generator\n"
    "━━━━━━━━━━━━━━\n"
    "_🤖 Asep TraKerja • Info lowongan otomatis_\n"
    "_#ForABetterLife_"
)
# ------------------------------


def _bagi_menjadi_chunk(lowongan_list: list[dict], batas_karakter: int = BATAS_AMAN_KARAKTER) -> list[list[dict]]:
    """
    Bagi daftar lowongan jadi beberapa kelompok supaya tiap pesan Telegram
    tidak melebihi batas karakter aman. Nomor urut item tetap berlanjut
    lintas-chunk (ditangani di format_pesan lewat parameter `nomor_awal`).
    """
    chunks: list[list[dict]] = []
    chunk_sekarang: list[dict] = []
    panjang_sekarang = 0

    for lo in lowongan_list:
        panjang_item = len(_format_satu_item(1, lo))  # perkiraan panjang, nomor tidak signifikan
        if chunk_sekarang and (panjang_sekarang + panjang_item) > batas_karakter:
            chunks.append(chunk_sekarang)
            chunk_sekarang = []
            panjang_sekarang = 0
        chunk_sekarang.append(lo)
        panjang_sekarang += panjang_item

    if chunk_sekarang:
        chunks.append(chunk_sekarang)

    return chunks


def format_pesan(
    lowongan_list: list[dict],
    judul_pesan: str = JUDUL_DEFAULT_REGULER,
    nomor_awal: int = 1,
    bagian: tuple[int, int] | None = None,
) -> str:
    
    if not lowongan_list:
        return ""

    tanggal = lowongan_list[0].get("tanggal_post", "-")

    header = judul_pesan
    if bagian and bagian[1] > 1:
        header += f" (Bagian {bagian[0]}/{bagian[1]})"

    baris = [header, f"📅 {tanggal}\n"]

    for offset, lo in enumerate(lowongan_list):
        baris.append(_format_satu_item(nomor_awal + offset, lo))

    # Cek apakah ini bagian terakhir (atau tidak dipecah sama sekali)
    is_bagian_terakhir = not bagian or (bagian[0] == bagian[1])
    
    if is_bagian_terakhir:
        # Munculkan promosi lengkap TraKerja di bagian paling akhir
        baris.append(FOOTER_PESAN)
    else:
        # Jika masih ada bagian selanjutnya, beri footer pemisah sederhana
        baris.append("━━━━━━━━━━━━━━\n_Bersambung ke bagian selanjutnya..._")

    return "\n".join(baris)


def _kirim_satu_pesan(chat_id: str, teks: str, message_thread_id: int | None = None) -> bool:
    payload = {
        "chat_id": chat_id,
        "text": teks,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id

    try:
        resp = requests.post(API_URL, json=payload, timeout=15)
        data = resp.json()
        if not data.get("ok"):
            print(f"❌ Gagal kirim ke {chat_id}: {data}")
            return False
        return True
    except requests.RequestException as e:
        print(f"❌ Error koneksi Telegram: {e}")
        return False


def kirim_ke_grup(
    chat_id: str,
    lowongan_list: list[dict],
    message_thread_id: int | None = None,
    judul_pesan: str = JUDUL_DEFAULT_REGULER,
) -> bool:
    """
    Kirim daftar lowongan ke satu grup. Kalau daftarnya panjang (mis. sampai
    ~100 item untuk sekali scrape), otomatis dipecah jadi beberapa pesan
    berurutan supaya tidak melebihi batas 4096 karakter Telegram. Return
    True hanya kalau SEMUA bagian berhasil terkirim.
    """
    if not lowongan_list:
        return False

    chunks = _bagi_menjadi_chunk(lowongan_list)
    total_bagian = len(chunks)
    nomor_awal = 1
    semua_sukses = True

    for idx, chunk in enumerate(chunks, start=1):
        teks = format_pesan(
            chunk,
            judul_pesan=judul_pesan,
            nomor_awal=nomor_awal,
            bagian=(idx, total_bagian),
        )
        sukses = _kirim_satu_pesan(chat_id, teks, message_thread_id=message_thread_id)
        semua_sukses = semua_sukses and sukses
        nomor_awal += len(chunk)

        if idx < total_bagian:
            time.sleep(0.7)  # jeda kecil antar-bagian biar tidak kena rate limit Telegram

    return semua_sukses