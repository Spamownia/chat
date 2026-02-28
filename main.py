import os
import asyncio
import discord
from discord.ext import commands
from ftplib import FTP
import io
import re
import hashlib
import pickle
import socket

# Environment variables
RCON_HOST       = os.getenv('RCON_HOST')
RCON_PORT       = int(os.getenv('RCON_PORT', '2302'))
RCON_PASSWORD   = os.getenv('RCON_PASSWORD')

DISCORD_TOKEN    = os.getenv('DISCORD_TOKEN')
DISCORD_CHANNEL_ID = int(os.getenv('DISCORD_CHANNEL_ID'))
BOT_PREFIX       = os.getenv('BOT_PREFIX', '!')

FTP_HOST        = os.getenv('FTP_HOST')
FTP_PORT        = int(os.getenv('FTP_PORT', '21'))          # ← DODANE
FTP_USER        = os.getenv('FTP_USER')
FTP_PASS        = os.getenv('FTP_PASS')
FTP_DIR         = os.getenv('FTP_DIR', '/adm_logs/')        # lub '/'
CHECK_INTERVAL  = int(os.getenv('CHECK_INTERVAL', '10'))    # sekundy

STATE_FILE = 'log_state.pkl'

# ────────────────────────────────────────────────
# RCON (tylko wysyłanie – bez odbierania logów)
# ────────────────────────────────────────────────

class RCONClient:
    def __init__(self, host, port, password):
        self.host = host
        self.port = port
        self.password = password
        self.sock = None
        self.connected = False
        self.packet_counter = 0

    async def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        try:
            self.sock.connect((self.host, self.port))
            login_packet = self.build_packet(b'\x00' + hashlib.md5(self.password.encode()).digest())
            self.sock.send(login_packet)
            response = self.sock.recv(1024)
            if len(response) < 8 or response[7] != 1:
                raise Exception("RCON login failed")
            self.connected = True
            print("RCON connected")
        except Exception as e:
            print(f"RCON connect failed: {e}")
            self.connected = False

    def build_packet(self, data):
        import struct
        import binascii
        self.packet_counter = (self.packet_counter + 1) % 256
        packet = b'\xFF' + struct.pack('<B', self.packet_counter) + data
        crc = struct.pack('<I', binascii.crc32(packet) & 0xffffffff)
        return b'BE' + crc + packet

    async def send_command(self, command):
        if not self.connected:
            return
        cmd_packet = self.build_packet(b'\x01' + command.encode('utf-8', errors='ignore'))
        try:
            self.sock.send(cmd_packet)
        except:
            self.connected = False

    async def disconnect(self):
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
        self.connected = False

# ────────────────────────────────────────────────
# Czytanie logów z FTP
# ────────────────────────────────────────────────

class FTPLogReader:
    def __init__(self):
        self.ftp = None
        self.last_file = None
        self.last_offset = 0
        self.load_state()

    def load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'rb') as f:
                    state = pickle.load(f)
                    self.last_file = state.get('last_file')
                    self.last_offset = state.get('last_offset', 0)
                print(f"Loaded state: {self.last_file} @ {self.last_offset}")
            except:
                print("Cannot load state file – starting fresh")

    def save_state(self):
        try:
            with open(STATE_FILE, 'wb') as f:
                pickle.dump({'last_file': self.last_file, 'last_offset': self.last_offset}, f)
        except Exception as e:
            print(f"Cannot save state: {e}")

    def _connect(self):
        self.ftp = FTP()
        self.ftp.connect(FTP_HOST, FTP_PORT, timeout=10)
        self.ftp.login(FTP_USER, FTP_PASS)
        self.ftp.cwd(FTP_DIR)

    def _disconnect(self):
        try:
            if self.ftp:
                self.ftp.quit()
        except:
            pass

    async def get_latest_log_file(self):
        self._connect()
        try:
            lines = []
            self.ftp.retrlines('LIST', lines.append)
            adm_files = []
            for line in lines:
                parts = line.split()
                if len(parts) >= 9 and parts[-1].lower().endswith('.adm'):
                    adm_files.append(parts[-1])
            if not adm_files:
                return None
            adm_files.sort(reverse=True)  # najnowszy na górze (zakładając datę w nazwie)
            return adm_files[0]
        finally:
            self._disconnect()

    async def read_new_logs(self):
        current_file = await self.get_latest_log_file()
        if not current_file:
            print("Brak plików .ADM na FTP")
            return []

        if current_file != self.last_file:
            print(f"Przełączono na nowy plik logów: {current_file}")
            self.last_file = current_file
            self.last_offset = 0

        self._connect()
        try:
            buffer = io.BytesIO()
            self.ftp.retrbinary(f'RETR {current_file}', buffer.write)
            buffer.seek(0)
            content = buffer.read().decode('utf-8', errors='replace')
            lines = content.splitlines(True)  # zachowujemy \n

            new_lines = lines[self.last_offset:]
            self.last_offset = len(lines)
            self.save_state()

            return [line.rstrip('\r\n') for line in new_lines if line.strip()]
        except Exception as e:
            print(f"Błąd pobierania logu {current_file}: {e}")
            return []
        finally:
            self._disconnect()

    async def process_logs(self):
        while True:
            try:
                new_lines = await self.read_new_logs()
                for line in new_lines:
                    # Typowy format chatu w DayZ (możesz dostosować regex)
                    m = re.match(r'^(\d{2}:\d{2}:\d{2}) \| Chat\("([^"]+)"\): (.+)$', line.strip())
                    if m:
                        czas, gracz, wiadomosc = m.groups()
                        msg = f"[{czas}] **{gracz}**: {wiadomosc}"
                        await send_to_discord(msg)
            except Exception as e:
                print(f"Błąd w pętli logów: {e}")
            await asyncio.sleep(CHECK_INTERVAL)

# ────────────────────────────────────────────────
# Discord
# ────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)

rcon = RCONClient(RCON_HOST, RCON_PORT, RCON_PASSWORD)
ftp_reader = FTPLogReader()

async def send_to_discord(message: str):
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel:
        try:
            await channel.send(message)
        except:
            print("Nie udało się wysłać wiadomości na Discord")

@bot.event
async def on_ready():
    print(f"Zalogowano jako {bot.user}")
    await rcon.connect()
    asyncio.create_task(ftp_reader.process_logs())

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.channel.id == DISCORD_CHANNEL_ID:
        content = message.content.strip()
        if content:
            dayz_msg = f"{message.author.display_name}: {content}"
            await rcon.send_command(f'say -1 "{dayz_msg}"')
    await bot.process_commands(message)

@bot.event
async def on_disconnect():
    await rcon.disconnect()

# ────────────────────────────────────────────────

async def main():
    try:
        await bot.start(DISCORD_TOKEN)
    finally:
        await rcon.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
