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
# Zmienne środowiskowe (Render / .env)
# ────────────────────────────────────────────────
RCON_HOST      = os.getenv('RCON_HOST')
RCON_PORT      = int(os.getenv('RCON_PORT', '2302'))
RCON_PASSWORD  = os.getenv('RCON_PASSWORD')
DISCORD_TOKEN  = os.getenv('DISCORD_TOKEN')
DISCORD_CHANNEL_ID = int(os.getenv('DISCORD_CHANNEL_ID'))
BOT_PREFIX     = os.getenv('BOT_PREFIX', '!')
FTP_HOST       = os.getenv('FTP_HOST')
FTP_PORT       = int(os.getenv('FTP_PORT', '21'))
FTP_USER       = os.getenv('FTP_USER')
FTP_PASS       = os.getenv('FTP_PASS')
FTP_DIR        = os.getenv('FTP_DIR', '/')
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '10'))   # sekundy

STATE_FILE = 'last_log_state.pkl'

# ────────────────────────────────────────────────
# Poprawny BattleEye RCON client
# ────────────────────────────────────────────────
class BattleEyeRcon:
    def __init__(self):
        self.sock = None
        self.connected = False
        self.sequence = 0

    async def connect(self):
        if not RCON_HOST or not RCON_PASSWORD:
            print("Brak RCON_HOST lub RCON_PASSWORD → RCON wyłączony")
            return False

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5)
            self.sock.connect((RCON_HOST, RCON_PORT))

            # Pakiet logowania (typ 0)
            login_data = RCON_PASSWORD.encode('utf-8') + b'\x00'
            packet = self._build_packet(0, login_data)
            self.sock.send(packet)

            response = self._receive()
            if not response or len(response) < 9 or response[8] != 0x01:
                raise Exception("Logowanie RCON nieudane – złe hasło?")

            self.connected = True
            print("BattleEye RCON → połączono pomyślnie")
            return True

        except Exception as e:
            print(f"RCON connect failed: {e}")
            self.connected = False
            self._close_socket()
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
            return False

        try:
            cmd_bytes = command.encode('utf-8', errors='replace') + b'\x00'
            packet = self._build_packet(1, cmd_bytes)   # typ 1 = komenda
            self.sock.send(packet)

            # Odczyt odpowiedzi (opcjonalny – pomaga w debugu)
            resp = self._receive()
            if resp:
                try:
                    seq = struct.unpack('<I', resp[0:4])[0]
                    msg = resp[9:].decode('utf-8', errors='replace').strip()
                    print(f"RCON reply seq={seq}: {msg}")
                except:
                    pass

            return True

        except Exception as e:
            print(f"RCON send error: {e}")
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
# Czytanie logów .adm z FTP
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
        ftp.connect(FTP_HOST, FTP_PORT, timeout=12)
        ftp.login(FTP_USER, FTP_PASS)
        if FTP_DIR and FTP_DIR != '/':
            ftp.cwd(FTP_DIR)
        return ftp

    async def get_new_lines(self):
        if not all([FTP_HOST, FTP_USER, FTP_PASS]):
            return []

        try:
            ftp = self._ftp_connect()
            files = []
            ftp.retrlines('LIST', files.append)
            adm_files = [line.split()[-1] for line in files if line.lower().endswith('.adm')]
            ftp.quit()

            if not adm_files:
                return []

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
            print(f"FTP error: {e}")
            return []

    async def run(self, callback):
        while True:
            lines = await self.get_new_lines()
            for line in lines:
                # Nowszy format (2024-2025+)
                m = re.match(r'^(\d{2}:\d{2}:\d{2}) \| \[Chat -(.*?)\]\("([^"]+)"(?:\s*\(id=[^\)]+\))?\): (.+)$', line)
                if m:
                    time, ch_type, nick, msg = m.groups()
                    ch_type = ch_type.strip()
                    await callback(f"[{time}] **{nick}** ({ch_type}): {msg}")
                    continue

                # Starszy / prostszy format
                m = re.match(r'^(\d{2}:\d{2}:\d{2}) \| Chat\("([^"]+)"(?:\s*\(id=[^\)]+\))?\): (.+)$', line)
                if m:
                    time, nick, msg = m.groups()
                    await callback(f"[{time}] **{nick}**: {msg}")

            await asyncio.sleep(CHECK_INTERVAL)


# ────────────────────────────────────────────────
# Discord Bot
# ────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)

rcon = BattleEyeRcon()
watcher = FTPLogWatcher()

async def send_to_discord(msg: str):
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel:
        try:
            await channel.send(msg[:2000])  # limit Discorda
        except:
            pass

@bot.event
async def on_ready():
    print(f"Bot zalogowany jako {bot.user}")
    if await rcon.connect():
        print("RCON gotowy do wysyłania")
    else:
        print("RCON NIE DZIAŁA – sprawdź hasło/port/host")
    asyncio.create_task(watcher.run(send_to_discord))


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel.id != DISCORD_CHANNEL_ID:
        await bot.process_commands(message)
        return

    # Pomijamy komendy bota (!help itp.)
    if message.content.startswith(BOT_PREFIX):
        await bot.process_commands(message)
        return

    content = message.clean_content.strip()
    if not content:
        return

    # Oczyszczamy wiadomość dla DayZ
    safe_msg = content.replace('"', "'").replace('\n', ' ').strip()
    dayz_msg = f"{message.author.display_name}: {safe_msg}"

    print(f"[Discord → DayZ] {dayz_msg}")

    if rcon.connected:
        success = await rcon.send_command(f"say -1 {dayz_msg}")
        if success:
            # await message.add_reaction("✅")   # opcjonalne
            pass
        else:
            await message.reply("⚠ Nie udało się wysłać do gry (RCON error)", delete_after=10)
    else:
        await message.reply("⚠ RCON nie jest podłączony", delete_after=10)

    await bot.process_commands(message)


@bot.event
async def on_disconnect():
    await rcon.close()


# ────────────────────────────────────────────────
# Flask keep-alive dla Render
# ────────────────────────────────────────────────
app = Flask(__name__)

@app.route('/')
def home():
    return "DayZ ↔ Discord relay running"

@app.route('/health')
def health():
    return "OK", 200

def run_flask():
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

Thread(target=run_flask, daemon=True).start()


# ────────────────────────────────────────────────
# Główna pętla
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
