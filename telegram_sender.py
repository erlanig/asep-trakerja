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
    if not lowongan_list:
        return ""

    tanggal = lowongan_list[0].get("tanggal_post", "-")

    baris = [
        f"📋 *Info Lowongan Kerja Hari Ini*",
        f"📅 {tanggal}\n"
    ]

    for i, lo in enumerate(lowongan_list, start=1):
        gaji = lo.get("gaji") or "Tidak disebutkan"
        lokasi = lo.get("lokasi") or "Tidak disebutkan"
        tipe = lo.get("tipe_kerja") or "Tidak disebutkan"

        baris.append(
            f"*{i}. {lo.get('judul', '-') }*\n"
            f"🏢 {lo.get('perusahaan', '-')}\n"
            f"📍 {lokasi} | 🕒 {tipe}\n"
            f"💰 {gaji}\n"
            f"🔗 [Lihat detail & lamar]({lo.get('sumber_url', '#')})\n"
        )

    # Footer
    baris.append(
        "━━━━━━━━━━━━━━\n"
        "_🤖 Asep TraKerja • Info lowongan otomatis_\n"
        "_#ForABetterLife_"
    )

    return "\n".join(baris)


def kirim_ke_grup(
    chat_id: str,
    lowongan_list: list[dict],
    message_thread_id: int | None = None
) -> bool:

    if not lowongan_list:
        return False

    payload = {
        "chat_id": chat_id,
        "text": format_pesan(lowongan_list),
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    if message_thread_id:
        payload["message_thread_id"] = message_thread_id

    try:
        resp = requests.post(
            API_URL,
            json=payload,
            timeout=15
        )

        data = resp.json()

        if not data.get("ok"):
            print(f"❌ Gagal kirim ke {chat_id}: {data}")
            return False

        return True

    except requests.RequestException as e:
        print(f"❌ Error koneksi Telegram: {e}")
        return False