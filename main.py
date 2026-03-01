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
# Konfiguracja ze zmiennych środowiskowych
# ────────────────────────────────────────────────
RCON_HOST      = os.getenv('RCON_HOST')
RCON_PORT      = int(os.getenv('RCON_PORT', '2305'))          # domyślnie 2305 – zmień jeśli u Ciebie inny
RCON_PASSWORD  = os.getenv('RCON_PASSWORD')
DISCORD_TOKEN  = os.getenv('DISCORD_TOKEN')
DISCORD_CHANNEL_ID = int(os.getenv('DISCORD_CHANNEL_ID', '0'))
BOT_PREFIX     = os.getenv('BOT_PREFIX', '!')
FTP_HOST       = os.getenv('FTP_HOST')
FTP_PORT       = int(os.getenv('FTP_PORT', '21'))
FTP_USER       = os.getenv('FTP_USER')
FTP_PASS       = os.getenv('FTP_PASS')
FTP_DIR        = os.getenv('FTP_DIR', '/config/')
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '12'))       # sekundy

STATE_FILE = 'last_log_state.pkl'

print("=== DAYZ ↔ DISCORD RELAY ===")
print(f"RCON:     {RCON_HOST or 'BRAK'}:{RCON_PORT}  hasło: {'tak' if RCON_PASSWORD else 'NIE'}")
print(f"FTP:      {FTP_HOST or 'BRAK'}  user: {FTP_USER or 'BRAK'}  dir: {FTP_DIR}")
print(f"Discord:  channel {DISCORD_CHANNEL_ID}   token: {'obecny' if DISCORD_TOKEN else 'BRAK'}")
print(f"Interwał: {CHECK_INTERVAL} s")
print("=" * 60)

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
            print("RCON → brak hosta lub hasła → pomijam")
            return False

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
                    print("[RCON] POŁĄCZONO ✓")
                    return True
                else:
                    print("[RCON] Nieudane logowanie")
            except Exception as e:
                print(f"[RCON] Błąd (próba {attempt}): {e}")
            self._close_socket()
            await asyncio.sleep(4)
        print("[RCON] Wszystkie próby nieudane")
        return False

    def _build_packet(self, pkt_type: int, data: bytes) -> bytes:
        self.sequence += 1
        body = struct.pack('<I', self.sequence) + bytes([pkt_type]) + data
        header = struct.pack('<I', len(body))
        return header + body

    def _receive(self) -> bytes:
        try:
            header = self.sock.recv(4)
            if len(header) != 4:
                return b''
            size = struct.unpack('<I', header)[0]
            return self.sock.recv(size)
        except:
            return b''

    async def send_command(self, command: str) -> bool:
        if not self.connected or not self.sock:
            print("[RCON] Brak połączenia")
            return False
        try:
            cmd_bytes = command.encode('utf-8', errors='replace') + b'\x00'
            packet = self._build_packet(1, cmd_bytes)
            self.sock.send(packet)
            resp = self._receive()
            if resp:
                try:
                    seq = struct.unpack('<I', resp[0:4])[0]
                    msg = resp[9:].decode('utf-8', errors='replace').rstrip('\x00').strip()
                    print(f"[RCON] Odpowiedź seq={seq}: {msg}")
                except:
                    pass
            print(f"[RCON] Wysłano: {command}")
            return True
        except Exception as e:
            print(f"[RCON] Błąd wysyłania: {e}")
            self.connected = False
            self._close_socket()
            return False

    def _close_socket(self):
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None

    async def close(self):
        self._close_socket()
        self.connected = False


