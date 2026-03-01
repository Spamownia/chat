import os
import asyncio
import discord
from discord.ext import commands
import socket
import struct
import re
import pickle
import io
from ftplib import FTP
from threading import Thread
from flask import Flask

# ────────────────────────────────────────────────
# Konfiguracja
# ────────────────────────────────────────────────
RCON_HOST      = os.getenv('RCON_HOST')
RCON_PORT      = int(os.getenv('RCON_PORT', '2305'))
RCON_PASSWORD  = os.getenv('RCON_PASSWORD')
DISCORD_TOKEN  = os.getenv('DISCORD_TOKEN')
DISCORD_CHANNEL_ID = int(os.getenv('DISCORD_CHANNEL_ID', '0'))
FTP_HOST       = os.getenv('FTP_HOST')
FTP_PORT       = int(os.getenv('FTP_PORT', '21'))
FTP_USER       = os.getenv('FTP_USER')
FTP_PASS       = os.getenv('FTP_PASS')
FTP_DIR        = os.getenv('FTP_DIR', '/config/')
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '12'))

STATE_FILE = 'last_log_state.pkl'

print("=== DAYZ ↔ DISCORD RELAY v2 ===")
print(f"RCON:     {RCON_HOST or 'BRAK'}:{RCON_PORT}  hasło: {'tak' if RCON_PASSWORD else 'NIE'}")
print(f"FTP:      {FTP_HOST or 'BRAK'}")
print(f"Discord channel: {DISCORD_CHANNEL_ID}")
print("=" * 60)

# ────────────────────────────────────────────────
# TEST POŁĄCZENIA SOCKET (nowa funkcja debug)
# ────────────────────────────────────────────────
async def test_rcon_connection():
    if not RCON_HOST or not RCON_PORT:
        print("[TEST RCON] Brak hosta lub portu")
        return False
    print(f"[TEST RCON] Próba surowego połączenia z {RCON_HOST}:{RCON_PORT}...")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(6)
        s.connect((RCON_HOST, RCON_PORT))
        print("[TEST RCON] ✓ POŁĄCZENIE SOCKET UDANE – port otwarty!")
        s.close()
        return True
    except Exception as e:
        print(f"[TEST RCON] ❌ BŁĄD: {type(e).__name__}: {e}")
        print("   → Najczęstsze przyczyny: firewall hostingu / blokada outbound na Render / zły port")
        return False

# ────────────────────────────────────────────────
# BattleEye RCON
# ────────────────────────────────────────────────
class BattleEyeRcon:
    def __init__(self):
        self.sock = None
        self.connected = False
        self.sequence = 0

    async def connect(self):
        if not RCON_HOST or not RCON_PASSWORD:
            print("RCON → brak danych → pomijam")
            return False

        print("[RCON] Rozpoczynam logowanie...")
        for attempt in range(1, 4):
            print(f"[RCON] Próba {attempt}/3 → {RCON_HOST}:{RCON_PORT}")
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(8)
                self.sock.connect((RCON_HOST, RCON_PORT))
                
                login_data = RCON_PASSWORD.encode('utf-8') + b'\x00'
                packet = self._build_packet(0, login_data)
                self.sock.send(packet)
                
                response = self._receive()
                if response and len(response) >= 9 and response[8] == 0x01:
                    self.connected = True
                    print("[RCON] POŁĄCZONO ✓ GOTOWE DO UŻYCIA!")
                    return True
                else:
                    print("[RCON] Nieudane logowanie (zła odpowiedź)")
            except Exception as e:
                print(f"[RCON] Błąd połączenia (próba {attempt}): {type(e).__name__}: {e}")
            self._close_socket()
            await asyncio.sleep(3)
        return False

    def _build_packet(self, pkt_type: int, data: bytes) -> bytes:
        self.sequence += 1
        body = struct.pack('<I', self.sequence) + bytes([pkt_type]) + data
        header = struct.pack('<I', len(body))
        return header + body

    def _receive(self) -> bytes:
        try:
            header = self.sock.recv(4)
            if len(header) != 4: return b''
            size = struct.unpack('<I', header)[0]
            return self.sock.recv(size)
        except:
            return b''

    async def send_command(self, command: str) -> bool:
        if not self.connected:
            print("[RCON] Nie połączony – pomijam komendę")
            return False
        try:
            cmd_bytes = command.encode('utf-8', errors='replace') + b'\x00'
            packet = self._build_packet(1, cmd_bytes)
            self.sock.send(packet)
            print(f"[RCON] Wysłano: {command}")
            return True
        except Exception as e:
            print(f"[RCON] Błąd wysyłania: {e}")
            self.connected = False
            return False

    def _close_socket(self):
        if self.sock:
            try: self.sock.close()
            except: pass
            self.sock = None

    async def close(self):
        self._close_socket()
        self.connected = False

