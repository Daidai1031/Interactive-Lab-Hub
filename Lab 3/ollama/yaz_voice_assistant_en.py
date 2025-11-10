#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YAZ Voice Assistant (English only)
- Ask & store medication time (voice)
- Tell stored time (voice)
- Short YAZ Q&A via Ollama (<= 50 words) + TTS

Deps (venv):
  pip install openai-whisper gTTS requests
System:
  sudo apt install -y ffmpeg alsa-utils mpg123 espeak-ng
Ollama:
  cd ~/Interactive-Lab-Hub/Lab\ 3/ollama && ./ollama pull phi3:mini
"""

import os
import re
import sys
import time
import socket
import subprocess
import requests

# =======================
# Config (adapted to your setup)
# =======================
# Prefer env var; else default to local service
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
OLLAMA_URL  = f"http://{OLLAMA_HOST}"
OLLAMA_MODEL = "phi3:mini"
REQUEST_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "600"))

# Your Ollama folder & binary
PROJECT_OLLAMA_DIR = os.path.expanduser("~/Interactive-Lab-Hub/Lab 3/ollama")
OLLAMA_BIN = os.path.join(PROJECT_OLLAMA_DIR, "ollama")  # expects ./ollama here

# Files / audio
DATA_FILE = "yaz_meds_time.txt"   # stores HH:MM (24h)
WAV_FILE  = "yaz_input.wav"
RECORD_SECONDS = 6
ALSA_DEV = ""                      # e.g. "plughw:1,0" from `arecord -l`

# =======================
# Utilities
# =======================
def say_en(text: str):
    """TTS: prefer gTTS (en), fallback to espeak"""
    try:
        from gtts import gTTS
        import tempfile
        mp3 = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3").name
        gTTS(text=text, lang="en").save(mp3)
        try:
            subprocess.run(["mpg123", "-q", mp3], check=False)
        finally:
            os.remove(mp3)
        return
    except Exception:
        pass
    # Fallback: espeak-ng / espeak
    try:
        subprocess.run(["espeak-ng", "-v", "en-us", text], check=False)
    except FileNotFoundError:
        subprocess.run(["espeak", "-v", "en-us", text], check=False)

def record_wav(seconds=RECORD_SECONDS, wav=WAV_FILE):
    print(f"[REC] Recording {seconds}s -> {wav}")
    if ALSA_DEV:
        cmd = ["arecord", "-D", ALSA_DEV, "-d", str(seconds), "-f", "S16_LE", "-r", "16000", "-c", "1", wav]
    else:
        cmd = ["arecord", "-d", str(seconds), "-f", "S16_LE", "-r", "16000", "-c", "1", wav]
    subprocess.run(cmd, check=True)

def transcribe_en(wav=WAV_FILE) -> str:
    """Whisper English transcription (tiny.en)"""
    try:
        import whisper
    except Exception:
        print("[ERROR] Install openai-whisper in venv: pip install -U openai-whisper")
        return ""
    print("[STT] Transcribing...")
    model = whisper.load_model("tiny.en")
    res = model.transcribe(wav, language="en")
    text = (res.get("text") or "").strip()
    print(f"[TRANSCRIPT] {text}")
    return text

# =======================
# Time parsing (English)
# =======================
WORDS = {
    'zero':0,'oh':0,'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,'nine':9,
    'ten':10,'eleven':11,'twelve':12,'thirteen':13,'fourteen':14,'fifteen':15,
    'sixteen':16,'seventeen':17,'eighteen':18,'nineteen':19,
    'twenty':20,'thirty':30,'forty':40,'fifty':50
}

def word_to_number(token):
    token = token.lower()
    return WORDS.get(token, None)

def parse_time_en(text: str) -> str:
    """
    Extract time like:
      - "9:30", "09:05", "21:10"
      - "9 pm", "7am"
      - "nine thirty", "half past nine", "quarter to ten"
    Return "HH:MM" or "" if not found.
    """
    t = (text or "").lower()
    t = t.replace("a.m.", "am").replace("p.m.", "pm").replace(" a. m.", " am").replace(" p. m.", " pm")
    t = t.replace(" o' clock", " oclock").replace("o clock", "oclock")

    # 1) hh:mm
    m = re.search(r"\b([01]?\d|2[0-3])[:：]([0-5]\d)\b", t)
    if m:
        hh = int(m.group(1)); mm = int(m.group(2))
        return f"{hh:02d}:{mm:02d}"

    # 2) hh am/pm (no minutes)
    m = re.search(r"\b([0-1]?\d)\s*(am|pm)\b", t)
    if m:
        h = int(m.group(1)); ap = m.group(2)
        if ap == "pm" and h != 12: h += 12
        if ap == "am" and h == 12: h = 0
        return f"{h:02d}:00"

    # 3) hh mm am/pm (digits or words)
    m = re.search(r"\b([a-z]+|\d{1,2})\s+([a-z]+|\d{1,2})\s*(am|pm)\b", t)
    if m:
        h_tok, m_tok, ap = m.groups()
        h = int(h_tok) if h_tok.isdigit() else (word_to_number(h_tok) or 0)
        mv = int(m_tok) if m_tok.isdigit() else (word_to_number(m_tok) or 0)
        if ap == "pm" and h != 12: h += 12
        if ap == "am" and h == 12: h = 0
        if 0 <= h <= 23 and 0 <= mv <= 59:
            return f"{h:02d}:{mv:02d}"

    # 4) "half/quarter past/to nine/10"
    m = re.search(r"\b(half|quarter)\s+(past|to)\s+([a-z]+|\d{1,2})\b", t)
    if m:
        frac, rel, hr = m.groups()
        h = int(hr) if hr.isdigit() else (word_to_number(hr) or 0)
        if frac == "half":
            # "half to ten" ≈ 09:30
            if rel == "to":
                h = (h - 1) % 24
            mm = 30
        else:  # quarter
            if rel == "past":
                mm = 15
            else:
                h = (h - 1) % 24
                mm = 45
        return f"{h:02d}:{mm:02d}"

    # 5) "nine thirty" / "ten five"
    tokens = re.findall(r"[a-z]+|\d{1,2}", t)
    for i in range(len(tokens)-1):
        h_tok, m_tok = tokens[i], tokens[i+1]
        h = int(h_tok) if h_tok.isdigit() else word_to_number(h_tok)
        mv = int(m_tok) if m_tok.isdigit() else word_to_number(m_tok)
        if h is not None and mv is not None and 0 <= h <= 23 and 0 <= mv <= 59:
            return f"{h:02d}:{mv:02d}"

    # 6) single hour fallback => HH:00
    m = re.search(r"\b([0-1]?\d|2[0-3])\b", t)
    if m:
        return f"{int(m.group(1)):02d}:00"
    for w in tokens:
        val = word_to_number(w)
        if val is not None and 0 <= val <= 23:
            return f"{val:02d}:00"
    return ""

# =======================
# Storage
# =======================
def save_time(hhmm: str):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        f.write(hhmm.strip()+"\n")

def load_time() -> str:
    if not os.path.exists(DATA_FILE): return ""
    return open(DATA_FILE, "r", encoding="utf-8").read().strip()

# =======================
# Ollama helpers
# =======================
def is_port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False

def ensure_ollama_ready() -> bool:
    """Ensure Ollama is listening; if not, try to start from your folder and pull model."""
    try:
        host, port = OLLAMA_HOST.split(":")[0], int(OLLAMA_HOST.split(":")[1])
    except Exception:
        print(f"[ERROR] Invalid OLLAMA_HOST: {OLLAMA_HOST}")
        return False

    if not is_port_open(host, port):
        print(f"[INFO] Ollama not on {OLLAMA_HOST}. Trying to start from {PROJECT_OLLAMA_DIR} ...")
        try:
            subprocess.Popen([OLLAMA_BIN, "serve"], cwd=PROJECT_OLLAMA_DIR)
            time.sleep(2)
        except Exception as e:
            print(f"[WARN] Failed to spawn ollama serve: {e}")

    if not is_port_open(host, port):
        print(f"[ERROR] Ollama still not available on {OLLAMA_HOST}.")
        return False

    # Touch /api/tags
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        if r.status_code != 200:
            print("[WARN] /api/tags not OK yet.")
    except Exception as e:
        print(f"[WARN] Cannot reach /api/tags: {e}")

    # Pull model (API or CLI)
    try:
        requests.post(f"{OLLAMA_URL}/api/pull", json={"name": OLLAMA_MODEL}, timeout=REQUEST_TIMEOUT)
    except Exception:
        try:
            subprocess.run([OLLAMA_BIN, "pull", OLLAMA_MODEL], cwd=PROJECT_OLLAMA_DIR, check=False)
        except Exception:
            pass
    return True

SYSTEM_EN = (
  "You are a medical info assistant. Answer in ENGLISH, concise, <= 50 words. "
  "If safety or individual differences matter, suggest consulting a clinician."
)

def ask_ollama_short_en(user_q: str) -> str:
    prompt = f"{SYSTEM_EN}\nQuestion: {user_q}\nShort answer:"
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_predict": 60,      # tighter: 40/30 => even shorter
                    "temperature": 0.2,
                    "repeat_penalty": 1.1
                },
                "stop": ["\n\n"]          # stop early on blank line
            },
            timeout=REQUEST_TIMEOUT
        )
        if r.status_code == 200:
            text = (r.json().get("response","") or "").strip()
            words = text.split()
            if len(words) > 50:
                text = " ".join(words[:50])
            return text
        return f"Error: HTTP {r.status_code}"
    except requests.exceptions.Timeout:
        return "Sorry, response timed out. Please try again."
    except Exception as e:
        return f"Error: {e}"

# =======================
# Flows
# =======================
def flow_set_time():
    say_en("What time would you like to take your pill? Please say the time.")
    print(">> Speak now…")
    record_wav()
    text = transcribe_en()
    hhmm = parse_time_en(text)
    if not hhmm:
        say_en("I did not catch a valid time. Please try again.")
        print("[WARN] No time parsed.")
        return
    save_time(hhmm)
    say_en(f"Got it. I saved your medication time as {hhmm} every day.")
    print(f"[OK] Saved: {hhmm}")

def flow_ask_time():
    hhmm = load_time()
    if hhmm:
        say_en(f"Your medication time is {hhmm} every day.")
        print(f"[INFO] Current: {hhmm}")
    else:
        say_en("You have not set a medication time yet.")
        print("[INFO] Not set.")

def flow_yaz_qna():
    say_en("What question do you have about your medication?")
    print(">> Speak now…")
    record_wav()
    text = transcribe_en()
    if not text:
        say_en("I did not hear your question. Please try again.")
        return
    ans = ask_ollama_short_en(text)
    print(f"[AI] {ans}")
    say_en(ans)

# =======================
# Main
# =======================
def main():
    print("=== YAZ Voice Assistant (English) ===")
    # ensure ollama from your folder is ready
    if not ensure_ollama_ready():
        print(f"Please start Ollama manually: {OLLAMA_BIN} serve  (in {PROJECT_OLLAMA_DIR})")
        return

    print("1) Set medication time (voice)")
    print("2) Query medication time (voice)")
    print("3) YAZ Q&A (<=50 words, voice)")
    print("4) Quit")
    while True:
        try:
            choice = input("Choose 1-4: ").strip()
        except EOFError:
            break
        if choice == "1":
            flow_set_time()
        elif choice == "2":
            flow_ask_time()
        elif choice == "3":
            flow_yaz_qna()
        elif choice == "4":
            say_en("Goodbye.")
            break
        else:
            print("Invalid option.")

if __name__ == "__main__":
    main()
