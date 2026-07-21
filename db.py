"""
Modul koneksi dan operasi database MySQL/MariaDB untuk bot lowongan.
"""

import os
import mysql.connector
from mysql.connector import Error
from dotenv import load_dotenv

load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "port": int(os.getenv("DB_PORT", 3306)),
    "database": os.getenv("DB_DATABASE"),
    "user": os.getenv("DB_USERNAME"),
    "password": os.getenv("DB_PASSWORD"),
    "connection_timeout": 10,
}


def get_connection():
    return mysql.connector.connect(**DB_CONFIG)


def simpan_lowongan(lowongan: dict) -> bool:
    """
    Simpan satu lowongan ke DB. Skip otomatis kalau sumber_url sudah ada
    (dedupe via UNIQUE constraint di kolom sumber_url).
    Return True kalau berhasil disimpan (baris baru), False kalau sudah ada / gagal.
    """
    query = """
        INSERT INTO lowongan
            (judul, perusahaan, lokasi, tipe_kerja, kategori, deskripsi,
             gaji, sumber_platform, sumber_url, tanggal_post)
        VALUES (%(judul)s, %(perusahaan)s, %(lokasi)s, %(tipe_kerja)s, %(kategori)s,
                %(deskripsi)s, %(gaji)s, %(sumber_platform)s, %(sumber_url)s, %(tanggal_post)s)
        ON DUPLICATE KEY UPDATE
            judul = judul  -- no-op, cukup untuk skip tanpa error
    """
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(query, lowongan)
        conn.commit()
        inserted = cursor.lastrowid != 0 and cursor.rowcount == 1
        cursor.close()
        return inserted
    except Error as e:
        print(f"❌ Gagal simpan lowongan '{lowongan.get('judul')}': {e}")
        return False
    finally:
        if conn and conn.is_connected():
            conn.close()


def ambil_lowongan_belum_kirim(limit: int = 10, tipe_kerja: str | None = None) -> list[dict]:
    """
    Ambil lowongan berstatus 'belum' terkirim.
    `tipe_kerja`:
      - None (default)  -> semua tipe (perilaku lama)
      - "magang"         -> hanya lowongan magang
      - "!magang"        -> semua KECUALI magang (dipakai untuk pesan "reguler")
    Dipakai untuk memisah pengiriman jadi 2 pesan: lowongan kerja reguler
    dan lowongan magang.
    """
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        if tipe_kerja == "magang":
            where_tipe = "AND tipe_kerja = 'magang'"
        elif tipe_kerja == "!magang":
            where_tipe = "AND (tipe_kerja IS NULL OR tipe_kerja <> 'magang')"
        else:
            where_tipe = ""

        cursor.execute(
            f"""
            SELECT * FROM lowongan
            WHERE status_kirim = 'belum'
            {where_tipe}
            ORDER BY tanggal_post DESC, created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cursor.fetchall()
        cursor.close()
        return rows
    except Error as e:
        print(f"❌ Gagal ambil lowongan: {e}")
        return []
    finally:
        if conn and conn.is_connected():
            conn.close()


def tandai_terkirim(ids: list[int]):
    if not ids:
        return
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        format_ids = ",".join(["%s"] * len(ids))
        cursor.execute(
            f"""
            UPDATE lowongan
            SET status_kirim = 'terkirim', tanggal_kirim = NOW()
            WHERE id IN ({format_ids})
            """,
            tuple(ids),
        )
        conn.commit()
        cursor.close()
    except Error as e:
        print(f"❌ Gagal update status_kirim: {e}")
    finally:
        if conn and conn.is_connected():
            conn.close()


def ambil_grup_aktif() -> list[dict]:
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM grup_telegram WHERE aktif = 1")
        rows = cursor.fetchall()
        cursor.close()
        return rows
    except Error as e:
        print(f"❌ Gagal ambil grup: {e}")
        return []
    finally:
        if conn and conn.is_connected():
            conn.close()


def catat_log_pengiriman(grup_id: int, jumlah: int, status: str, keterangan: str = ""):
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO log_pengiriman (grup_id, jumlah_lowongan, status, keterangan)
            VALUES (%s, %s, %s, %s)
            """,
            (grup_id, jumlah, status, keterangan),
        )
        conn.commit()
        cursor.close()
    except Error as e:
        print(f"❌ Gagal catat log: {e}")
    finally:
        if conn and conn.is_connected():
            conn.close()