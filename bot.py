import discord
from discord.ext import commands
import asyncio
import os
from dotenv import load_dotenv
from faster_whisper import WhisperModel
import time
from discord import opus  # <--- Importă modulul opus
import ctypes.util
import aiohttp

# --- FIX PENTRU WSL/LINUX: ÎNCĂRCARE MANUALĂ OPUS ---
if not opus.is_loaded():
    # Caută biblioteca în sistem
    opus_path = ctypes.util.find_library('opus')
    if opus_path:
        print(f"📚 Am găsit libopus la: {opus_path}")
        opus.load_opus(opus_path)
    else:
        # Fallback dacă find_library nu o găsește (uzual în WSL Ubuntu)
        try:
            opus.load_opus("libopus.so.0")
            print("📚 Am încărcat forțat libopus.so.0")
        except Exception as e:
            print("❌ CRITIC: Nu pot încărca biblioteca Opus! Audio nu va merge.")
            print(f"Eroare: {e}")

# ---------------- CONFIGURARE ----------------
# Încărcăm variabilele
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN', '1234')
BOT_MODE = os.getenv('BOT_MODE', 'REACTIVE').upper()
TOXICITY_API_URL = os.getenv('TOXICITY_API_URL', 'http://127.0.0.1:8000/check')

print(f"BOT RUNS IN {BOT_MODE} MODE")
print(f'Toxic api checker runs at {TOXICITY_API_URL}')

# Încărcăm modelul Whisper O SINGURĂ DATĂ (la start)
print("⏳ Se încarcă modelul Whisper (poate dura 10-20 secunde)...")
# Folosim 'base.en' pentru viteză pe CPU
MODEL = WhisperModel("base.en", device="cpu", compute_type="int8")
print("✅ Model Whisper încărcat!")

# Configurare Bot Discord
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Variabilă globală să controlăm bucla de înregistrare
is_recording = False
current_voice_client = None

# ---------------- FUNCȚII DE PROCESARE ----------------

def transcribe_audio(audio_file_path):
    """Primește calea către un wav și returnează textul."""
    try:
        segments, _ = MODEL.transcribe(audio_file_path, beam_size=5)
        text = " ".join([segment.text for segment in segments])
        return text.strip()
    except Exception as e:
        print(f"Eroare Whisper: {e}")
        return ""

async def check_toxicity(text):
    """Întreabă API-ul dacă textul e toxic."""
    async with aiohttp.ClientSession() as session:
        try:
            payload = {"text": text, "threshold": 0.5}
            async with session.post(TOXICITY_API_URL, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("toxic_labels", [])
        except Exception as e:
            print(f"⚠️ Nu pot contacta API-ul de toxicitate: {e}")
            return []
    return []

async def processing_callback(sink, channel):
    for user_id, audio in sink.audio_data.items():
        if audio:
            # 1. Salvare Audio
            filename = f"user_{user_id}_{int(asyncio.get_event_loop().time())}.wav"
            with open(filename, "wb") as f:
                f.write(audio.file.read())

            # 2. Transcriere
            text = await asyncio.to_thread(transcribe_audio, filename)
            
            if not text:
                os.remove(filename)
                continue

            print(f"🗣️ User {user_id}: {text}")
            
            # 3. Verificare Toxicitate
            toxic_labels = await check_toxicity(text)
            is_toxic = len(toxic_labels) > 0

            # --- LOGICA DE DISERTAȚIE ---
            
            if BOT_MODE == "REACTIVE":
                # Modul CLASIC: Se aude tot, pedepsim după.
                os.remove(filename) # Nu ne mai trebuie sunetul
                if is_toxic:
                    reasons = ", ".join([l['label'] for l in toxic_labels])
                    await channel.send(f"🚨 **ALERTA TOXICITATE!** <@{user_id}>: \"{text}\"\nMotiv: `{reasons}`")
                else:
                    await channel.send(f"✅ <@{user_id}>: {text}")

            elif BOT_MODE == "PREVENTIVE":
                # Modul RELAY: Tu vorbești -> Bot Ascultă -> Bot Redă (dacă e ok)
                if is_toxic:
                    print(f"🛑 BLOCAT mesaj toxic de la {user_id}")
                    await channel.send(f"🛡️ **Mesaj Blocat (Preventive):** <@{user_id}> a încercat să fie toxic!")
                    os.remove(filename) # Ștergem dovada, nimeni nu aude nimic
                else:
                    print(f"✅ Mesaj OK. Redare către ceilalți...")
                    if current_voice_client:
                        # Redăm sunetul original înapoi
                        await play_audio_back(current_voice_client, filename)
                    # Nu ștergem imediat fișierul că încă se redă (cleanup-ul e mai complex aici, dar pt demo e ok)

async def play_audio_back(voice_client, filename):
    """Redă fișierul audio înapoi în canal (Pentru modul Preventive)."""
    while voice_client.is_playing():
        await asyncio.sleep(0.1)
    # FFmpegPCMAudio redă fișierul salvat pe disc
    voice_client.play(discord.FFmpegPCMAudio(filename))
    
async def record_loop(ctx):
    """Bucla infinită care înregistrează în bucăți de 5 secunde."""
    global is_recording, current_voice_client
    
    while is_recording and current_voice_client and current_voice_client.is_connected():
        # 1. Pregătim Sink-ul (cel care prinde audio)
        # Filters={'time': 0} înseamnă că nu tăiem liniștea, luăm tot
        sink = discord.sinks.WaveSink()
        
        # 2. Pornim înregistrarea
        current_voice_client.start_recording(
            sink, 
            processing_callback, # Funcția care se apelează la stop
            ctx.channel # Argument extra trimis către callback
        )
        
        # 3. Așteptăm X secunde (fereastra de timp)
        await asyncio.sleep(4) 
        
        # 4. Oprim înregistrarea (Asta declanșează processing_callback)
        current_voice_client.stop_recording()
        
        # Așteptăm puțin să se proceseze callback-ul înainte de a relua
        # (Nu e obligatoriu, dar ajută la stabilitate)
        await asyncio.sleep(0.5)

# ---------------- COMENZI BOT ----------------

@bot.event
async def on_ready():
    print(f'✅ Bot conectat ca: {bot.user}')

@bot.command()
async def join(ctx):
    global is_recording, current_voice_client
    
    if ctx.author.voice is None:
        await ctx.send("❌ Intră întâi într-un canal de voce!")
        return

    channel = ctx.author.voice.channel
    
    # Conectare
    if ctx.voice_client is not None:
        await ctx.voice_client.move_to(channel)
        current_voice_client = ctx.voice_client
    else:
        current_voice_client = await channel.connect()

    await ctx.send(f"🔊 Conectat la **{channel.name}**. Încep ascultarea...")
    
    # Pornim bucla de înregistrare
    is_recording = True
    bot.loop.create_task(record_loop(ctx))

@bot.command()
async def leave(ctx):
    global is_recording
    is_recording = False # Oprim bucla
    
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("👋 Deconectat.")

@bot.command()
async def ping(ctx):
    await ctx.send("pong")

if __name__ == "__main__":
    if not TOKEN:
        print("❌ Nu am găsit token-ul!")
    else:
        bot.run(TOKEN)