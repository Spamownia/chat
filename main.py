import os
import asyncio
import discord
from discord.ext import commands
import socket
import struct
import hashlib
import re

# Environment variables
RCON_HOST = os.getenv('RCON_HOST')  # e.g., 'your.server.ip'
RCON_PORT = int(os.getenv('RCON_PORT', '2302'))  # Default BattlEye RCON port for DayZ
RCON_PASSWORD = os.getenv('RCON_PASSWORD')
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
DISCORD_CHANNEL_ID = int(os.getenv('DISCORD_CHANNEL_ID'))  # The Discord channel ID for chat relay
DISCORD_GUILD_ID = int(os.getenv('DISCORD_GUILD_ID'))  # Optional, if needed for guild-specific
BOT_PREFIX = os.getenv('BOT_PREFIX', '!')  # Prefix for Discord commands if needed

# BattlEye RCON protocol constants
BE_HEADER = b'\xFF'
BE_LOGIN = b'\x00'
BE_COMMAND = b'\x01'
BE_SERVER_MESSAGE = b'\x02'

class RCONClient:
    def __init__(self, host, port, password):
        self.host = host
        self.port = port
        self.password = password
        self.sock = None
        self.connected = False
        self.packet_counter = 0
        self.loop = asyncio.get_event_loop()
        self.receive_task = None

    async def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        try:
            self.sock.connect((self.host, self.port))
            await self.login()
            self.connected = True
            self.receive_task = self.loop.create_task(self.receive_logs())
            print("RCON connected successfully.")
        except Exception as e:
            print(f"Failed to connect to RCON: {e}")
            self.connected = False

    async def login(self):
        login_packet = self.build_packet(BE_LOGIN + hashlib.md5(self.password.encode()).digest())
        self.sock.send(login_packet)
        response = self.sock.recv(1024)
        if response[7] != 1:  # Login failed if not 0x01
            raise Exception("RCON login failed.")

    def build_packet(self, data):
        self.packet_counter = (self.packet_counter + 1) % 256
        packet = BE_HEADER + struct.pack('<B', self.packet_counter) + data
        crc = struct.pack('<I', binascii.crc32(packet) & 0xffffffff)
        return b'BE' + crc + packet

    async def send_command(self, command):
        if not self.connected:
            return
        cmd_packet = self.build_packet(BE_COMMAND + command.encode())
        self.sock.send(cmd_packet)

    async def receive_logs(self):
        buffer = b''
        while self.connected:
            try:
                data = await self.loop.run_in_executor(None, self.sock.recv, 4096)
                if not data:
                    print("RCON connection closed.")
                    self.connected = False
                    break
                buffer += data
                while len(buffer) >= 9:  # Minimum packet size
                    if buffer[:2] != b'BE':
                        print("Invalid packet header.")
                        buffer = buffer[2:]
                        continue
                    crc = buffer[2:6]
                    packet = buffer[6:]
                    calculated_crc = struct.pack('<I', binascii.crc32(packet) & 0xffffffff)
                    if crc != calculated_crc:
                        print("CRC mismatch.")
                        buffer = buffer[1:]
                        continue
                    header = packet[0]
                    if header == 0xFF and packet[1] == BE_SERVER_MESSAGE[0]:
                        # Server message (logs)
                        msg_len = len(packet) - 3  # seq + type + message
                        message = packet[3:3+msg_len].decode(errors='ignore')
                        await self.process_log(message)
                    # Consume the packet
                    packet_size = 6 + len(packet)  # BE + CRC + packet
                    buffer = buffer[packet_size:]
            except Exception as e:
                print(f"Error receiving logs: {e}")
                self.connected = False
                break

    async def process_log(self, log_line):
        # Parse DayZ chat logs (example format: "PlayerName: message")
        chat_match = re.match(r'(\d{2}:\d{2}:\d{2}) \| Chat\("([^"]+)"\): (.*)', log_line)
        if chat_match:
            time, player, message = chat_match.groups()
            discord_message = f"[{time}] {player}: {message}"
            await send_to_discord(discord_message)

    async def disconnect(self):
        if self.sock:
            self.sock.close()
        self.connected = False
        if self.receive_task:
            self.receive_task.cancel()

# Discord bot setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)
rcon = RCONClient(RCON_HOST, RCON_PORT, RCON_PASSWORD)

async def send_to_discord(message):
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel:
        await channel.send(message)

@bot.event
async def on_ready():
    print(f"Discord bot logged in as {bot.user}")
    await rcon.connect()

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

# Run the bot
async def main():
    try:
        await bot.start(DISCORD_TOKEN)
    finally:
        await rcon.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