# ────────────────────────────────────────────────
# FTP Log Watcher – tylko .ADM
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
                print(f"[STATE] Wczytano: {self.last_file} | {self.last_line_count} linii")
            except:
                print("[STATE] Błąd wczytywania stanu")

    def _save_state(self):
        try:
            with open(STATE_FILE, 'wb') as f:
                pickle.dump({'file': self.last_file, 'lines': self.last_line_count}, f)
            print(f"[STATE] Zapisano: {self.last_file} | {self.last_line_count} linii")
        except:
            print("[STATE] Błąd zapisu stanu")

    def _ftp_connect(self):
        ftp = FTP()
        ftp.connect(FTP_HOST, FTP_PORT, timeout=10)
        ftp.login(FTP_USER, FTP_PASS)
        if FTP_DIR and FTP_DIR != '/':
            ftp.cwd(FTP_DIR)
        return ftp

    async def get_new_lines(self):
        if not all([FTP_HOST, FTP_USER, FTP_PASS]):
            print("[FTP] Brak danych logowania")
            return []

        try:
            ftp = self._ftp_connect()
            files = []
            ftp.retrlines('LIST', files.append)
            ftp.quit()

            pattern = re.compile(r'^DayZServer_x64_.*\.adm$', re.IGNORECASE)
            candidates = [line.split()[-1] for line in files if pattern.match(line.split()[-1])]

            if not candidates:
                print("[FTP] Brak plików .ADM")
                return []

            latest = max(candidates)   # najnowszy wg nazwy (data w nazwie)
            print(f"[FTP] Najnowszy: {latest}")

            if latest != self.last_file:
                print(f"[FTP] Nowy plik logów: {latest}")
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
            print(f"[FTP] {latest}: {len(lines)} linii | nowych: {len(new_lines)}")

            self.last_line_count = len(lines)
            self._save_state()

            return [line.strip() for line in new_lines if line.strip()]

        except Exception as e:
            print(f"[FTP] Błąd: {type(e).__name__}: {e}")
            return []

    async def run(self, callback):
        print("[Watcher] Start monitorowania .ADM...")
        while True:
            lines = await self.get_new_lines()
            for line in lines:
                # Poprawiony regex – pasuje do Twojego przykładu
                m = re.match(
                    r'^(\d{2}:\d{2}:\d{2})\s*\|\s*'
                    r'\[Chat\s*-\s*(\w+)\]\s*'
                    r'\("([^"]+)"\s*\([^)]+\)\)\s*:\s*(.+)$',
                    line, re.IGNORECASE
                )
                if m:
                    time_str, channel_type, nick, msg = m.groups()
                    formatted = f"[{time_str}] **{nick}** ({channel_type}): {msg.strip()}"
                    print(f"[CHAT → Discord] {formatted}")
                    await callback(formatted)
                # elif "Chat" in line:
                #     print(f"[CHAT MISS] {line[:120]}")   # odkomentuj do debugu

            await asyncio.sleep(CHECK_INTERVAL)


# ────────────────────────────────────────────────
# Discord Bot
# ────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)

rcon = BattleEyeRcon()
watcher = FTPLogWatcher()

async def send_to_discord(message: str):
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel:
        try:
            await channel.send(message[:1990])
            print(f"[→ Discord] {message[:80]}{'...' if len(message)>80 else ''}")
        except Exception as e:
            print(f"[Discord send error] {e}")

@bot.event
async def on_ready():
    print(f"[Discord] Zalogowano jako {bot.user}")
    await rcon.connect()
    asyncio.create_task(watcher.run(send_to_discord))

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.channel.id != DISCORD_CHANNEL_ID:
        return await bot.process_commands(message)

    content = message.clean_content.strip()
    if not content:
        return

    safe_msg = content.replace('"', "'").replace('\n', ' ').strip()
    dayz_msg = f"{message.author.display_name}: {safe_msg}"

    print(f"[Discord → DayZ] {dayz_msg}")

    if rcon.connected:
        success = await rcon.send_command(f'say -1 {dayz_msg}')
        if not success:
            await message.reply("⚠ Nie udało się wysłać do gry", delete_after=8)
    else:
        await message.reply("⚠ RCON niepodłączony", delete_after=8)

    await bot.process_commands(message)


# ────────────────────────────────────────────────
# Flask – keep-alive na Render
# ────────────────────────────────────────────────
app = Flask(__name__)

@app.route('/')
def home():
    return "DayZ ↔ Discord bridge działa"

@app.route('/health')
def health():
    return "OK", 200

def run_flask():
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

Thread(target=run_flask, daemon=True).start()


# ────────────────────────────────────────────────
# Start
# ────────────────────────────────────────────────
async def main():
    try:
        await bot.start(DISCORD_TOKEN)
    except KeyboardInterrupt:
        pass
    finally:
        await rcon.close()
        await bot.close()

if __name__ == "__main__":
    asyncio.run(main())
