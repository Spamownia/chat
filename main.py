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
RCON_HOST = os.getenv('RCON_HOST')
RCON_PORT = int(os.getenv('RCON_PORT', '3705'))
RCON_PASSWORD = os.getenv('RCON_PASSWORD')
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
DISCORD_CHANNEL_ID = int(os.getenv('DISCORD_CHANNEL_ID'))
BOT_PREFIX = os.getenv('BOT_PREFIX', '!')
FTP_HOST = os.getenv('FTP_HOST')
FTP_PORT = int(os.getenv('FTP_PORT', '21'))
FTP_USER = os.getenv('FTP_USER')
FTP_PASS = os.getenv('FTP_PASS')
FTP_DIR = os.getenv('FTP_DIR', '/')
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '10')) # sekundy
STATE_FILE = 'last_log_state.pkl'
print("=== START BOTA ===")
print(f"RCON: {RCON_HOST or 'BRAK'}:{RCON_PORT} hasło: {'tak' if RCON_PASSWORD else 'NIE'}")
print(f"FTP: {FTP_HOST or 'BRAK'} user: {FTP_USER or 'BRAK'} katalog: {FTP_DIR}")
print(f"Discord channel ID: {DISCORD_CHANNEL_ID}")
print(f"Sprawdzanie logów co: {CHECK_INTERVAL} s")
print("=" * 60)
# ────────────────────────────────────────────────
# BattleEye RCON z retry
# ────────────────────────────────────────────────
class BattleEyeRcon:
    def __init__(self):
        self.sock = None
        self.connected = False
        self.sequence = 0
    async def connect(self):
        if not RCON_HOST or not RCON_PASSWORD:
            print("RCON → brak hosta lub hasła → wyłączam")
            return False
        for attempt in range(1, 4):
            print(f"[RCON] Próba {attempt}/3 → {RCON_HOST}:{RCON_PORT}")
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(10)
                self.sock.connect((RCON_HOST, RCON_PORT))
                login_data = RCON_PASSWORD.encode('utf-8') + b'\x00'
                packet = self._build_packet(0, login_data)
                self.sock.send(packet)
                response = self._receive()
                if response and len(response) >= 9 and response[8] == 0x01:
                    self.connected = True
                    print("[RCON] POŁĄCZONO POMYŚLNIE ✓")
                    return True
                else:
                    print("[RCON] Nieudane logowanie – zła odpowiedź")
            except Exception as e:
                print(f"[RCON] Błąd połączenia (próba {attempt}): {e}")
            if self.sock:
                try:
                    self.sock.close()
                except:
                    pass
                self.sock = None
            await asyncio.sleep(5)
        print("[RCON] WSZYSTKIE PRÓBY NIEUDANE")
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
            print("[RCON] send_command → brak połączenia")
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
                    print("[RCON] Nie udało się odczytać odpowiedzi")
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
# FTP watcher – TYLKO DayZServer_x64_*.adm / *.RPT
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
                print(f"[STATE] Wczytano poprzedni stan: {self.last_file} | {self.last_line_count} linii")
            except:
                print("[STATE] Nie udało się wczytać poprzedniego stanu")
    def _save_state(self):
        try:
            with open(STATE_FILE, 'wb') as f:
                pickle.dump({'file': self.last_file, 'lines': self.last_line_count}, f)
            print(f"[STATE] Zapisano: {self.last_file} | {self.last_line_count} linii")
        except:
            print("[STATE] Błąd zapisu stanu")
    def _ftp_connect(self):
        ftp = FTP()
        ftp.connect(FTP_HOST, FTP_PORT, timeout=12)
        ftp.login(FTP_USER, FTP_PASS)
        if FTP_DIR and FTP_DIR != '/':
            ftp.cwd(FTP_DIR)
            print(f"[FTP] Przejście do katalogu: {FTP_DIR}")
        return ftp
    async def get_new_lines(self):
        if not all([FTP_HOST, FTP_USER, FTP_PASS]):
            print("[FTP] Brak danych logowania – pomijam")
            return []
        try:
            print("[FTP] Pobieranie listy plików...")
            ftp = self._ftp_connect()
            files = []
            ftp.retrlines('LIST', files.append)
            ftp.quit()
            # Tylko DayZServer_x64_*.adm / *.RPT (ignorujemy wielkość liter)
            pattern = re.compile(r'^DayZServer_x64_.*\.(adm|rpt)$', re.IGNORECASE)
            candidates = [line.split()[-1] for line in files if pattern.match(line.split()[-1])]
            print(f"[FTP] Znaleziono {len(candidates)} pasujących plików")
            if candidates:
                print(" " + "\n ".join(candidates[:6]) + (" ..." if len(candidates) > 6 else ""))
            if not candidates:
                print("[FTP] Brak plików DayZServer_x64_*.adm / *.RPT")
                return []
            # Najnowszy plik – sortowanie odwrotne alfabetyczne (data w nazwie)
            latest = sorted(candidates, reverse=True)[0]
            print(f"[FTP] Najnowszy plik: {latest}")
            if latest != self.last_file:
                print(f"[FTP] Przełączono na nowy plik: {latest}")
                self.last_file = latest
                self.last_line_count = 0
            # Pobieramy zawartość
            ftp = self._ftp_connect()
            buf = io.BytesIO()
            ftp.retrbinary(f'RETR {latest}', buf.write)
            ftp.quit()
            buf.seek(0)
            content = buf.read().decode('utf-8', errors='replace')
            lines = content.splitlines()
            print(f"[FTP] Plik {latest}: {len(lines)} linii ogółem, dotychczas przetworzono {self.last_line_count}")
            new_lines = lines[self.last_line_count:]
            print(f"[FTP] Nowych linii: {len(new_lines)}")
            if new_lines:
                print("[FTP] Pierwsze 2 nowe:")
                for line in new_lines[:2]:
                    print(f" {line}")
                print("[FTP] Ostatnie 2 nowe:")
                for line in new_lines[-2:]:
                    print(f" {line}")
            self.last_line_count = len(lines)
            self._save_state()
            return [line.strip() for line in new_lines if line.strip()]
        except Exception as e:
            print(f"[FTP] Błąd: {type(e).__name__}: {e}")
            return []
    async def run(self, callback):
        print("[Watcher] Start pętli sprawdzania logów...")
        while True:
            lines = await self.get_new_lines()
            detected = 0
            for line in lines:
                # Poprawiony regex na format z logu: 14:04:26 | [Chat - Global]("Anu"(id=...)): wiadomość
                m = re.match(
                    r'^(\d{2}:\d{2}:\d{2})\s*\|\s*\[Chat\s*-\s*(\w+)\]\s*\("([^"]+)"\s*\((id=[^)]+)\)\s*\):\s*(.+)$',
                    line, re.IGNORECASE
                )
                if m:
                    time, ch_type, nick, id_str, msg = m.groups()
                    ch_type = (ch_type or 'Global').strip()
                    formatted = f"[{time}] **{nick}** ({ch_type}): {msg.strip()}"
                    print(f"[CHAT] Wykryto → {formatted}")
                    await callback(formatted)
                    detected += 1
                elif "Chat" in line:
                    print(f"[CHAT MISS] Linia nie złapana: {line[:140]}")
            if detected > 0:
                print(f"[Watcher] Przetworzono {detected} wiadomości czatu w tej turze")
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
            await channel.send(msg[:2000])
            print(f"[DISCORD] Wysyłanie: {msg[:80]}{'...' if len(msg)>80 else ''}")
        except Exception as e:
            print(f"[DISCORD] Błąd wysyłania: {e}")
