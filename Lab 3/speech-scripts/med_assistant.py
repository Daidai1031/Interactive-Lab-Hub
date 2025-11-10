#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Medication Voice Assistant — Raspberry Pi 5 + Adafruit Mini PiTFT (240×135)
-----------------------------------------------------------------------------
Runs on a Pi 5 with:
  • Adafruit Mini PiTFT 135×240 Color TFT (buttons used as START/SNOOZE + FINISH)
  • Logitech webcam microphone (default input)
  • Bluetooth speaker (default output)

I/O language: English only.

Features
- Press TOP button → assistant asks: "Starting a new medication plan. When should I remind you?"
- User says a time like "in three minutes", "after 45 seconds", "at 8:05 PM", etc.
- The utterance is parsed to an absolute time T (local timezone).
- At T: speak "It’s time to take your medicine." and open a 30 s response window.
    • TOP button during window → Snooze 30 s and ring again.
    • BOTTOM button during window → "Good job. Finished!" and return to idle.
    • No press in 30 s → Auto-snooze 30 s and re-ring.

IMPORTANT: This script is written to be resilient. If some optional libraries are
missing (e.g., the RGB display driver), it will fall back to console logs.

Directory convention (adjust if needed):
  /home/pi/Interactive-Lab-Hub/Lab 3/speech-scripts
    └── med_assistant.py                ← this file
    └── models/vosk-en/                 ← Vosk English model directory (downloaded)

Install system deps (Debian/Raspberry Pi OS):
  sudo apt update
  sudo apt install -y python3-pip python3-pil python3-numpy espeak-ng portaudio19-dev libasound2-dev

Create venv (if not already):
  cd "/home/pi/Interactive-Lab-Hub/Lab 3"
  python3 -m venv .venv
  source .venv/bin/activate

Python packages (in your venv):
  pip install --upgrade pip wheel
  pip install vosk sounddevice dateparser gpiozero pillow
  # Optional fallback TTS engine if espeak-ng not found:
  pip install pyttsx3
  # Optional display drivers (if you plan to draw to Mini PiTFT via SPI):
  pip install adafruit-circuitpython-rgb-display adafruit-blinka

Download a Vosk English model (small):
  mkdir -p "models"
  cd models
  # Pick one of the small English models from https://alphacephei.com/vosk/models
  # Example (you may need to adjust the URL to the latest):
  # wget https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
  # unzip vosk-model-small-en-us-0.15.zip
  # mv vosk-model-small-en-us-0.15 vosk-en

Run:
  cd "/home/pi/Interactive-Lab-Hub/Lab 3/speech-scripts"
  source ../.venv/bin/activate
  python med_assistant.py

GPIO NOTE: Button pin numbers below are BCM guesses for the Mini PiTFT.
If your buttons don’t respond, change TOP_BUTTON_PIN / BOTTOM_BUTTON_PIN to your board’s pins.

