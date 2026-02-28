import os
import asyncio
import discord
from discord.ext import commands
import socket
import struct
import hashlib
import binascii
import re
from ftplib import FTP
import io

# Flask
from flask import Flask
from threading import Thread

# Env vars (bez zmian)
RCON_HOST       = os.getenv('RCON_HOST')
RCON_PORT       = int(os.getenv('RCON_PORT', '2302'))
RCON_PASSWORD   = os.getenv('RCON_PASSWORD')

DISCORD_TOKEN    = os.getenv('DISCORD_TOKEN')
DISCORD_CHANNEL_ID = int(os.getenv('DISCORD_CHANNEL_ID'))
BOT_PREFIX       = os.getenv('BOT_PREFIX', '!')

FTP_HOST        = os.getenv('FTP_HOST')
FTP_PORT        = int(os.getenv('FTP_PORT', '21'))
FTP_USER        = os.getenv('FTP_USER')
FTP_PASS        = os.getenv('FTP_PASS')
FTP_DIR         = os.getenv('FTP_DIR', '/')

CHECK_INTERVAL  = int(os.getenv('CHECK_INTERVAL', '12'))

# Tymczasowo WYŁĄCZAMY pickle – zawsze startujemy od zera
STATE_FILE = None   # <--- wyłączamy zapamiętywanie

# RCON (bez zmian)
class RCONClient:
    def __init__(self):
        self.sock = None
        self.connected = False
        self.packet_counter = 0

    def _build_packet(self, data):
        self.packet_counter = (self.packet_counter + 1) % 256
        packet = b'\xFF' + struct.pack('<B', self.packet_counter) + data
        crc = struct.pack('<I', binascii.crc32(packet) & 0xffffffff)
        return b'BE' + crc + packet

    async def connect(self):
        if not RCON_HOST or not RCON_PASSWORD:
            print("Brak RCON_HOST lub RCON_PASSWORD")
            return
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(6)
            self.sock.connect((RCON_HOST, RCON_PORT))
            login_data = b'\x00' + hashlib.md5(RCON_PASSWORD.encode()).digest()
            pkt = self._build_packet(login_data)
            self.sock.send(pkt)
            resp = self.sock.recv(1024)
            if len(resp) < 8 or resp[7] != 1:
                raise Exception("RCON login failed")
            self.connected = True
            print("RCON OK")
        except Exception as e:
            print(f"RCON error: {e}")
            self.connected = False

    async def send(self, command: str):
        if not self.connected:
            return
        try:
            pkt = self._build_packet(b'\x01' + command.encode('utf-8', errors='replace'))
            self.sock.send(pkt)
        except:
            self.connected = False

    async def close(self):
        if self.sock:
            self.sock.close()
        self.connected = False

# FTP + MOCNY DEBUG
class FTPLogWatcher:
    def __init__(self):
        self.last_file = None
        self.last_line_count = 0   # zawsze od zera

    def _ftp_connect(self):
        ftp = FTP()
        ftp.connect(FTP_HOST, FTP_PORT, timeout=12)
        ftp.login(FTP_USER, FTP_PASS)
        try:
            ftp.cwd(FTP_DIR)
            print(f"cwd OK: {FTP_DIR}")
        except Exception as e:
            print(f"Błąd cwd {FTP_DIR}: {e}")
        return ftp

    async def get_new_lines(self):
        print("Próba pobrania listy plików...")
        try:
            ftp = self._ftp_connect()
            files = []
            ftp.retrlines('LIST', files.append)
            print(f"Znaleziono {len(files)} elementów w katalogu")

            log_files = []
            for line in files:
                fname = line.split()[-1]
                if fname.lower().endswith(('.adm', '.log', '.rpt')):
                    log_files.append(fname)
                    print(f" → wykryto log: {fname}")

            ftp.quit()

            if not log_files:
                print("!!! ZERO plików .adm / .log / .rpt !!!")
                return []

            latest = sorted(log_files, reverse=True)[0]
            print(f"Najnowszy plik: {latest}")

            if latest != self.last_file:
                print(f"NOWY PLIK → {latest}")
                self.last_file = latest
                self.last_line_count = 0

            print(f"Pobieram: {latest} (od linii {self.last_line_count})")

            ftp = self._ftp_connect()
            buf = io.BytesIO()
            ftp.retrbinary(f'RETR {latest}', buf.write)
            ftp.quit()

            buf.seek(0)
            content = buf.read().decode('utf-8', errors='replace')
            all_lines = content.splitlines()
            print(f"Plik ma {len(all_lines)} linii ogółem")

            new_lines = all_lines[self.last_line_count:]
            self.last_line_count = len(all_lines)
            print(f"Nowe linie: {len(new_lines)}")

            if new_lines:
                print("Pierwsze 5 nowych linii:")
                for i, ln in enumerate(new_lines[:5], 1):
                    print(f"   {i}> {ln[:120]}{'...' if len(ln)>120 else ''}")

            return [ln.strip() for ln in new_lines if ln.strip()]

        except Exception as e:
            print(f"!!! FTP BŁĄD: {type(e).__name__} → {str(e)}")
            return []

    async def run(self, callback):
        while True:
            lines = await self.get_new_lines()
            matched = 0

            for line in lines:
                m = re.match(r'^(\d{2}:\d{2}:\d{2}) \| \[Chat - (Global|Direct|Group|Side|Vehicle)\]\("([^"]+)"(?:\s*\([^)]+\))?\): (.+)$', line)
                if m:
                    t, ch, nick, msg = m.groups()
                    print(f"Dopasowano chat: {nick} ({ch}) → {msg}")
                    await callback(f"[{t}] **{nick}** ({ch}): {msg}")
                    matched += 1

            if matched == 0 and lines:
                print("Nie dopasowano żadnej linii chatu (sprawdź format powyżej)")

            await asyncio.sleep(CHECK_INTERVAL)

# Discord bot (bez zmian)
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)

rcon = RCONClient()
watcher = FTPLogWatcher()

async def send_to_discord(message):
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel:
        await channel.send(message)
        print(f"→ Discord: {message[:80]}...")

@bot.event
async def on_ready():
    print(f"Bot zalogowany: {bot.user}")
    await rcon.connect()
    asyncio.create_task(watcher.run(send_to_discord))

@bot.event
async def on_message(message):
    if message.author.bot: return
    if message.channel.id != DISCORD_CHANNEL_ID: return

    content = message.content.strip()
    if not content: return

    msg = f"{message.author.display_name}: {content}"
    await rcon.send(f'say -1 "{msg}"')
    print(f"→ Gra: {msg}")

    await bot.process_commands(message)

# Flask (bez zmian)
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot działa"

@app.route('/health')
def health():
    return "OK", 200

def run_flask():
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

Thread(target=run_flask, daemon=True).start()

async def main():
    try:
        await bot.start(DISCORD_TOKEN)
    finally:
        await rcon.close()

if __name__ == "__main__":
    asyncio.run(main())
