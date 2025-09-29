#!/usr/bin/env bash
set -euo pipefail

# -------------------------
# Config
# -------------------------
QUESTION="How many credits are you taking this year? Please answer after the beep."
RECORD_SECONDS=8
WAV_FILE="answer.wav"
TRANSCRIPT_TXT="answer_transcript.txt"
DIGITS_TXT="answer_digits.txt"

STT_ENGINE="faster"      # faster (recommended) | whisper
FASTER_MODEL="tiny.en"   # tiny.en/base.en/...
WHISPER_MODEL="tiny.en"  # tiny.en/base.en/...
TTS_ENGINE="gtts"        # gtts | espeak
ALSA_DEV=""              # e.g., "plughw:1,0" (use `arecord -l` to check)
DEBUG_PLAYBACK=true      # play back the recorded audio for sanity check

have() { command -v "$1" >/dev/null 2>&1; }

say_en() {
  local text="$1"
  if [[ "$TTS_ENGINE" == "gtts" ]]; then
    if have python3 && python3 - <<'PY' >/dev/null 2>&1; then
from gtts import gTTS
PY
      local mp3; mp3="$(mktemp --suffix=.mp3)"
      python3 - "$text" "$mp3" <<'PY'
import sys
from gtts import gTTS
text, outp = sys.argv[1], sys.argv[2]
gTTS(text=text, lang="en").save(outp)
PY
      if have mpg123; then mpg123 -q "$mp3"; else echo "[WARN] mpg123 not installed"; fi
      rm -f "$mp3"
      return 0
    fi
    echo "[WARN] gTTS unavailable; falling back to espeak."
  fi
  if have espeak-ng; then espeak-ng -v en-us "$text"
  elif have espeak; then espeak -v en-us "$text"
  else echo "[ERROR] No TTS available (need gTTS+mpg123 or espeak[-ng])"
  fi
}

beep() { printf "\a"; }

record_wav() {
  local seconds="$1" wav="$2"
  echo "[REC] Recording ${seconds}s -> ${wav}"
  if [[ -n "$ALSA_DEV" ]]; then
    arecord -D "$ALSA_DEV" -d "$seconds" -f S16_LE -r 16000 -c 1 "$wav"
  else
    arecord -d "$seconds" -f S16_LE -r 16000 -c 1 "$wav"
  fi
}

transcribe_faster() {
  local wav="$1" out_txt="$2" model="$3"
  python3 - "$wav" "$out_txt" "$model" <<'PY'
import sys
wav, out_txt, model_id = sys.argv[1:]
try:
    from faster_whisper import WhisperModel
except Exception:
    print("[ERROR] Install faster-whisper: pip install -U faster-whisper"); sys.exit(1)
model = WhisperModel(model_id, device="cpu", compute_type="int8")
segs, info = model.transcribe(wav, language="en", beam_size=1)
text = "".join(s.text for s in segs).strip()
print("[TRANSCRIPT]", text)
open(out_txt, "w", encoding="utf-8").write(text + "\n")
PY
}

transcribe_whisper() {
  local wav="$1" out_txt="$2" model="$3"
  python3 - "$wav" "$out_txt" "$model" <<'PY'
import sys
wav, out_txt, model = sys.argv[1:]
try:
    import whisper
except Exception:
    print("[ERROR] Install openai-whisper: pip install -U openai-whisper"); sys.exit(1)
m = whisper.load_model(model)
res = m.transcribe(wav, language="en")
text = res.get("text","").strip()
print("[TRANSCRIPT]", text)
open(out_txt, "w", encoding="utf-8").write(text + "\n")
PY
}

extract_number() {
  # Read transcript; prefer digits; else parse English number words (e.g., "twenty-five")
  python3 - "$TRANSCRIPT_TXT" <<'PY'
import sys, re
path = sys.argv[1]
text = open(path, encoding="utf-8").read()

m = re.search(r'\d{1,6}', text)
if m:
    print(m.group(0))
    sys.exit(0)

# Parse simple English number words up to thousands
units = {
    'zero':0,'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,'nine':9,
    'ten':10,'eleven':11,'twelve':12,'thirteen':13,'fourteen':14,'fifteen':15,'sixteen':16,
    'seventeen':17,'eighteen':18,'nineteen':19
}
tens = {'twenty':20,'thirty':30,'forty':40,'fifty':50,'sixty':60,'seventy':70,'eighty':80,'ninety':90}
scales = {'hundred':100,'thousand':1000}

tokens = re.findall(r"[A-Za-z]+", text.lower().replace("-", " "))
def try_span(i, j):
    total = 0; current = 0; seen = False
    for w in tokens[i:j]:
        if w in units: current += units[w]; seen = True
        elif w in tens: current += tens[w]; seen = True
        elif w in scales:
            if current == 0: current = 1
            current *= scales[w]; total += current; current = 0; seen = True
        elif w == 'and': continue
        else:
            return None
    return (total + current) if seen else None

best = None
for i in range(len(tokens)):
    # limit span length to avoid over-parse
    for j in range(i+1, min(i+6, len(tokens))+1):
        val = try_span(i, j)
        if val is not None:
            best = val; break
    if best is not None: break

print("" if best is None else best)
PY
}

pluralize() {
  local n="$1"
  if [[ "$n" == "1" ]]; then echo "credit"; else echo "credits"; fi
}

# -------------------------
echo "[ask_credits] EN only | STT=$STT_ENGINE | TTS=$TTS_ENGINE"

# 1) Ask
say_en "$QUESTION"; sleep 0.6; beep; sleep 0.4

# 2) Record
record_wav "$RECORD_SECONDS" "$WAV_FILE"

# Optional playback for debugging
if [[ "${DEBUG_PLAYBACK:-false}" == "true" ]]; then
  if have aplay; then aplay "$WAV_FILE"; fi
fi

# 3) Transcribe
if [[ "$STT_ENGINE" == "faster" ]]; then
  if python3 -c "import faster_whisper" >/dev/null 2>&1; then
    transcribe_faster "$WAV_FILE" "$TRANSCRIPT_TXT" "$FASTER_MODEL"
  else
    echo "[WARN] faster-whisper not installed; falling back to whisper"
    STT_ENGINE="whisper"
  fi
fi
if [[ "$STT_ENGINE" == "whisper" ]]; then
  if python3 -c "import whisper" >/dev/null 2>&1; then
    transcribe_whisper "$WAV_FILE" "$TRANSCRIPT_TXT" "$WHISPER_MODEL"
  else
    echo "[ERROR] No STT engine installed. Install faster-whisper or openai-whisper."; exit 1
  fi
fi

# 4) Extract number & respond
NUM="$(extract_number)"
echo "${NUM}" > "$DIGITS_TXT"
if [[ -n "$NUM" ]]; then
  echo "[RESULT] Credits detected: $NUM"
  say_en "Great, you are taking ${NUM} $(pluralize "$NUM") this year."
else
  echo "[RESULT] No number detected."
  say_en "Sorry, I could not catch a number. Please try again."
fi

echo "[ask_credits] Done."