# ────────────────────────────────────────────────
# FTP Watcher (tylko .ADM – czat)
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
            except: pass

    def _save_state(self):
        try:
            with open(STATE_FILE, 'wb') as f:
                pickle.dump({'file': self.last_file, 'lines': self.last_line_count}, f)
        except: pass

    def _ftp_connect(self):
        ftp = FTP()
        ftp.connect(FTP_HOST, FTP_PORT, timeout=10)
        ftp.login(FTP_USER, FTP_PASS)
        if FTP_DIR and FTP_DIR != '/': ftp.cwd(FTP_DIR)
        return ftp

    async def get_new_lines(self):
        if not all([FTP_HOST, FTP_USER, FTP_PASS]): return []
        try:
            ftp = self._ftp_connect()
            files = []
            ftp.retrlines('LIST', files.append)
            ftp.quit()

            candidates = [line.split()[-1] for line in files if re.match(r'^DayZServer_x64_.*\.adm$', line.split()[-1], re.IGNORECASE)]
            if not candidates: return []

            latest = max(candidates)
            if latest != self.last_file:
                self.last_file = latest
                self.last_line_count = 0

            ftp = self._ftp_connect()
            buf = io.BytesIO()
            ftp.retrbinary(f'RETR {latest}', buf.write)
            ftp.quit()

            buf.seek(0)
            lines = buf.read().decode('utf-8', errors='replace').splitlines()
            new_lines = lines[self.last_line_count:]
            self.last_line_count = len(lines)
            self._save_state()
            return [line.strip() for line in new_lines if line.strip()]
        except: return []

    async def run(self, callback):
        while True:
            lines = await self.get_new_lines()
            for line in lines:
                m = re.match(
                    r'^(\d{2}:\d{2}:\d{2})\s*\|\s*\[Chat\s*-\s*(\w+)\]\s*\("([^"]+)"\s*\([^)]+\)\)\s*:\s*(.+)$',
                    line, re.IGNORECASE
                )
                if m:
                    time, ch, nick, msg = m.groups()
                    await callback(f"[{time}] **{nick}** ({ch}): {msg.strip()}")
            await asyncio.sleep(CHECK_INTERVAL)

# ────────────────────────────────────────────────
# Discord Bot
# ────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

rcon = BattleEyeRcon()
watcher = FTPLogWatcher()

async def send_to_discord(msg: str):
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel: await channel.send(msg[:1990])

@bot.event
async def on_ready():
    print(f"[Discord] Zalogowano jako {bot.user}")
    await test_rcon_connection()          # ← NOWY TEST
    success = await rcon.connect()
    if success:
        print("[RCON] Gotowy do wysyłania wiadomości z Discorda!")
    asyncio.create_task(watcher.run(send_to_discord))

@bot.event
async def on_message(message):
    if message.author.bot or message.channel.id != DISCORD_CHANNEL_ID: 
        return await bot.process_commands(message)
    
    content = message.clean_content.strip()
    if not content: return

    dayz_msg = f"{message.author.display_name}: {content.replace('\"', \"'\")}"
    print(f"[Discord → DayZ] {dayz_msg}")

    if rcon.connected:
        await rcon.send_command(f"say -1 {dayz_msg}")
    else:
        await message.reply("⚠ RCON nadal niepodłączony", delete_after=8)

    await bot.process_commands(message)

# ────────────────────────────────────────────────
# Flask keep-alive
# ────────────────────────────────────────────────
app = Flask(__name__)
@app.route('/') 
def home(): return "DayZ ↔ Discord bridge działa"
def run_flask():
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
Thread(target=run_flask, daemon=True).start()

# ────────────────────────────────────────────────
# Start
# ────────────────────────────────────────────────
async def main():
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
