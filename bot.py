import discord
from discord.ext import commands
import asyncio
import os
import aiohttp
import ctypes.util
from dotenv import load_dotenv
from faster_whisper import WhisperModel
from discord import opus

# --- FIX PENTRU WSL/LINUX ---
if not opus.is_loaded():
    try:
        opus_path = ctypes.util.find_library('opus')
        if opus_path:
            opus.load_opus(opus_path)
        else:
            opus.load_opus("libopus.so.0") # Fallback standard
    except Exception as e:
        print("❌ EROARE OPUS: Nu pot încărca biblioteca audio sistem!")

# ---------------- CONFIGURARE ----------------
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
BOT_MODE = os.getenv('BOT_MODE', 'PREVENTIVE').upper() # Default pe Preventive ca să testăm nebunia
TOXICITY_API_URL = os.getenv('TOXICITY_API_URL', 'http://127.0.0.1:8000/check')

print(f"🤖 BOT PORNIT ÎN MODUL: [ {BOT_MODE} ]")
print(f"🔗 API Check: {TOXICITY_API_URL}")

print("⏳ Se încarcă Whisper...")
MODEL = WhisperModel("base.en", device="cpu", compute_type="int8")
print("✅ Whisper Gata!")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

is_recording = False
current_voice_client = None

# ---------------- FUNCȚII DE LOGICĂ ----------------

def transcribe_audio(filename):
    """Procesare CPU Whisper."""
    try:
        segments, _ = MODEL.transcribe(filename, beam_size=5)
        return " ".join([s.text for s in segments]).strip()
    except Exception as e:
        print(f"Err Whisper: {e}")
        return ""

async def check_toxicity(text):
    """Apel HTTP către microserviciu."""
    async with aiohttp.ClientSession() as session:
        try:
            payload = {"text": text, "threshold": 0.5}
            async with session.post(TOXICITY_API_URL, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("toxic_labels", [])
        except Exception as e:
            print(f"⚠️ Eroare API: {e}")
    return []

async def play_audio_back(voice_client, filename):
    """Redă fișierul audio doar dacă utilizatorul nu a fost toxic."""
    # Așteptăm să fie liber canalul de ieșire
    while voice_client.is_playing():
        await asyncio.sleep(0.1)
    
    # Specificăm calea către ffmpeg explicit dacă e nevoie, altfel default
    # Pe Linux/WSL de obicei merge default dacă e instalat cu apt
    voice_client.play(discord.FFmpegPCMAudio(filename))
    
    # Așteptăm să termine redarea ca să putem șterge fișierul
    while voice_client.is_playing():
        await asyncio.sleep(0.5)

async def processing_callback(sink, channel):
    """Creierul care decide cine se aude și cine nu."""
    for user_id, audio in sink.audio_data.items():
        if audio:
            # 1. Nume unic fisier
            filename = f"user_{user_id}_{int(asyncio.get_event_loop().time())}.wav"
            with open(filename, "wb") as f:
                f.write(audio.file.read())

            # 2. Transcriere
            text = await asyncio.to_thread(transcribe_audio, filename)
            
            if not text:
                os.remove(filename) # Liniște = Gunoi
                continue

            print(f"🗣️ User {user_id}: {text}")
            
            # 3. Verificare Toxicitate
            toxic_labels = await check_toxicity(text)
            is_toxic = len(toxic_labels) > 0

            # ---------------- MOD REACTIVE (Simplu) ----------------
            if BOT_MODE == "REACTIVE":
                os.remove(filename) # Ștergem audio, s-a auzit deja live
                if is_toxic:
                    reasons = ", ".join([l['label'] for l in toxic_labels])
                    await channel.send(f"🚨 **ALERTA (Reactive):** <@{user_id}>: \"{text}\"\nMotiv: `{reasons}`")
                else:
                    await channel.send(f"✅ <@{user_id}>: {text}")

            # ---------------- MOD PREVENTIVE (Relay/Nebunia) ----------------
            elif BOT_MODE == "PREVENTIVE":
                if is_toxic:
                    # E TOXIC? -> NU REDĂM NIMIC.
                    print(f"🛑 BLOCAT mesaj toxic de la {user_id}")
                    await channel.send(f"🛡️ **Mesaj Blocat (Preventive):** <@{user_id}> a încercat să fie toxic!")
                    os.remove(filename) # Ștergem dovada
                else:
                    # E CUMINTE? -> REDĂM AUDIO.
                    print(f"✅ Mesaj OK. Redare către ceilalți...")
                    if current_voice_client and current_voice_client.is_connected():
                        await play_audio_back(current_voice_client, filename)
                        
                        # Curățenie după redare
                        try:
                            os.remove(filename)
                        except:
                            pass
                    else:
                        os.remove(filename)

async def record_loop(ctx):
    global is_recording, current_voice_client
    while is_recording and current_voice_client and current_voice_client.is_connected():
        sink = discord.sinks.WaveSink()
        # Ascultă 4 secunde (Aici se creează buffer-ul de întârziere)
        current_voice_client.start_recording(sink, processing_callback, ctx.channel)
        await asyncio.sleep(2.2) 
        current_voice_client.stop_recording()

# ---------------- COMENZI ----------------

@bot.event
async def on_ready():
    print(f'✅ Bot conectat: {bot.user}')

@bot.command()
async def join(ctx):
    global is_recording, current_voice_client
    if ctx.author.voice is None: return await ctx.send("❌ Intră în voce!")
    
    channel = ctx.author.voice.channel
    if ctx.voice_client: current_voice_client = ctx.voice_client
    else: current_voice_client = await channel.connect()

    await ctx.send(f"🎙️ **ToxicGuard Activat**\nMod: `{BOT_MODE}`\nCanal: `{channel.name}`")
    
    if BOT_MODE == "PREVENTIVE":
        await ctx.send(
            "⚠️ **INSTRUCȚIUNI MOD PREVENTIVE:**\n"
            "1. Dați **MUTE (Click Dreapta)** tuturor celorlalți participanți.\n"
            "2. Lăsați **DOAR BOTUL** cu sunet.\n"
            "3. Vorbiți normal. Botul vă va reda vocea doar dacă nu este toxică."
        )

    is_recording = True
    bot.loop.create_task(record_loop(ctx))

@bot.command()
async def leave(ctx):
    global is_recording
    is_recording = False
    if ctx.voice_client: await ctx.voice_client.disconnect()
    await ctx.send("👋")

if __name__ == "__main__":
    bot.run(TOKEN)