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
# Zmienne środowiskowe
# ────────────────────────────────────────────────
RCON_HOST      = os.getenv('RCON_HOST')
RCON_PORT      = int(os.getenv('RCON_PORT', '3705'))          # ← Twój port 3705
RCON_PASSWORD  = os.getenv('RCON_PASSWORD')
DISCORD_TOKEN  = os.getenv('DISCORD_TOKEN')
DISCORD_CHANNEL_ID = int(os.getenv('DISCORD_CHANNEL_ID'))
BOT_PREFIX     = os.getenv('BOT_PREFIX', '!')
FTP_HOST       = os.getenv('FTP_HOST')
FTP_PORT       = int(os.getenv('FTP_PORT', '21'))
FTP_USER       = os.getenv('FTP_USER')
FTP_PASS       = os.getenv('FTP_PASS')
FTP_DIR        = os.getenv('FTP_DIR', '/')
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '10'))
STATE_FILE     = 'last_log_state.pkl'

print("START – zmienne środowiskowe:")
print(f"RCON: {RCON_HOST}:{RCON_PORT}  |  hasło: {'ustawione' if RCON_PASSWORD else 'BRAK'}")
print(f"FTP:  {FTP_HOST}:{FTP_PORT}  user={FTP_USER}  dir={FTP_DIR}")
print(f"Discord channel: {DISCORD_CHANNEL_ID}")
print("-" * 60)

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
            print("RCON → brak hosta lub hasła – wyłączam RCON")
            return False

        for attempt in range(1, 4):
            print(f"RCON connect próba {attempt}/3 → {RCON_HOST}:{RCON_PORT}")
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
                    print("✅ RCON POŁĄCZONO POMYŚLNIE")
                    return True
                else:
                    print("RCON login failed – zła odpowiedź lub hasło")
            except Exception as e:
                print(f"RCON connect error (próba {attempt}): {type(e).__name__} → {e}")

            if self.sock:
                self.sock.close()
                self.sock = None
            await asyncio.sleep(4)

        print("RCON → wszystkie próby nieudane")
        self.connected = False
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
        if not self.connected:
            print("send_command → RCON niepołączony")
            return False
        try:
            cmd_bytes = command.encode('utf-8', errors='replace') + b'\x00'
            packet = self._build_packet(1, cmd_bytes)
            self.sock.send(packet)
            resp = self._receive()
            if resp:
                try:
                    seq = struct.unpack('<I', resp[0:4])[0]
                    msg = resp[9:].decode('utf-8', errors='replace').rstrip('\x00')
                    print(f"RCON reply seq={seq}: {msg}")
                except:
                    print("RCON reply → nie udało się zdekodować odpowiedzi")
            print(f"RCON → wysłano: {command}")
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
# FTP watcher – szuka .adm i .log + dużo debugu
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
                print(f"Stan wczytany: plik={self.last_file}, linie={self.last_line_count}")
            except Exception as e:
                print(f"Błąd wczytywania stanu: {e}")

    def _save_state(self):
        try:
            with open(STATE_FILE, 'wb') as f:
                pickle.dump({'file': self.last_file, 'lines': self.last_line_count}, f)
            print(f"Stan zapisany: {self.last_file} → {self.last_line_count} linii")
        except Exception as e:
            print(f"Błąd zapisu stanu: {e}")

    def _ftp_connect(self):
        ftp = FTP()
        ftp.connect(FTP_HOST, FTP_PORT, timeout=12)
        ftp.login(FTP_USER, FTP_PASS)
        if FTP_DIR and FTP_DIR != '/':
            ftp.cwd(FTP_DIR)
            print(f"FTP → zmieniono katalog na: {FTP_DIR}")
        return ftp

    async def get_new_lines(self):
        if not all([FTP_HOST, FTP_USER, FTP_PASS]):
            print("FTP → brak danych logowania – pomijam")
            return []

        try:
            print("FTP → pobieram listę plików...")
            ftp = self._ftp_connect()
            files = []
            ftp.retrlines('LIST', files.append)
            ftp.quit()

            # Szukamy .adm i .log (DayZServer*.log, *.log, *.adm)
            candidates = [f.split()[-1] for f in files if f.lower().endswith(('.adm', '.log'))]
            print(f"FTP → znaleziono {len(candidates)} pasujących plików: {candidates[:6]}{'...' if len(candidates)>6 else ''}")

            if not candidates:
                print("FTP → brak plików .adm ani .log!")
                return []

            # Najnowszy – sortujemy alfabetycznie (DayZ dodaje datę w nazwie → działa)
            latest = sorted(candidates, reverse=True)[0]
            print(f"FTP → wybrano najnowszy plik: {latest}")

            if latest != self.last_file:
                print(f"NOWY PLIK LOGÓW: {latest} (poprzedni: {self.last_file})")
                self.last_file = latest
                self.last_line_count = 0

            # Pobieramy zawartość
            ftp = self._ftp_connect()
            buf = io.BytesIO()
            ftp.retrbinary(f'RETR {latest}', buf.write)
            ftp.quit()

            buf.seek(0)
            try:
                content = buf.read().decode('utf-8', errors='replace')
            except Exception as e:
                print(f"FTP decode error: {e}")
                return []

            lines = content.splitlines()
            print(f"Plik {latest}: {len(lines)} linii ogółem, dotychczas przeczytano {self.last_line_count}")

            new_lines = lines[self.last_line_count:]
            print(f"Nowe linie do przetworzenia: {len(new_lines)}")

            if new_lines:
                print("Ostatnie 3 nowe linie (lub mniej):")
                for ln in new_lines[-3:]:
                    print(f"  {ln}")

            self.last_line_count = len(lines)
            self._save_state()

            return [line.strip() for line in new_lines if line.strip()]

        except Exception as e:
            print(f"FTP ogólny błąd: {type(e).__name__} → {e}")
            return []

    async def run(self, callback):
        print("FTP watcher → start pętli...")
        while True:
            lines = await self.get_new_lines()
            for line in lines:
                print(f"Przetwarzam linię: {line[:120]}{'...' if len(line)>120 else ''}")

                # Bardzo elastyczny regex na czat (różne formaty DayZ + mody)
                m = re.match(
                    r'^(?P<time>\d{2}:\d{2}:\d{2})\s*[|]\s*'
                    r'(?:Chat\s*(?:-\s*(?P<chtype>[^\s"]+))?\s*)?'
                    r'(?:"(?P<nick>[^"]+)"(?:\s*\([^)]+\))?)\s*:\s*(?P<msg>.+)$',
                    line,
                    re.IGNORECASE
                )

                if m:
                    d = m.groupdict()
                    time = d['time']
                    nick = d['nick']
                    msg = d['msg'].strip()
                    chtype = d['chtype'] or 'Global'
                    print(f"→ wykryto czat: {nick} ({chtype}): {msg}")
                    await callback(f"[{time}] **{nick}** ({chtype}): {msg}")
                else:
                    # print("→ linia nie pasuje do regexu czatu")
                    pass

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
            print(f"Discord → wysłano: {msg[:80]}...")
        except Exception as e:
            print(f"Błąd wysyłania na Discord: {e}")

@bot.event
async def on_ready():
    print(f"Bot zalogowany jako {bot.user} (ID: {bot.user.id})")
    if await rcon.connect():
        print("RCON → gotowy do wysyłania komend")
    else:
        print("RCON → NIE DZIAŁA po starcie")
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
    print(f"[Discord → DayZ] {dayz_msg}")

    if rcon.connected:
        success = await rcon.send_command(f"say -1 {dayz_msg}")
        if success:
            print("→ wiadomość wysłana do gry")
        else:
            await message.reply("⚠ Błąd wysyłania do gry (RCON)", delete_after=8)
    else:
        await message.reply("⚠ RCON nie jest podłączony", delete_after=8)

    await bot.process_commands(message)

@bot.event
async def on_disconnect():
    print("Discord → rozłączono")
    await rcon.close()


# Flask keep-alive
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


# Główna pętla
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
