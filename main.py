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
import pickle

# Flask – wymagany przez Render Web Service
from flask import Flask
from threading import Thread

# Zmienne środowiskowe
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

CHECK_INTERVAL  = int(os.getenv('CHECK_INTERVAL', '12'))   # co ile sekund sprawdzać nowe linie

STATE_FILE = 'last_log_state.pkl'

# ────────────────────────────────────────────────
# RCON – tylko wysyłanie wiadomości
# ────────────────────────────────────────────────

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
            print("Brak RCON_HOST lub RCON_PASSWORD – pomijam RCON")
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
                raise Exception("Logowanie RCON nieudane")
                
            self.connected = True
            print("RCON połączony")
        except Exception as e:
            print(f"RCON connect error: {e}")
            self.connected = False

    async def send(self, command: str):
        if not self.connected or not self.sock:
            return
        try:
            pkt = self._build_packet(b'\x01' + command.encode('utf-8', errors='replace'))
            self.sock.send(pkt)
        except Exception as e:
            print(f"RCON send error: {e}")
            self.connected = False

    async def close(self):
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
        self.connected = False

# ────────────────────────────────────────────────
# Czytanie logów z FTP
# ────────────────────────────────────────────────

class FTPLogWatcher:
    def __init__(self):
        self.last_file = None
        self.last_line_count = 0
        self._load_state()

    def _load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'rb') as f:
                    data = pickle.load(f)
                    self.last_file = data.get('file')
                    self.last_line_count = data.get('lines', 0)
            except:
                pass

    def _save_state(self):
        try:
            with open(STATE_FILE, 'wb') as f:
                pickle.dump({'file': self.last_file, 'lines': self.last_line_count}, f)
        except:
            pass

    def _ftp_connect(self):
        ftp = FTP()
        ftp.connect(FTP_HOST, FTP_PORT, timeout=10)
        ftp.login(FTP_USER, FTP_PASS)
        if FTP_DIR:
            ftp.cwd(FTP_DIR)
        return ftp

    async def get_new_lines(self):
        if not all([FTP_HOST, FTP_USER, FTP_PASS]):
            print("Brak danych FTP – pomijam")
            return []

        try:
            ftp = self._ftp_connect()
            files = []
            ftp.retrlines('LIST', files.append)
            adm_files = [line.split()[-1] for line in files if line.lower().endswith('.adm')]
            ftp.quit()

            if not adm_files:
                return []

            # Najnowszy plik (zakładamy sortowanie alfabetyczne odwrotne = najnowszy)
            latest = sorted(adm_files, reverse=True)[0]

            if latest != self.last_file:
                print(f"Nowy plik logów: {latest}")
                self.last_file = latest
                self.last_line_count = 0

            ftp = self._ftp_connect()
            buf = io.BytesIO()
            ftp.retrbinary(f'RETR {latest}', buf.write)
            ftp.quit()

            buf.seek(0)
            content = buf.read().decode('utf-8', errors='replace')
            lines = content.splitlines()

            new_lines = lines[self.last_line_count:]
            self.last_line_count = len(lines)
            self._save_state()

            return [line.strip() for line in new_lines if line.strip()]

        except Exception as e:
            print(f"Błąd FTP: {e}")
            return []

    async def run(self, callback):
        while True:
            lines = await self.get_new_lines()
            for line in lines:
                # Poprawiony parser – obsługuje format z podanego logu: HH:MM:SS | [Chat - Global]("NICK"(id=ID)): wiadomość
                m = re.match(r'^(\d{2}:\d{2}:\d{2}) \| \[Chat - (Global|Direct|Group|Side|Vehicle)\]\("([^"]+)"(?:\s*\(id=[^\)]+\))?\): (.+)$', line)
                if m:
                    t, channel, nick, msg = m.groups()
                    await callback(f"[{t}] **{nick}** ({channel}): {msg}")
                    continue

                # Wariant bez kanału w nawiasie kwadratowym
                m = re.match(r'^(\d{2}:\d{2}:\d{2}) \| Chat\("([^"]+)"(?:\s*\(id=[^\)]+\))?\): (.+)$', line)
                if m:
                    t, nick, msg = m.groups()
                    await callback(f"[{t}] **{nick}**: {msg}")
                    continue

                # Luźny wariant bez "Chat": HH:MM:SS | [Global] Nick: wiadomość
                m = re.match(r'^(\d{2}:\d{2}:\d{2}) \| \[(Global|Direct|Group|Side|Vehicle)\] ([^:]+): (.+)$', line)
                if m:
                    t, channel, nick, msg = m.groups()
                    await callback(f"[{t}] **{nick}** ({channel}): {msg}")
                    continue

                # Bardzo prosty: HH:MM:SS | Nick: wiadomość
                m = re.match(r'^(\d{2}:\d{2}:\d{2}) \| ([^:]+): (.+)$', line)
                if m:
                    t, nick, msg = m.groups()
                    await callback(f"[{t}] **{nick}**: {msg}")

            await asyncio.sleep(CHECK_INTERVAL)

# ────────────────────────────────────────────────
# Discord bot
# ────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)

rcon = RCONClient()
watcher = FTPLogWatcher()

async def send_to_discord(message):
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel:
        await channel.send(message)

@bot.event
async def on_ready():
    print(f"Discord bot logged in as {bot.user}")
    await rcon.connect()
    asyncio.create_task(watcher.run(send_to_discord))

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if message.channel.id == DISCORD_CHANNEL_ID:
        # Relay to DayZ chat using RCON 'say -1 message' (-1 for global)
        dayz_message = f"{message.author.name}: {message.content}"
        await rcon.send_command(f"say -1 {dayz_message}")
    await bot.process_commands(message)

@bot.event
async def on_disconnect():
    await rcon.disconnect()

# Flask – minimalny serwer HTTP żeby Render nie wyłączał instancji
app = Flask(__name__)

@app.route('/')
def home():
    return "DayZ chat relay bot is running"

@app.route('/health')
def health():
    return "OK", 200

def run_flask():
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

Thread(target=run_flask, daemon=True).start()

# Run the bot
async def main():
    try:
        await bot.start(DISCORD_TOKEN)
    finally:
        await rcon.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
