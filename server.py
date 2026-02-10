import os
import asyncio
import aiohttp
import io
from typing import Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from faster_whisper import WhisperModel

app = FastAPI()

# --- CONFIGURARE ---
TOXICITY_API_URL = "http://127.0.0.1:8000/check"

print("🚀 Încărcare Whisper TURBO (tiny.en)...")
model = WhisperModel("tiny.en", device="cpu", compute_type="int8", cpu_threads=4)
print("✅ Whisper Gata!")

# --- MANAGER DE CONEXIUNI AVANSAT ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[WebSocket, str] = {}

    async def connect(self, websocket: WebSocket, username: str):
        await websocket.accept()
        # Salvăm conexiunea
        self.active_connections[websocket] = username
        
        # 1. Anunțăm sistemul
        await self.broadcast_system(f"🔵 {username} s-a alăturat.")
        
        # 2. Trimitem LISTA ACTUALIZATĂ la toată lumea
        await self.broadcast_user_list()

    def disconnect(self, websocket: WebSocket):
        username = self.active_connections.get(websocket, "Unknown")
        if websocket in self.active_connections:
            del self.active_connections[websocket]
        return username

    async def broadcast_user_list(self):
        """Trimite lista cu toți userii conectați către toată lumea"""
        # Extragem doar lista de nume unice
        users_list = list(self.active_connections.values())
        message = {"type": "user_list", "users": users_list}
        
        for connection in self.active_connections:
            await self.send_json(connection, message)

    async def broadcast_audio(self, audio_data: bytes, sender: WebSocket):
        sender_name = self.active_connections.get(sender, "Anonim")
        
        # Trimitem event că acest user vorbește (pentru animație UI)
        await self.broadcast_json({"type": "speaking_start", "user": sender_name})

        for connection in self.active_connections:
            if connection != sender:
                await connection.send_bytes(audio_data)

    async def broadcast_system(self, message: str):
        print(f"SYSTEM: {message}")
        await self.broadcast_json({"type": "system", "message": message})

    async def broadcast_json(self, data: dict):
        """Trimite un JSON la toată lumea"""
        for connection in self.active_connections:
            await self.send_json(connection, data)

    async def send_personal_message(self, websocket: WebSocket, message: str, status: str):
        await self.send_json(websocket, {"type": "status", "status": status, "message": message})

    async def send_json(self, websocket: WebSocket, data: dict):
        try:
            await websocket.send_json(data)
        except:
            pass
        
manager = ConnectionManager()

@app.get("/")
async def get():
    with open("index.html", "r") as f:
        return HTMLResponse(f.read())

async def check_toxicity(text):
    async with aiohttp.ClientSession() as session:
        try:
            payload = {"text": text, "threshold": 0.5}
            async with session.post(TOXICITY_API_URL, json=payload, timeout=2) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("toxic_labels", [])
        except:
            return []
    return []

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: int, username: str = "Anonim"):
    # Primim username-ul direct din URL (Query Param)
    await manager.connect(websocket, username)
    
    try:
        while True:
            audio_data = await websocket.receive_bytes()
            audio_file = io.BytesIO(audio_data)
            
            try:
                segments, _ = await asyncio.to_thread(model.transcribe, audio_file, beam_size=1)
                text = " ".join([s.text for s in segments]).strip()
            except Exception as e:
                print(f"Err: {e}")
                continue

            if not text: continue

            print(f"🗣️ {username}: {text}")

            toxic_labels = await check_toxicity(text)

            if toxic_labels:
                reasons = ", ".join([l['label'] for l in toxic_labels])
                print(f"🛑 BLOCAT ({username}): {text}")
                await manager.send_personal_message(websocket, f"BLOCAT: {reasons}", "toxic")
            else:
                # E SAFE -> Trimitem la toți
                await manager.broadcast_audio(audio_data, websocket)
                # Confirmare către cel care a vorbit
                await manager.send_personal_message(websocket, f"Trimis: {text}", "safe")

    except WebSocketDisconnect:
        user_left = manager.disconnect(websocket)
        await manager.broadcast_system(f"🔴 {user_left} s-a deconectat.")