#!/usr/bin/env bash
set -euo pipefail

# -------------------------
# Config (EN only, 5-digit ZIP only)
# -------------------------
QUESTION="Please say your five-digit ZIP code after the beep."
RECORD_SECONDS=6
WAV_FILE="zip_input.wav"
TRANSCRIPT_TXT="zip_transcript.txt"
ZIP_TXT="zip_digits.txt"

# Whisper model: tiny.en / base.en / small.en (bigger = slower, more robust)
WHISPER_MODEL="tiny.en"

# TTS: gtts (online) or espeak (offline)
TTS_ENGINE="gtts"

# Audio device (leave empty to use default). Example: ALSA_DEV="plughw:1,0"
ALSA_DEV=""
DEBUG_PLAYBACK=true

have() { command -v "$1" >/dev/null 2>&1; }

say_en() {
  local text="$1"
  if [[ "$TTS_ENGINE" == "gtts" ]]; then
    if have python3 && python3 -c 'import gtts' >/dev/null 2>&1 ; then
      local mp3; mp3="$(mktemp --suffix=.mp3)"
      # 用 python -c，避免 heredoc 和引号陷阱
      python3 -c 'import sys; from gtts import gTTS; gTTS(text=sys.argv[1], lang="en").save(sys.argv[2])' \
        "$text" "$mp3"
      if have mpg123; then mpg123 -q "$mp3"; else echo "[WARN] mpg123 not installed"; fi
      rm -f "$mp3"
      return 0
    else
      echo "[WARN] gTTS unavailable; falling back to espeak."
    fi
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

transcribe_whisper() {
  local wav="$1" out_txt="$2" model="$3"
  # 全部放进 python -c，避免 heredoc
  python3 -c '
import sys, warnings
wav, out_txt, model = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    import whisper
except Exception:
    print("[ERROR] Install openai-whisper in your venv: pip install -U openai-whisper"); sys.exit(1)
warnings.filterwarnings("ignore", category=UserWarning)
m = whisper.load_model(model)
res = m.transcribe(
    wav,
    language="en",
    temperature=0.0,
    no_speech_threshold=0.15,
    logprob_threshold=-1.0,
    compression_ratio_threshold=2.4
)
text = (res.get("text") or "").strip()
print("[TRANSCRIPT]", text)
open(out_txt, "w", encoding="utf-8").write(text + "\n")
' "$wav" "$out_txt" "$model"
}

extract_zip_5only() {
  # 从转写中提取所有数字字符，拼接后取前 5 个
  python3 - "$TRANSCRIPT_TXT" <<'PY'
import sys, re
text = open(sys.argv[1], encoding="utf-8").read()
digits = re.findall(r"\d", text)          # 抓取所有数字字符（忽略逗号/空格/句号）
joined = "".join(digits)
print(joined[:5] if len(joined) >= 5 else "", end="")
PY
}

speak_zip_back() {
  local zip="$1"
  # 直接读出整体数字串
  say_en "Great, your ZIP code is ${zip}."
}

echo "[ask_zip_whisper] EN only | STT=whisper($WHISPER_MODEL) | TTS=$TTS_ENGINE"

# 1) Ask
say_en "$QUESTION"; sleep 0.6; beep; sleep 0.4

# 2) Record
record_wav "$RECORD_SECONDS" "$WAV_FILE"

# 3) (optional) Playback
if [[ "${DEBUG_PLAYBACK:-false}" == "true" ]]; then
  have aplay && aplay "$WAV_FILE" || true
fi

# 4) Transcribe with Whisper
transcribe_whisper "$WAV_FILE" "$TRANSCRIPT_TXT" "$WHISPER_MODEL"

# 5) Extract 5-digit ZIP & speak back
ZIP="$(extract_zip_5only)"
echo -n "$ZIP" > "$ZIP_TXT"

if [[ -n "$ZIP" ]]; then
  echo "[RESULT] 5-digit ZIP detected: $ZIP"
  speak_zip_back "$ZIP"
else
  echo "[RESULT] No 5-digit ZIP detected."
  say_en "Sorry, I could not catch a five-digit ZIP code. Please try again and speak the digits clearly."
fi

echo "[ask_zip_whisper] Done."
