import os
import json
import asyncio
import aiohttp
import io
import time
import csv
import wave
from datetime import datetime
from typing import Dict, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydub import AudioSegment # <--- ADAUGĂ ASTA SUS LA IMPORTURI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

# --- IMPORT VOSK ---
from vosk import Model, KaldiRecognizer

app = FastAPI()

origins = [
    "http://localhost:5000",
    "https://unburned-unbargained-marsha.ngrok-free.dev" 
]

try:
    AudioSegment.converter_test = AudioSegment.converter
    print(f"✅ FFmpeg configurat la: {AudioSegment.converter}")
except:
    print("⚠️  ATENȚIE: FFmpeg s-ar putea să nu fie găsit! Audio nu va merge.")
# --- CONFIGURARE ---
TOXICITY_API_URL = "http://127.0.0.1:8000/check"
# IMPORTANT: Schimbăm numele fișierului de log pentru a nu suprascrie datele de la Whisper
CSV_FILE = "stats_vosk.csv" 
VOSK_MODEL_PATH = "model"  # Asigură-te că ai folderul 'model' aici

# Inițializăm CSV-ul dacă nu există
if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, mode='w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "user", "text", "toxic_labels", "stt_time", "ai_time", "latency_ms", "user_count"])

# --- INIȚIALIZARE VOSK ---
if not os.path.exists(VOSK_MODEL_PATH):
    print(f"❌ EROARE: Nu găsesc folderul '{VOSK_MODEL_PATH}'. Descarcă un model Vosk și dezarhivează-l aici!")
    exit(1)

print(f"🚀 Încărcare VOSK Model din '{VOSK_MODEL_PATH}'...")
vosk_model = Model(VOSK_MODEL_PATH)
print("✅ Vosk Gata!")

