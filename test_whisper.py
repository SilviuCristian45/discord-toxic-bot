from faster_whisper import WhisperModel
import time
import os

# Alegem modelul. 
# Optiuni: "tiny", "base", "small", "medium", "large-v3"
# "tiny" e cel mai rapid (aproape instant). "base" e un balans bun. "small" e deja lent pe CPU.
MODEL_SIZE = "base.en" # .en = model specific pt engleza (mai rapid decat cel multilingv)

print(f"⏳ Încărcare model '{MODEL_SIZE}'...")
start_load = time.time()

# device="cpu" pentru ca esti pe WSL fara GPU passthrough probabil. 
# compute_type="int8" face magia de viteza.
model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")

end_load = time.time()
print(f"✅ Model încărcat în {end_load - start_load:.2f} secunde!")

print("-" * 30)
print("Pentru test, ai nevoie de un fișier audio 'test.wav' în folder.")
print("Dacă nu ai, scriptul se va opri aici.")

# Verificăm dacă ai un fișier de test (opțional)
if os.path.exists("test.wav"):
    print("🎤 Încep transcrierea...")
    start_transcribe = time.time()
    
    segments, info = model.transcribe("test.wav", beam_size=5)
    
    full_text = ""
    for segment in segments:
        full_text += segment.text + " "
        
    end_transcribe = time.time()
    
    print(f"📝 Text: {full_text}")
    print(f"⏱️ Timp transcriere: {end_transcribe - start_transcribe:.2f} secunde")
else:
    print("⚠️ Pune un fișier 'test.wav' scurt aici ca să testezi viteza de transcriere.")
