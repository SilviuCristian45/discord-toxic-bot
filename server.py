import os
import asyncio
import aiohttp
import io
import time
import csv
from datetime import datetime
from typing import Dict, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from faster_whisper import WhisperModel

app = FastAPI()

# --- CONFIGURARE ---
TOXICITY_API_URL = "http://127.0.0.1:8000/check"
CSV_FILE = "stats.csv"

# Inițializăm CSV-ul dacă nu există
if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, mode='w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        # Header-ul CSV-ului
        writer.writerow(["timestamp", "user", "text", "toxic_labels", "stt_time", "ai_time", "latency_ms", "user_count"])

print("🚀 Încărcare Whisper TURBO (tiny.en)...")
model = WhisperModel("tiny.en", device="cpu", compute_type="int8", cpu_threads=4)
print("✅ Whisper Gata!")

# --- LOGARE ÎN CSV ---
def log_interaction(username, text, toxic_labels, stt_time, ai_time, total_latency, user_count):
    try:
        with open(CSV_FILE, mode='a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            labels_str = ";".join([l['label'] for l in toxic_labels]) if toxic_labels else "SAFE"
            writer.writerow([timestamp, username, text, labels_str, f"{stt_time:.2f}", f"{ai_time:.2f}", f"{total_latency:.2f}", user_count])
    except Exception as e:
        print(f"Eroare scriere CSV: {e}")

# --- MANAGER DE CONEXIUNI ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[WebSocket, str] = {}

    async def connect(self, websocket: WebSocket, username: str):
        await websocket.accept()
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
        sender_name = self.active_connections.get(sender, "Anonim")
        # Trimitem notificare că X vorbește (pt animație)
        for connection in self.active_connections:
            try: await connection.send_json({"type": "speaking_start", "user": sender_name})
            except: pass
            
            # Trimitem audio la toți ceilalți (NU și la cel care vorbește)
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

# --- RUTELE WEB (AICI ERA PROBLEMA TA) ---

@app.get("/")
async def get_app():
    # Asta deschide WALKIE TALKIE
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/dashboard")
async def get_dashboard():
    # Asta deschide GRAFICELE (trebuie să ai fișierul dashboard.html)
    if os.path.exists("dashboard.html"):
        with open("dashboard.html", "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    else:
        return HTMLResponse("<h1>Eroare: Nu gasesc dashboard.html</h1>")

@app.get("/api/stats")
async def get_stats():
    # API-ul care citește CSV-ul pentru grafic
    data = []
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            data = list(reader)
    return JSONResponse(data)

# --- WEBSOCKET ---

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: int, username: str = "Anonim"):
    await manager.connect(websocket, username)
    try:
        while True:
            # Primire Audio

            audio_data = await websocket.receive_bytes()
            audio_file = io.BytesIO(audio_data)
            t0 = time.time()

			# Transcriere (Whisper)
            segments, _ = await asyncio.to_thread(model.transcribe, audio_file, beam_size=1)
            text = " ".join([s.text for s in segments]).strip()

			# 2. Timp intermediar (După Whisper)
            t1 = time.time()

			# Predicție (BERT)
            toxic_labels = await check_toxicity(text)

			# 3. Stop Cronometru
            t2 = time.time()

			# Calcule
            stt_time = (t1 - t0) * 1000
            ai_time  = (t2 - t1) * 1000
            total_latency = stt_time + ai_time
            
            user_count = len(manager.active_connections)
            
            # Salvăm în CSV
            log_interaction(username, text, toxic_labels, stt_time, ai_time, total_latency, user_count)
            print(f"🗣️ {username}: {text} ({total_latency:.0f}ms)")

            # 3. Decizie
            if toxic_labels:
                reasons = ", ".join([l['label'] for l in toxic_labels])
                await manager.send_json(websocket, {"type": "status", "status": "toxic", "message": f"BLOCAT: {reasons}"})
            else:
                await manager.broadcast_audio(audio_data, websocket)

    except WebSocketDisconnect:
        left_user = manager.disconnect(websocket)
        await manager.broadcast_user_list()
        await manager.broadcast_system(f"🔴 {left_user} a ieșit.")