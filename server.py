import os
import asyncio
import aiohttp
import io
from typing import List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from faster_whisper import WhisperModel

app = FastAPI()

# --- CONFIGURARE TURBO ---
TOXICITY_API_URL = "http://127.0.0.1:8000/check"

print("🚀 Încărcare Whisper TURBO (tiny.en)...")
# 'tiny.en' este cel mai rapid model posibil
model = WhisperModel("tiny.en", device="cpu", compute_type="int8", cpu_threads=4)
print("✅ Whisper Gata!")

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast_audio(self, audio_data: bytes, sender: WebSocket):
        # Trimitem datele brute direct din RAM
        for connection in self.active_connections:
            if connection != sender:
                await connection.send_bytes(audio_data)

    async def send_status(self, websocket: WebSocket, message: str, status: str):
        await websocket.send_text(f'{{"status": "{status}", "message": "{message}"}}')

manager = ConnectionManager()

@app.get("/")
async def get():
    with open("index.html", "r") as f:
        return HTMLResponse(f.read())

async def check_toxicity(text):
    async with aiohttp.ClientSession() as session:
        try:
            # Timeout scurt de 2 secunde - dacă API-ul răspunde greu, ignorăm
            payload = {"text": text, "threshold": 0.5}
            async with session.post(TOXICITY_API_URL, json=payload, timeout=2) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("toxic_labels", [])
        except Exception as e:
            print(f"⚠️ API Timeout/Eroare: {e}")
            return []
    return []

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: int):
    await manager.connect(websocket)
    try:
        while True:
            # 1. Primim datele în RAM
            audio_data = await websocket.receive_bytes()
            
            # 2. Creăm un "fișier virtual" în memorie
            audio_file = io.BytesIO(audio_data)
            
            # 3. Transcriem direct din RAM (Fără salvare pe disc)
            # Rulăm în thread separat pentru a nu bloca WebSocket-ul
            try:
                segments, _ = await asyncio.to_thread(model.transcribe, audio_file, beam_size=1)
                text = " ".join([s.text for s in segments]).strip()
            except Exception as e:
                print(f"Eroare Whisper: {e}")
                continue

            if not text:
                continue

            print(f"⚡ User {client_id}: {text}")

            # 4. Verificăm Toxicitatea
            toxic_labels = await check_toxicity(text)

            if toxic_labels:
                reasons = ", ".join([l['label'] for l in toxic_labels])
                print(f"🛑 BLOCAT: {text}")
                await manager.send_status(websocket, f"BLOCAT: {reasons}", "toxic")
            else:
                print(f"✅ SAFE -> Broadcast")
                await manager.broadcast_audio(audio_data, websocket)
                await manager.send_status(websocket, f"Trimis: {text}", "safe")

    except WebSocketDisconnect:
        manager.disconnect(websocket)