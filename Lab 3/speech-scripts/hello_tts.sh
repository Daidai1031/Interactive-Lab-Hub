#!/usr/bin/env bash
set -euo pipefail

# Default options
ENGINE=""           # auto-detect if empty
NAME="${USER:-Friend}"
LANG="en"           # en | zh
RATE=""             # engine-specific (e.g., 150-200 for espeak)
VOICE=""            # engine-specific (e.g., en-us, en+f3)

usage() {
  cat <<'EOF'
Usage:
  ./hello_tts.sh [-e ENGINE] [-n NAME] [-l LANG] [-r RATE] [-v VOICE]

Options:
  -e ENGINE   TTS engine: espeak|espeak-ng|festival|flite|pico2wave|piper
               (default: auto-detect what's installed)
  -n NAME     Your name (default: current user)
  -l LANG     Language: en or zh (default: en)
  -r RATE     Speaking rate (engine-specific, optional)
  -v VOICE    Voice id (engine-specific, optional)

Examples:
  ./hello_tts.sh -n Daisy
  ./hello_tts.sh -e espeak -n Daisy -r 175 -v en-us
  ./hello_tts.sh -e pico2wave -n 小兰 -l zh
  ./hello_tts.sh -e piper -n Daisy
EOF
}

while getopts ":e:n:l:r:v:h" opt; do
  case $opt in
    e) ENGINE="$OPTARG" ;;
    n) NAME="$OPTARG" ;;
    l) LANG="$OPTARG" ;;
    r) RATE="$OPTARG" ;;
    v) VOICE="$OPTARG" ;;
    h) usage; exit 0 ;;
    \?) echo "Invalid option: -$OPTARG" >&2; usage; exit 1 ;;
  esac
done

# Message templates
if [[ "$LANG" == "zh" ]]; then
  MSG="你好，${NAME}！欢迎回到你的树莓派。祝你实验顺利！"
else
  MSG="Hello, ${NAME}! Welcome back to your Raspberry Pi. Have a great lab!"
fi

# Helper: command exists?
have() { command -v "$1" >/dev/null 2>&1; }

# Auto-detect engine if not specified (priority order)
if [[ -z "$ENGINE" ]]; then
  if   have espeak-ng; then ENGINE="espeak-ng"
  elif have espeak;    then ENGINE="espeak"
  elif have pico2wave; then ENGINE="pico2wave"
  elif have festival;  then ENGINE="festival"
  elif have flite;     then ENGINE="flite"
  elif have piper;     then ENGINE="piper"
  else
    echo "No supported TTS engine found. Install one of: espeak-ng, espeak, pico2wave, festival, flite, piper." >&2
    exit 1
  fi
fi

echo "[hello_tts] Engine: $ENGINE | Name: $NAME | Lang: $LANG"

case "$ENGINE" in
  espeak-ng|espeak)
    # Language & voice handling
    # Common voice ids: en-us, en-gb. Chinese voices vary by install; espeak-ng often uses 'zh' or 'zh+yue'.
    VOPT=()
    [[ -n "$VOICE" ]] && VOPT+=(-v "$VOICE") || {
      if [[ "$LANG" == "zh" ]]; then
        VOPT+=(-v zh)  # adjust if your device has a specific zh voice (e.g., zh+yue)
      else
        VOPT+=(-v en-us)
      fi
    }
    [[ -n "$RATE" ]] && VOPT+=(-s "$RATE")
    "$ENGINE" "${VOPT[@]}" "$MSG"
    ;;

  festival)
    # festival relies on installed voices; default is English
    echo "$MSG" | festival --tts
    ;;

  flite)
    # flite English-centric; will best handle LANG=en
    flite -t "$MSG"
    ;;

  pico2wave)
    # pico2wave supports: en-US, en-GB, de-DE, es-ES, fr-FR, it-IT
    # It does NOT support Chinese; we fallback to English voice for zh as well.
    if [[ "$LANG" == "zh" ]]; then
      PICO_LANG="en-US"
      echo "[hello_tts] pico2wave has no zh voice; using $PICO_LANG instead."
    else
      PICO_LANG="en-US"
    fi
    TMPWAV="/tmp/hello_${RANDOM}.wav"
    pico2wave -l "$PICO_LANG" -w "$TMPWAV" "$MSG"
    aplay "$TMPWAV"
    rm -f "$TMPWAV"
    ;;

  piper)
    # Pick a piper model (downloaded on first use)
    if [[ "$LANG" == "zh" ]]; then
      # If you have a Chinese model, replace below with that model id.
      # As a safe default, use English model.
      MODEL="en_US-lessac-medium"
      echo "[hello_tts] Using English piper model as fallback for zh."
    else
      MODEL="en_US-lessac-medium"
    fi
    echo "$MSG" | piper --model "$MODEL" --output-raw | aplay -r 22050 -f S16_LE -t raw -
    ;;

  *)
    echo "Unsupported engine: $ENGINE" >&2
    exit 1
    ;;
esac

echo "[hello_tts] Done."