"""

import os
import sys
import time
import re
import math
import queue
import threading
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta

# Timezone handling (Python 3.9+ has zoneinfo)
try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # Fallback: naive datetimes

# Optional libs (we’ll handle graceful fallbacks)
try:
    import dateparser  # type: ignore
except Exception:
    dateparser = None

try:
    from gpiozero import Button  # type: ignore
except Exception:
    Button = None

# Display stack (optional)
_DISPLAY_AVAILABLE = True
try:
    import board
    import digitalio
    from PIL import Image, ImageDraw, ImageFont
    import adafruit_rgb_display.st7789 as st7789
except Exception:
    _DISPLAY_AVAILABLE = False
    board = None  # type: ignore
    digitalio = None  # type: ignore
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore
    ImageFont = None  # type: ignore
    st7789 = None  # type: ignore

# Vosk ASR (offline) + sounddevice audio capture
_ASR_AVAILABLE = True
try:
    import sounddevice as sd  # type: ignore
    from vosk import Model, KaldiRecognizer  # type: ignore
except Exception:
    _ASR_AVAILABLE = False
    sd = None  # type: ignore
    Model = None  # type: ignore
    KaldiRecognizer = None  # type: ignore

# Optional TTS fallback
try:
    import pyttsx3  # type: ignore
except Exception:
    pyttsx3 = None

################################################################################
# Configuration
################################################################################

# Paths
WORKDIR = os.path.abspath(os.path.dirname(__file__))
MODEL_DIR = os.path.join(WORKDIR, "..", "speech-scripts", "models", "vosk-en")  # keep relative to Lab 3
if not os.path.isdir(MODEL_DIR):
    # Secondary guess: models folder under this script
    alt = os.path.join(WORKDIR, "models", "vosk-en")
    if os.path.isdir(alt):
        MODEL_DIR = alt

# GPIO pins (BCM numbering) — adjust to your Mini PiTFT
TOP_BUTTON_PIN = int(os.getenv("TOP_BUTTON_PIN", "23"))      # Start/Snooze
BOTTOM_BUTTON_PIN = int(os.getenv("BOTTOM_BUTTON_PIN", "24"))  # Finish
BUTTON_DEBOUNCE_S = 0.15

# Timings
RING_WINDOW_SEC = 30
SNOOZE_SEC = 30
MIN_DELAY_SEC = 5
MAX_DELAY_SEC = 24 * 3600

# Audio / ASR parameters
ASR_SAMPLE_RATE = 16000
ASR_LISTEN_WINDOW_SEC = 8

# Timezone
LOCAL_TZ_NAME = os.getenv("TZ", "America/New_York")  # change if your Pi is set differently
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME) if ZoneInfo else None

################################################################################
# Utilities
################################################################################

def now_tz() -> datetime:
    dt = datetime.now()
    if LOCAL_TZ:
        return datetime.now(tz=LOCAL_TZ)
    return dt


def humanize_delta(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} hour" + ("s" if h != 1 else ""))
    if m:
        parts.append(f"{m} minute" + ("s" if m != 1 else ""))
    if s and not h and not m:
        parts.append(f"{s} second" + ("s" if s != 1 else ""))
    return " ".join(parts) if parts else "0 seconds"


def clamp_delay(seconds: int) -> int:
    return max(MIN_DELAY_SEC, min(MAX_DELAY_SEC, seconds))

################################################################################
# Text-to-Speech
################################################################################

class Speaker:
    def __init__(self):
        self._has_espeak = self._check_espeak()
        self._tts_engine = None
        if not self._has_espeak and pyttsx3 is not None:
            try:
                self._tts_engine = pyttsx3.init()
            except Exception:
                self._tts_engine = None

    def _check_espeak(self) -> bool:
        try:
            subprocess.run(["espeak-ng", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except FileNotFoundError:
            return False

    def say(self, text: str, rate_wpm: int = 170):
        text = text.strip()
        if not text:
            return
        print(f"[TTS] {text}")
        if self._has_espeak:
            try:
                subprocess.run(["espeak-ng", "-s", str(rate_wpm), text], check=False)
            except Exception as e:
                print(f"[TTS] espeak-ng error: {e}")
        elif self._tts_engine is not None:
            try:
                self._tts_engine.setProperty('rate', rate_wpm)
                self._tts_engine.say(text)
                self._tts_engine.runAndWait()
            except Exception as e:
                print(f"[TTS] pyttsx3 error: {e}")
        else:
            # Fallback: print only
            pass

################################################################################
# Display manager (optional Mini PiTFT over SPI). Falls back to console logs.
################################################################################

class Display:
    WIDTH = 240
    HEIGHT = 135

    def __init__(self):
        self.available = _DISPLAY_AVAILABLE
        self._disp = None
        self._image = None
        self._draw = None
        self._font = None
        if self.available:
            try:
                # SPI display setup
                cs_pin = digitalio.DigitalInOut(board.CE0)
                dc_pin = digitalio.DigitalInOut(board.D25)
                reset_pin = digitalio.DigitalInOut(board.D24)

                BAUDRATE = 64000000  # 64MHz suggested
                spi = board.SPI()
                self._disp = st7789.ST7789(
                    spi,
                    cs=cs_pin,
                    dc=dc_pin,
                    rst=reset_pin,
                    baudrate=BAUDRATE,
                    width=self.WIDTH,
                    height=self.HEIGHT,
                    x_offset=53,
                    y_offset=40,
                    rotation=270,
                )
                self._image = Image.new("RGB", (self.WIDTH, self.HEIGHT))
                self._draw = ImageDraw.Draw(self._image)
                try:
                    self._font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
                except Exception:
                    self._font = ImageFont.load_default()
                self._clear((0, 0, 0))
            except Exception as e:
                print(f"[DISPLAY] Init failed, falling back to console: {e}")
                self.available = False

    def _clear(self, color=(0, 0, 0)):
        if not self.available:
            return
        self._draw.rectangle((0, 0, self.WIDTH, self.HEIGHT), fill=color)
        self._disp.image(self._image)

    def _write_center(self, lines, fill=(255, 255, 255)):
        if not self.available:
            for ln in lines:
                print(f"[DISPLAY] {ln}")
            return
        self._clear((0, 0, 0))
        # Center multi-line text
        total_h = 0
        sizes = []
        for ln in lines:
            w, h = self._draw.textsize(ln, font=self._font)
            sizes.append((w, h))
            total_h += h
        top = (self.HEIGHT - total_h) // 2
        y = top
        for (ln, (w, h)) in zip(lines, sizes):
            x = (self.WIDTH - w) // 2
            self._draw.text((x, y), ln, font=self._font, fill=fill)
            y += h + 2
        self._disp.image(self._image)

    # Public convenience methods
    def show_idle(self):
        self._write_center(["Medication Assistant", "Press TOP to start"], fill=(180, 220, 255))

    def show_listening(self):
        self._write_center(["Listening…", "Say a time"], fill=(255, 255, 180))

    def show_scheduled(self, dt: datetime):
        tstr = dt.astimezone(LOCAL_TZ).strftime("%H:%M:%S") if (LOCAL_TZ and dt.tzinfo) else dt.strftime("%H:%M:%S")
        self._write_center(["Scheduled for", tstr], fill=(180, 255, 200))

    def show_ringing(self):
        self._write_center(["Time to take", "medicine"], fill=(255, 200, 200))

    def show_snoozed(self):
        self._write_center(["Snoozed", "30 s"], fill=(200, 220, 255))

    def show_finished(self):
        self._write_center(["Taken", "✅"], fill=(200, 255, 200))

    def show_error(self, msg: str):
        self._write_center(["Error", msg], fill=(255, 180, 180))

################################################################################
# ASR (Vosk) — returns a lowercased transcript or None on timeout/low conf
################################################################################

class Listener:
    def __init__(self, model_dir: str):
        if not _ASR_AVAILABLE:
            raise RuntimeError("ASR libraries not available (vosk/sounddevice)")
        if not os.path.isdir(model_dir):
            raise RuntimeError(f"Vosk model not found at: {model_dir}")
        self._model = Model(model_dir)

    def listen(self, max_sec: int = ASR_LISTEN_WINDOW_SEC) -> str | None:
        """Capture microphone for up to max_sec seconds and return transcript (en).
        Returns None on timeout or empty/low-confidence result.
        """
        recognizer = KaldiRecognizer(self._model, ASR_SAMPLE_RATE)
        recognizer.SetWords(True)

        result_text = None
        start = time.time()

        def callback(indata, frames, time_info, status):
            nonlocal result_text
            if status:
                print(f"[ASR] status: {status}")
            if recognizer.AcceptWaveform(indata):
                res = recognizer.Result()
                txt = _extract_text(res)
                if txt:
                    result_text = txt

        with sd.RawInputStream(samplerate=ASR_SAMPLE_RATE, blocksize=8000,
                               dtype='int16', channels=1, callback=callback):
            while (time.time() - start) < max_sec and result_text is None:
                time.sleep(0.05)
            if result_text is None:
                # Try final result flush
                try:
                    txt = _extract_text(recognizer.FinalResult())
                    result_text = txt or None
                except Exception:
                    pass
        if result_text:
            print(f"[ASR] Heard: {result_text}")
        else:
            print("[ASR] No speech recognized")
        return result_text


def _extract_text(vosk_json: str) -> str:
    # Vosk returns a JSON string like {"text": "in three minutes"}
    try:
        import json
        j = json.loads(vosk_json)
        txt = (j.get("text") or "").strip().lower()
        return txt
    except Exception:
        return ""

################################################################################
# Time expression parsing → absolute datetime in local timezone
################################################################################

_ABS_AT_RE = re.compile(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.IGNORECASE)
_REL_IN_RE = re.compile(r"\b(?:in|after)\s+(.*)", re.IGNORECASE)


def parse_time_expr(utter: str) -> tuple[datetime | None, str | None]:
    """Parse utterance into an absolute datetime and a humanized delta string.
    Supports: "in N seconds/minutes/hours", "after 45 seconds", "in 1 hour 30 minutes",
    and absolute forms like "at 8:05 pm" or "at 8 pm".
    """
    utter = (utter or "").strip().lower()
    if not utter:
        return None, None

    now = now_tz()

    # 1) Relative time (in/after ...)
    m = _REL_IN_RE.search(utter)
    if m:
        frag = m.group(1).strip()
        seconds = _parse_relative_to_seconds(frag)
        if seconds is not None:
            seconds = clamp_delay(seconds)
            dt = now + timedelta(seconds=seconds)
            return dt, humanize_delta(seconds)

    # 2) Absolute time: at HH[:MM] [AM/PM]
    m = _ABS_AT_RE.search(utter)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2)) if m.group(2) else 0
        ampm = (m.group(3) or "").lower()
        if ampm == 'pm' and hh != 12:
            hh += 12
        if ampm == 'am' and hh == 12:
            hh = 0
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now + timedelta(seconds=1):
            candidate += timedelta(days=1)  # schedule next occurrence
        delta = int((candidate - now).total_seconds())
        delta = clamp_delay(delta)
        return candidate, humanize_delta(delta)

    # 3) Fallback: dateparser if available
    if dateparser is not None:
        dp_dt = dateparser.parse(
            utter,
            languages=['en'],
            settings={
                'PREFER_DATES_FROM': 'future',
                'RELATIVE_BASE': now,
                'RETURN_AS_TIMEZONE_AWARE': bool(LOCAL_TZ),
                'TIMEZONE': LOCAL_TZ_NAME if LOCAL_TZ else None,
            },
        )
        if dp_dt:
            if LOCAL_TZ and dp_dt.tzinfo is None:
                dp_dt = dp_dt.replace(tzinfo=LOCAL_TZ)
            delta = int((dp_dt - now).total_seconds())
            if delta <= 0:
                dp_dt = now + timedelta(seconds=clamp_delay(MIN_DELAY_SEC))
                delta = MIN_DELAY_SEC
            delta = clamp_delay(delta)
            return dp_dt, humanize_delta(delta)

    return None, None


def _parse_relative_to_seconds(text: str) -> int | None:
    """Parse fragments like "3 minutes", "1 hour 20 minutes", "45 sec", "1h 30m"."""
    # Normalize words
    txt = text.replace("seconds", "sec").replace("second", "sec")
    txt = txt.replace("minutes", "min").replace("minute", "min")
    txt = txt.replace("hours", "h").replace("hour", "h")
    # Tokenize simple units
    pattern = re.compile(r"(\d+)(?:\s*)(h|hr|hrs|min|m|sec|s)")
    total = 0
    found = False
    for n, unit in pattern.findall(txt):
        n = int(n)
        unit = unit.lower()
        if unit in ("h", "hr", "hrs"):
            total += n * 3600
        elif unit in ("min", "m"):
            total += n * 60
        elif unit in ("sec", "s"):
            total += n
        found = True
    if found and total > 0:
        return total

    # Try simple natural phrases: "three minutes" etc.
    WORDNUM = {
        'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
        'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
        'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
        'fifteen': 15, 'twenty': 20, 'thirty': 30, 'forty': 40,
        'fifty': 50,
    }
    m = re.search(r"(zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|twenty|thirty|forty|fifty)\s+(seconds?|minutes?|hours?)",
                  text)
    if m:
        n = WORDNUM.get(m.group(1), 0)
        unit = m.group(2)
        if unit.startswith('hour'):
            return n * 3600
        if unit.startswith('minute'):
            return n * 60
        if unit.startswith('second'):
            return n
    return None

################################################################################
# Button driver (gpiozero). If unavailable, simulate via keyboard input.
################################################################################

class ButtonDriver:
    def __init__(self, top_pin: int, bottom_pin: int, on_top, on_bottom):
        self.top_pin = top_pin
        self.bottom_pin = bottom_pin
        self.on_top = on_top
        self.on_bottom = on_bottom
        self._using_gpio = False
        self._threads: list[threading.Thread] = []

        if Button is not None:
            try:
                self._btn_top = Button(self.top_pin, pull_up=True, bounce_time=BUTTON_DEBOUNCE_S)
                self._btn_bottom = Button(self.bottom_pin, pull_up=True, bounce_time=BUTTON_DEBOUNCE_S)
                self._btn_top.when_pressed = lambda: self.on_top()
                self._btn_bottom.when_pressed = lambda: self.on_bottom()
                self._using_gpio = True
                print(f"[GPIO] Using gpiozero on pins TOP={self.top_pin} BOTTOM={self.bottom_pin}")
            except Exception as e:
                print(f"[GPIO] gpiozero init failed, falling back to keyboard: {e}")
        if not self._using_gpio:
            print("[GPIO] Keyboard fallback: press 't' (TOP), 'b' (BOTTOM), 'q' to quit")
            th = threading.Thread(target=self._keyboard_loop, daemon=True)
            th.start()
            self._threads.append(th)

    def _keyboard_loop(self):
        try:
            import termios, tty
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                while True:
                    ch = sys.stdin.read(1)
                    if ch == 't':
                        self.on_top()
                    elif ch == 'b':
                        self.on_bottom()
                    elif ch == 'q':
                        os._exit(0)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        except Exception:
            # Very minimal fallback
            while True:
                s = input().strip().lower()
                if s == 't':
                    self.on_top()
                elif s == 'b':
                    self.on_bottom()
                elif s == 'q':
                    os._exit(0)

################################################################################
# Core state machine
################################################################################

class Assistant:
    IDLE = 'IDLE'
    AWAIT_TIME = 'AWAIT_TIME'
    SCHEDULED = 'SCHEDULED'
    RINGING = 'RINGING'
    SNOOZE = 'SNOOZE'

    def __init__(self):
        self.state = self.IDLE
        self.display = Display()
        self.speaker = Speaker()
        self.listener = None
        if _ASR_AVAILABLE and os.path.isdir(MODEL_DIR):
            try:
                self.listener = Listener(MODEL_DIR)
            except Exception as e:
                print(f"[ASR] Disabled (init error): {e}")
                self.listener = None
        else:
            print("[ASR] Disabled (libraries missing or model not found)")

        self.event_q: queue.Queue[tuple[str, object | None]] = queue.Queue()
        self.schedule_dt: datetime | None = None
        self._timer_thread: threading.Thread | None = None
        self._ring_window_deadline: float | None = None
        self._reprompt_count = 0

        # Buttons
        self.buttons = ButtonDriver(TOP_BUTTON_PIN, BOTTOM_BUTTON_PIN,
                                    on_top=self._on_top_press,
                                    on_bottom=self._on_bottom_press)

    # ----------------------- Button event handlers -----------------------
    def _on_top_press(self):
        self.event_q.put(("BTN_TOP", None))

    def _on_bottom_press(self):
        self.event_q.put(("BTN_BOTTOM", None))

    # ----------------------- Public run loop ----------------------------
    def run(self):
        self.display.show_idle()
        print("[STATE] IDLE — waiting for TOP button")
        try:
            while True:
                try:
                    evt, data = self.event_q.get(timeout=0.2)
                except queue.Empty:
                    # Check timers
                    self._poll_timers()
                    continue
                self._dispatch(evt, data)
            # end while
        except KeyboardInterrupt:
            print("\n[EXIT] KeyboardInterrupt")

    # ----------------------- Event dispatch -----------------------------
    def _dispatch(self, evt: str, data):
        print(f"[EVT] {evt} in {self.state}")
        if evt == "BTN_TOP":
            self._handle_top()
        elif evt == "BTN_BOTTOM":
            self._handle_bottom()
        elif evt == "TIME_REACHED":
            self._handle_time_reached()
        elif evt == "WINDOW_TIMEOUT":
            self._handle_window_timeout()
        elif evt == "SNOOZE_TIMEOUT":
            self._handle_snooze_timeout()

    # ----------------------- Timer poller -------------------------------
    def _poll_timers(self):
        # For ring window timeout
        if self.state == self.RINGING and self._ring_window_deadline is not None:
            if time.time() >= self._ring_window_deadline:
                self._ring_window_deadline = None
                self.event_q.put(("WINDOW_TIMEOUT", None))
        # No busy wait for scheduled T — a dedicated timer thread handles it

    # ----------------------- State handlers -----------------------------
    def _handle_top(self):
        if self.state == self.IDLE:
            # Start a new plan
            self.speaker.say("Starting a new medication plan. When should I remind you?")
            self.display.show_listening()
            self.state = self.AWAIT_TIME
            self._reprompt_count = 0
            self._capture_and_schedule()
        elif self.state == self.RINGING:
            # Snooze 30 s
            self.speaker.say("Okay, I’ll remind you again in 30 seconds.")
            self.display.show_snoozed()
            self._start_snooze_timer()
            self.state = self.SNOOZE
        else:
            # Ignore or soft feedback
            pass

    def _handle_bottom(self):
        if self.state == self.RINGING:
            self.speaker.say("Good job. Finished!")
            self.display.show_finished()
            # Reset
            self.schedule_dt = None
            self.state = self.IDLE
            self.display.show_idle()
        else:
            # Ignore when not ringing
            pass

    def _handle_time_reached(self):
        if self.state == self.SCHEDULED:
            self._ring_once()
            self.state = self.RINGING
        # else ignore stale

    def _handle_window_timeout(self):
        if self.state == self.RINGING:
            # Auto-snooze
            self._start_snooze_timer()
            self.state = self.SNOOZE

    def _handle_snooze_timeout(self):
        if self.state == self.SNOOZE:
            self._ring_once()
            self.state = self.RINGING

    # ----------------------- Helpers ------------------------------------
    def _capture_and_schedule(self):
        utter = None
        if self.listener is not None:
            utter = self.listener.listen(max_sec=ASR_LISTEN_WINDOW_SEC)
        else:
            # Fallback to typed input
            print("[INPUT] Type time (e.g., 'in 3 minutes', 'at 8:05 pm'): ", end="", flush=True)
            try:
                utter = input().strip()
            except EOFError:
                utter = None

        dt, delta_str = parse_time_expr(utter or "")
        if dt is None:
            self._reprompt_count += 1
            if self._reprompt_count <= 1:
                self.speaker.say("Sorry, I didn’t catch the time. Please say it again.")
                self.display.show_listening()
                # Try one more capture
                if self.listener is not None:
                    utter = self.listener.listen(max_sec=ASR_LISTEN_WINDOW_SEC)
                else:
                    print("[INPUT] Type time again: ", end="", flush=True)
                    try:
                        utter = input().strip()
                    except EOFError:
                        utter = None
                dt, delta_str = parse_time_expr(utter or "")
            # If still invalid, cancel
            if dt is None:
                self.speaker.say("I still couldn’t parse that time. Let’s try again later.")
                self.display.show_error("Parse failed")
                time.sleep(1.0)
                self.display.show_idle()
                self.state = self.IDLE
                return

        # Valid schedule
        self.schedule_dt = dt
        if delta_str and _is_relative(utter):
            self.speaker.say(f"Got it. I’ll remind you in {delta_str}.")
        else:
            at_str = dt.astimezone(LOCAL_TZ).strftime("%I:%M %p").lstrip('0') if (LOCAL_TZ and dt.tzinfo) else dt.strftime("%H:%M")
            self.speaker.say(f"Got it. I’ll remind you at {at_str}.")
        self.display.show_scheduled(dt)
        self.state = self.SCHEDULED
        self._start_schedule_timer()

    def _start_schedule_timer(self):
        # Cancel previous timer thread if any
        if self._timer_thread and self._timer_thread.is_alive():
            # We don't have a direct cancel; just let it exit when time reached
            pass
        target = self.schedule_dt
        if target is None:
            return
        def waiter():
            while True:
                if self.state != self.SCHEDULED:
                    return
                now = now_tz()
                if now >= target:
                    self.event_q.put(("TIME_REACHED", None))
                    return
                # sleep a bit
                time.sleep(0.2)
        self._timer_thread = threading.Thread(target=waiter, daemon=True)
        self._timer_thread.start()

    def _ring_once(self):
        self.display.show_ringing()
        self.speaker.say("It’s time to take your medicine.")
        # Start 30 s ring window
        self._ring_window_deadline = time.time() + RING_WINDOW_SEC

    def _start_snooze_timer(self):
        def snoozer():
            time.sleep(SNOOZE_SEC)
            self.event_q.put(("SNOOZE_TIMEOUT", None))
        threading.Thread(target=snoozer, daemon=True).start()


################################################################################
# Helpers
################################################################################

def _is_relative(utter: str | None) -> bool:
    if not utter:
        return False
    return bool(re.search(r"\b(in|after)\b", utter, re.IGNORECASE))


################################################################################
# Entrypoint
################################################################################

def main():
    print("\n=== Medication Voice Assistant ===")
    print(f"WORKDIR: {WORKDIR}")
    print(f"Vosk model: {MODEL_DIR} ({'ok' if os.path.isdir(MODEL_DIR) else 'missing'})")
    print(f"Display available: {_DISPLAY_AVAILABLE}")
    print(f"ASR available: {_ASR_AVAILABLE}")
    print(f"GPIO pins: TOP={TOP_BUTTON_PIN} BOTTOM={BOTTOM_BUTTON_PIN}")
    print("----------------------------------\n")

    app = Assistant()
    app.run()


if __name__ == "__main__":
    main()