@bot.event
async def on_ready():
    print(f"[DISCORD] Bot zalogowany jako {bot.user}")
    success = await rcon.connect()
    if success:
        print("[DISCORD] RCON gotowy do wysyłania")
    else:
        print("[DISCORD] RCON NIE DZIAŁA po starcie")
    asyncio.create_task(watcher.run(send_to_discord))
@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.channel.id != DISCORD_CHANNEL_ID:
        await bot.process_commands(message)
        return
    if message.content.startswith(BOT_PREFIX):
        await bot.process_commands(message)
        return
    content = message.clean_content.strip()
    if not content:
        return
    safe_msg = content.replace('"', "'").replace('\n', ' ').strip()
    dayz_msg = f"{message.author.display_name}: {safe_msg}"
    print(f"[DISCORD → DayZ] {dayz_msg}")
    if rcon.connected:
        success = await rcon.send_command(f"say -1 {dayz_msg}")
        if success:
            print("[DISCORD → DayZ] Wysłano pomyślnie")
        else:
            await message.reply("⚠ Błąd wysyłania do gry", delete_after=10)
    else:
        await message.reply("⚠ RCON nie jest podłączony", delete_after=10)
    await bot.process_commands(message)
@bot.event
async def on_disconnect():
    print("[DISCORD] Rozłączono")
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