# --- LOGARE ÎN CSV ---
def log_interaction(username, text, toxic_labels, stt_time, ai_time, total_latency, user_count):
    try:
        with open(CSV_FILE, mode='a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # Formatăm etichetele toxice sau scriem SAFE
            # Verificăm structura răspunsului BERT (dacă e listă de dict-uri sau altceva)
            if toxic_labels and isinstance(toxic_labels, list):
                 labels_str = ";".join([l.get('label', 'TOXIC') for l in toxic_labels])
            elif toxic_labels: 
                 labels_str = "TOXIC"
            else:
                 labels_str = "SAFE"
                 
            writer.writerow([timestamp, username, text, labels_str, f"{stt_time:.2f}", f"{ai_time:.2f}", f"{total_latency:.2f}", user_count])
    except Exception as e:
        print(f"Eroare scriere CSV: {e}")

# --- MANAGER DE CONEXIUNI ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[WebSocket, str] = {}

    async def connect(self, websocket: WebSocket, username: str):
        #await websocket.accept()
        self.active_connections[websocket] = username
        await self.broadcast_user_list()
        await self.broadcast_system(f"🔵 {username} s-a conectat.")

    def disconnect(self, websocket: WebSocket):
        username = self.active_connections.get(websocket, "Unknown")
        if websocket in self.active_connections:
            del self.active_connections[websocket]
        return username

    async def broadcast_user_list(self):
        users_list = list(self.active_connections.values())
        for connection in self.active_connections:
            try: await connection.send_json({"type": "user_list", "users": users_list})
            except: pass

    async def broadcast_audio(self, audio_data: bytes, sender: WebSocket):
        # Trimitem audio la toți ceilalți
        for connection in self.active_connections:
            if connection != sender:
                try: await connection.send_bytes(audio_data)
                except: pass

    async def broadcast_system(self, message: str):
        for connection in self.active_connections:
            try: await connection.send_json({"type": "system", "message": message})
            except: pass
            
    async def send_json(self, websocket: WebSocket, data: dict):
        try: await websocket.send_json(data)
        except: pass

manager = ConnectionManager()

class LogOriginMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        print(f"📡 INCOMING REQUEST ORIGIN: {request.headers.get('origin')}")
        response = await call_next(request)
        return response

app.add_middleware(LogOriginMiddleware)

# --- VERIFICARE TOXICITATE (BERT) ---
async def check_toxicity(text):
    async with aiohttp.ClientSession() as session:
        try:
            payload = {"text": text, "threshold": 0.5}
            async with session.post(TOXICITY_API_URL, json=payload, timeout=2) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("toxic_labels", [])
        except: return []
    return []

# --- RUTE WEB ---
@app.get("/")
async def get_app():
    if os.path.exists("index.html"):
        with open("index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>index.html lipsă</h1>")

# --- WEBSOCKET ENDPOINT (ADAPTAT PENTRU VOSK) ---
@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: int, username: str = "Anonim"):
    await websocket.accept()
    await manager.connect(websocket, username)
    
    # Vosk Model Sample Rate (trebuie să fie la fel cu modelul, de obicei 16000)
    SAMPLE_RATE = 16000
    rec = KaldiRecognizer(vosk_model, SAMPLE_RATE)
    
    try:
        while True:
            # 1. Primim Audio Brut (poate fi WebM din browser sau WAV din script)
            audio_data = await websocket.receive_bytes()
            t0 = time.time()

            # --- CONVERSIE OBLIGATORIE PENTRU VOSK ---
            try:
                # Încărcăm bytes-ii într-un container audio (detectează automat dacă e WebM, WAV, MP3)
                audio_segment = AudioSegment.from_file(io.BytesIO(audio_data))
                
                # Forțăm formatul cerut de Vosk: 16000Hz, Mono, PCM s16le
                audio_segment = audio_segment.set_frame_rate(SAMPLE_RATE).set_channels(1).set_sample_width(2)
                
                # Extragem raw bytes (fără header WAV)
                pcm_data = audio_segment.raw_data
                
            except Exception as e:
                print(f"⚠️ Eroare conversie audio: {e}")
                continue # Sărim peste pachetul ăsta stricat
            # -----------------------------------------

            # Procesare Vosk cu datele curate (pcm_data)
            if rec.AcceptWaveform(pcm_data):
                res = json.loads(rec.Result())
                text = res.get("text", "").strip()
            else:
                # Vosk procesează stream-uri, uneori rezultatul e în PartialResult
                # Dar pentru testul tău (propoziție cu propoziție), ne bazăm pe FinalResult sau Result
                text = ""
                # Opțional: Poți verifica rec.PartialResult() dacă vrei realtime feedback

            # Dacă textul e gol după AcceptWaveform, verificăm FinalResult la final de stream
            # Dar aici suntem într-un loop continuu.
            # Truc: Pentru scriptul tău care trimite fraze scurte, AcceptWaveform s-ar putea să nu returneze text imediat.
            # Hai să forțăm un rezultat final la fiecare pachet mare primit (dacă scriptul trimite fraza întreagă o dată)
            
            # (Dacă scriptul trimite chunk-uri mici, logica e alta, dar pt auto_reader e ok așa)
            if not text:
                final_res = json.loads(rec.FinalResult()) # Asta golește bufferul Vosk
                text = final_res.get("text", "").strip()
                # Re-inițializăm recunoașterea pentru următoarea frază
                rec = KaldiRecognizer(vosk_model, SAMPLE_RATE)

            t1 = time.time()

            # 2. Predicție (BERT) & Logare
            if text:
                toxic_labels = await check_toxicity(text)
                t2 = time.time()
                
                stt_time = (t1 - t0) * 1000
                ai_time  = (t2 - t1) * 1000
                total_latency = stt_time + ai_time
                user_count = len(manager.active_connections)
                
                log_interaction(username, text, toxic_labels, stt_time, ai_time, total_latency, user_count)
                print(f"🗣️ {username} (Vosk): {text} ({total_latency:.0f}ms)")

                if toxic_labels:
                    reasons = ", ".join([l['label'] for l in toxic_labels])
                    await manager.send_json(websocket, {"type": "status", "status": "toxic", "message": f"BLOCAT: {reasons}"})
                else:
                    await manager.broadcast_audio(audio_data, websocket)
            
            # Nu facem nimic dacă nu e text (Silence)

    except WebSocketDisconnect:
        left_user = manager.disconnect(websocket)
        await manager.broadcast_system(f"🔴 {left_user} a ieșit.")
    except Exception as e:
        print(f"Eroare WS Generală: {e}")
        manager.disconnect(websocket)