"""
Modul untuk mengirim daftar lowongan ke grup Telegram via Bot API.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


def format_pesan(lowongan_list: list[dict]) -> str:
    tanggal = lowongan_list[0]["tanggal_post"] if lowongan_list else ""
    baris = [f"📋 *Info Lowongan Kerja Hari Ini* ({tanggal})\n"]

    for i, lo in enumerate(lowongan_list, start=1):
        gaji = lo.get("gaji") or "Tidak disebutkan"
        baris.append(
            f"*{i}. {lo['judul']}*\n"
            f"🏢 {lo['perusahaan']}\n"
            f"📍 {lo['lokasi']} | 🕒 {lo['tipe_kerja']}\n"
            f"💰 {gaji}\n"
            f"🔗 [Lihat detail & lamar]({lo['sumber_url']})\n"
        )

    baris.append(
        "\n_Info dikumpulkan otomatis oleh bot. Cek detail lengkap di link masing-masing._"
        "\n\n🚀 Oleh Asep TraKerja | #ForABetterLife"
    )
    return "\n".join(baris)


def kirim_ke_grup(chat_id: str, lowongan_list: list[dict], message_thread_id: int | None = None) -> bool:
    if not lowongan_list:
        return False

    pesan = format_pesan(lowongan_list)

    payload = {
        "chat_id": chat_id,
        "text": pesan,
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
        print(f"❌ Error koneksi saat kirim ke {chat_id}: {e}")
        return False