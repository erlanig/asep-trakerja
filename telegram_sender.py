"""
Modul untuk mengirim daftar lowongan ke grup Telegram via Bot API.
"""

import os
import re
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


def _escape_markdown(teks) -> str:
    """
    Escape karakter spesial untuk Telegram parse_mode='Markdown' (legacy).
    Field dinamis dari hasil scraping (judul, perusahaan, lokasi, gaji,
    tipe_kerja) bisa mengandung _ * ` [ yang bikin entity Markdown jadi
    tidak seimbang -> seluruh pesan gagal terkirim dengan error
    "can't parse entities". Karakter-karakter itu di-escape di sini
    supaya diperlakukan sebagai teks biasa, bukan markup.

    Catatan: ']' dan ')' sengaja tidak di-escape karena pada mode
    Markdown legacy hanya 4 karakter di atas yang berfungsi sebagai
    pembuka entity; escape berlebihan malah bisa merusak tampilan.
    """
    if teks is None:
        return "-"
    teks = str(teks)
    return re.sub(r'([_*`\[])', r'\\\1', teks)


def _format_satu_item(nomor: int, lo: dict) -> str:
    judul = _escape_markdown(lo.get("judul") or "-")
    perusahaan = _escape_markdown(lo.get("perusahaan") or "-")
    lokasi = _escape_markdown(lo.get("lokasi") or "Tidak disebutkan")
    tipe = _escape_markdown(lo.get("tipe_kerja") or "Tidak disebutkan")
    gaji = _escape_markdown(lo.get("gaji") or "Tidak disebutkan")
    # URL sengaja TIDAK di-escape supaya link tetap valid & bisa diklik
    url = lo.get("sumber_url") or "#"

    return (
        f"*{nomor}. {judul}*\n"
        f"🏢 {perusahaan}\n"
        f"📍 {lokasi} | 🕒 {tipe}\n"
        f"💰 {gaji}\n"
        f"🔗 [Lihat detail & lamar]({url})\n"
    )


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

        if data.get("ok"):
            return True

        deskripsi = data.get("description", "")

        # Fallback 1: kalau topic tujuan sudah ditutup/dihapus, coba kirim
        # ulang tanpa message_thread_id (jatuh ke topic General) supaya
        # pesan tetap sampai, bukan hilang begitu saja.
        if message_thread_id and any(k in deskripsi for k in ("TOPIC_CLOSED", "TOPIC_DELETED", "thread not found")):
            print(f"⚠️  Topic {message_thread_id} tidak bisa dipakai di {chat_id} ({deskripsi}), fallback ke General")
            payload_fallback = {**payload}
            payload_fallback.pop("message_thread_id", None)
            resp = requests.post(API_URL, json=payload_fallback, timeout=15)
            data = resp.json()
            if data.get("ok"):
                return True
            deskripsi = data.get("description", "")
            print(f"❌ Gagal kirim ke {chat_id} (fallback General): {data}")
            # lanjut ke fallback 2 di bawah kalau penyebabnya masih parse error

        # Fallback 2: kalau gagal karena parsing entity Markdown (mis. ada
        # karakter spesial yang lolos dari escape, atau kasus tak terduga
        # lain), kirim ulang sebagai PLAIN TEXT supaya isi tetap sampai
        # ke grup walau tanpa formatting bold/link cantik.
        if "can't parse entities" in deskripsi:
            print(f"⚠️  Gagal parse Markdown ke {chat_id} ({deskripsi}), fallback ke plain text")
            payload_plain = {**payload}
            payload_plain.pop("parse_mode", None)
            payload_plain.pop("message_thread_id", None) if message_thread_id and "Topic" in deskripsi else None
            if message_thread_id:
                payload_plain["message_thread_id"] = message_thread_id
            resp = requests.post(API_URL, json=payload_plain, timeout=15)
            data = resp.json()
            if data.get("ok"):
                return True
            print(f"❌ Gagal kirim ke {chat_id} (fallback plain text): {data}")
            return False

        print(f"❌ Gagal kirim ke {chat_id}: {data}")
        return False

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

    Penomoran item (nomor_awal) tetap berlanjut sesuai jumlah item asli di
    tiap chunk, terlepas dari chunk itu sukses terkirim atau tidak. Ini
    supaya kalau ada chunk yang gagal, nomor di chunk berikutnya tidak
    "menutupi" bolongnya secara diam-diam -- kegagalan tetap kelihatan
    jelas di log, bukan cuma soal nomor.
    """
    if not lowongan_list:
        return False

    chunks = _bagi_menjadi_chunk(lowongan_list)
    total_bagian = len(chunks)
    nomor_awal = 1
    semua_sukses = True
    bagian_gagal = []

    for idx, chunk in enumerate(chunks, start=1):
        teks = format_pesan(
            chunk,
            judul_pesan=judul_pesan,
            nomor_awal=nomor_awal,
            bagian=(idx, total_bagian),
        )
        sukses = _kirim_satu_pesan(chat_id, teks, message_thread_id=message_thread_id)
        if not sukses:
            semua_sukses = False
            bagian_gagal.append(idx)
        nomor_awal += len(chunk)

        if idx < total_bagian:
            time.sleep(0.7)  # jeda kecil antar-bagian biar tidak kena rate limit Telegram

    if bagian_gagal:
        print(f"⚠️  {chat_id}: bagian gagal terkirim -> {bagian_gagal}/{total_bagian}")

    return semua_sukses