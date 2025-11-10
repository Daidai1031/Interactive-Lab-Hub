#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Medication Voice Assistant (v2)
- ASCII-safe logs/strings (fixes UnicodeEncodeError on non-UTF8 consoles)
- Button backend: try gpiozero first; if GPIO busy (PiTFT overlay), fall back to evdev
  reading of /dev/input events (gpio-keys). If both unavailable, fallback to keyboard.
- Display init keeps trying ST7789; if GPIO pins are claimed by overlay, skip cleanly.

Run directory:
  /home/pi/Interactive-Lab-Hub/Lab 3/speech-scripts

Recommended extra deps:
  sudo apt update && sudo apt install -y espeak-ng portaudio19-dev libasound2-dev python3-evdev
  pip install vosk sounddevice dateparser gpiozero pillow pyttsx3 evdev adafruit-circuitpython-rgb-display adafruit-blinka

Env toggles:
  USE_DISPLAY=0          # force-disable TFT usage
  BUTTON_BACKEND=evdev   # force evdev / gpio / keyboard
  TOP_BUTTON_PIN=23      # BCM pin when using gpio backend
  BOTTOM_BUTTON_PIN=24   # BCM pin when using gpio backend
  TZ=America/New_York    # timezone name for scheduling

Keyboard fallback: 't' = TOP, 'b' = BOTTOM, 'q' = quit
"""

import os, sys, time, re, queue, threading, subprocess
from datetime import datetime, timedelta

# Try to force UTF-8 console where supported to avoid UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding='utf-8')  # py3.7+
except Exception:
    pass

# ---- Optional imports with graceful fallback ----
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

try:
    import dateparser
except Exception:
    dateparser = None

try:
    import sounddevice as sd
    from vosk import Model, KaldiRecognizer
    _ASR_AVAILABLE = True
except Exception:
    sd = None
    Model = None
    KaldiRecognizer = None
    _ASR_AVAILABLE = False

try:
    from gpiozero import Button
except Exception:
    Button = None

try:
    from evdev import InputDevice, categorize, ecodes, list_devices
    _EVDEV = True
except Exception:
    _EVDEV = False

# Display stack (optional)
_USE_DISPLAY = os.getenv('USE_DISPLAY', '1') != '0'
_DISPLAY_AVAILABLE = _USE_DISPLAY
try:
    import board, digitalio
    from PIL import Image, ImageDraw, ImageFont
    import adafruit_rgb_display.st7789 as st7789
except Exception:
    _DISPLAY_AVAILABLE = False
    board = digitalio = Image = ImageDraw = ImageFont = st7789 = None

try:
    import pyttsx3
except Exception:
    pyttsx3 = None

# ---- Config ----
WORKDIR = os.path.abspath(os.path.dirname(__file__))
MODEL_DIR = os.path.join(WORKDIR, 'models', 'vosk-en')
if not os.path.isdir(MODEL_DIR):
    alt = os.path.join(WORKDIR, '..', 'speech-scripts', 'models', 'vosk-en')
    if os.path.isdir(alt):
        MODEL_DIR = alt

TOP_BUTTON_PIN = int(os.getenv('TOP_BUTTON_PIN', '23'))
BOTTOM_BUTTON_PIN = int(os.getenv('BOTTOM_BUTTON_PIN', '24'))
BUTTON_BACKEND = os.getenv('BUTTON_BACKEND', '').strip().lower()  # '', 'gpio', 'evdev', 'keyboard'
BUTTON_DEBOUNCE_S = 0.15

RING_WINDOW_SEC = 30
SNOOZE_SEC = 30
MIN_DELAY_SEC = 5
MAX_DELAY_SEC = 24 * 3600

ASR_SAMPLE_RATE = 16000
ASR_LISTEN_WINDOW_SEC = 8

LOCAL_TZ_NAME = os.getenv('TZ', 'America/New_York')
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME) if ZoneInfo else None

# ---- Utilities ----

def now_tz():
    if LOCAL_TZ:
        return datetime.now(tz=LOCAL_TZ)
    return datetime.now()

def humanize_delta(seconds:int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        if m:
            return f"{h} hour{'s' if h!=1 else ''} {m} minute{'s' if m!=1 else ''}"
        return f"{h} hour{'s' if h!=1 else ''}"
    if m:
        return f"{m} minute{'s' if m!=1 else ''}"
    return f"{s} second{'s' if s!=1 else ''}"

def clamp_delay(seconds:int) -> int:
    return max(MIN_DELAY_SEC, min(MAX_DELAY_SEC, seconds))

# ---- TTS ----
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
            subprocess.run(['espeak-ng', '--version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except FileNotFoundError:
            return False

    def say(self, text:str, rate_wpm:int=170):
        text = (text or '').encode('ascii', errors='ignore').decode('ascii')  # ASCII-safe
        if not text:
            return
        print('[TTS] ' + text)
        if self._has_espeak:
            try:
                subprocess.run(['espeak-ng', '-s', str(rate_wpm), text], check=False)
            except Exception as e:
                print('[TTS] espeak-ng error:', e)
        elif self._tts_engine is not None:
            try:
                self._tts_engine.setProperty('rate', rate_wpm)
                self._tts_engine.say(text)
                self._tts_engine.runAndWait()
            except Exception as e:
                print('[TTS] pyttsx3 error:', e)

# ---- Display ----
class Display:
    WIDTH=240; HEIGHT=135
    def __init__(self):
        self.available = _DISPLAY_AVAILABLE
        self._disp = None
        if not self.available:
            return
        try:
            cs_pin = digitalio.DigitalInOut(board.CE0)
            dc_pin = digitalio.DigitalInOut(board.D25)
            reset_pin = digitalio.DigitalInOut(board.D24)  # may be busy if kernel overlay owns it
            spi = board.SPI()
            self._disp = st7789.ST7789(
                spi, cs=cs_pin, dc=dc_pin, rst=reset_pin,
                baudrate=64000000, width=self.WIDTH, height=self.HEIGHT,
                x_offset=53, y_offset=40, rotation=270,
            )
            self._image = Image.new('RGB', (self.WIDTH, self.HEIGHT))
            self._draw = ImageDraw.Draw(self._image)
            try:
                self._font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 16)
            except Exception:
                self._font = ImageFont.load_default()
            self._clear((0,0,0))
        except Exception as e:
            print("[DISPLAY] Init failed, console fallback:", e)
            self.available=False

    def _clear(self, color=(0,0,0)):
        if not self.available: return
        self._draw.rectangle((0,0,self.WIDTH,self.HEIGHT), fill=color)
        self._disp.image(self._image)

    def _write_center(self, lines, fill=(255,255,255)):
        # Always print to console too (ASCII-safe)
        for ln in lines:
            safe = (ln or '').encode('ascii', errors='ignore').decode('ascii')
            print('[DISPLAY]', safe)
        if not self.available: return
        self._clear((0,0,0))
        total_h=0; sizes=[]
        for ln in lines:
            w,h = self._draw.textsize(ln, font=self._font)
            sizes.append((w,h)); total_h+=h
        y=(self.HEIGHT-total_h)//2
        for (ln,(w,h)) in zip(lines,sizes):
            x=(self.WIDTH-w)//2
            self._draw.text((x,y), ln, font=self._font, fill=fill)
            y+=h+2
        self._disp.image(self._image)

    def show_idle(self):
        self._write_center(['Medication Assistant','Press TOP to start'], fill=(180,220,255))
    def show_listening(self):
        self._write_center(['Listening...','Say a time'], fill=(255,255,180))
    def show_scheduled(self, dt:datetime):
        tstr = dt.astimezone(LOCAL_TZ).strftime('%H:%M:%S') if (LOCAL_TZ and dt.tzinfo) else dt.strftime('%H:%M:%S')
        self._write_center(['Scheduled for', tstr], fill=(180,255,200))
    def show_ringing(self):
        self._write_center(['Time to take','medicine'], fill=(255,200,200))
    def show_snoozed(self):
        self._write_center(['Snoozed','30 s'], fill=(200,220,255))
    def show_finished(self):
        self._write_center(['Taken','OK'], fill=(200,255,200))
    def show_error(self, msg:str):
        self._write_center(['Error', (msg or '')], fill=(255,180,180))

# ---- ASR ----
class Listener:
    def __init__(self, model_dir:str):
        if not _ASR_AVAILABLE:
            raise RuntimeError('ASR unavailable')
        if not os.path.isdir(model_dir):
            raise RuntimeError('Vosk model not found: ' + model_dir)
        self._model = Model(model_dir)
    def listen(self, max_sec:int) -> str | None:
        rec = KaldiRecognizer(self._model, ASR_SAMPLE_RATE)
        rec.SetWords(True)
        result_text=None
        def callback(indata, frames, time_info, status):
            nonlocal result_text
            if rec.AcceptWaveform(indata):
                result_text=_extract_text(rec.Result())
        with sd.RawInputStream(samplerate=ASR_SAMPLE_RATE, blocksize=8000,
                               dtype='int16', channels=1, callback=callback):
            start=time.time()
            while (time.time()-start)<max_sec and result_text is None:
                time.sleep(0.05)
            if result_text is None:
                try:
                    result_text=_extract_text(rec.FinalResult()) or None
                except Exception:
                    pass
        if result_text:
            print('[ASR] Heard:', result_text)
        else:
            print('[ASR] No speech recognized')
        return result_text

def _extract_text(vosk_json:str) -> str:
    try:
        import json
        txt=(json.loads(vosk_json).get('text') or '').strip().lower()
        return txt
    except Exception:
        return ''

# ---- Time parsing ----
_ABS_AT_RE = re.compile(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)
_REL_IN_RE = re.compile(r"\b(?:in|after)\s+(.*)", re.I)

WORDNUM = {
    'zero':0,'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,
    'eight':8,'nine':9,'ten':10,'eleven':11,'twelve':12,'thirteen':13,
    'fourteen':14,'fifteen':15,'twenty':20,'thirty':30,'forty':40,'fifty':50
}

def parse_time_expr(utter:str) -> tuple[datetime | None, str | None]:
    utter=(utter or '').strip().lower()
    if not utter:
        return None,None
    now=now_tz()
    m=_REL_IN_RE.search(utter)
    if m:
        frag=m.group(1).strip()
        seconds=_parse_relative_to_seconds(frag)
        if seconds:
            seconds=clamp_delay(seconds)
            dt=now+timedelta(seconds=seconds)
            return dt, humanize_delta(seconds)
    m=_ABS_AT_RE.search(utter)
    if m:
        hh=int(m.group(1)); mm=int(m.group(2)) if m.group(2) else 0
        ampm=(m.group(3) or '').lower()
        if ampm=='pm' and hh!=12: hh+=12
        if ampm=='am' and hh==12: hh=0
        cand=now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= now + timedelta(seconds=1):
            cand += timedelta(days=1)
        delta=int((cand-now).total_seconds())
        delta=clamp_delay(delta)
        return cand, humanize_delta(delta)
    if dateparser is not None:
        dp=dateparser.parse(utter, languages=['en'], settings={
            'PREFER_DATES_FROM':'future', 'RELATIVE_BASE': now,
            'RETURN_AS_TIMEZONE_AWARE': bool(LOCAL_TZ), 'TIMEZONE': LOCAL_TZ_NAME if LOCAL_TZ else None
        })
        if dp:
            if LOCAL_TZ and dp.tzinfo is None:
                dp=dp.replace(tzinfo=LOCAL_TZ)
            delta=int((dp-now).total_seconds())
            if delta<=0:
                dp=now+timedelta(seconds=MIN_DELAY_SEC)
                delta=MIN_DELAY_SEC
            delta=clamp_delay(delta)
            return dp, humanize_delta(delta)
    return None,None

def _parse_relative_to_seconds(text:str) -> int | None:
    txt=text.replace('seconds','sec').replace('second','sec')
    txt=txt.replace('minutes','min').replace('minute','min')
    txt=txt.replace('hours','h').replace('hour','h')
    pattern=re.compile(r"(\d+)\s*(h|hr|hrs|min|m|sec|s)", re.I)
    total=0; found=False
    for n,unit in pattern.findall(txt):
        n=int(n); unit=unit.lower()
        if unit in ('h','hr','hrs'): total+=n*3600
        elif unit in ('min','m'): total+=n*60
        elif unit in ('sec','s'): total+=n
        found=True
    if found and total>0: return total
    m=re.search(r"(zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|twenty|thirty|forty|fifty)\s+(seconds?|minutes?|hours?)", text)
    if m:
        n=WORDNUM.get(m.group(1),0); unit=m.group(2)
        if unit.startswith('hour'): return n*3600
        if unit.startswith('minute'): return n*60
        if unit.startswith('second'): return n
    return None

# ---- Button backends ----
class ButtonDriver:
    def __init__(self, on_top, on_bottom):
        self.on_top = on_top; self.on_bottom = on_bottom
        forced = BUTTON_BACKEND in ('gpio','evdev','keyboard')
        if BUTTON_BACKEND=='gpio' or (not forced and self._try_gpio()):
            return
        if BUTTON_BACKEND=='evdev' or (not forced and self._try_evdev()):
            return
        print("[GPIO] Keyboard fallback: press 't' (TOP), 'b' (BOTTOM), 'q' to quit")
        th = threading.Thread(target=self._keyboard_loop, daemon=True)
        th.start()

    def _try_gpio(self) -> bool:
        if Button is None: return False
        try:
            self._btn_top = Button(TOP_BUTTON_PIN, pull_up=True, bounce_time=BUTTON_DEBOUNCE_S)
            self._btn_bottom = Button(BOTTOM_BUTTON_PIN, pull_up=True, bounce_time=BUTTON_DEBOUNCE_S)
            self._btn_top.when_pressed = lambda: self.on_top()
            self._btn_bottom.when_pressed = lambda: self.on_bottom()
            print(f"[GPIO] Using gpiozero TOP={TOP_BUTTON_PIN} BOTTOM={BOTTOM_BUTTON_PIN}")
            return True
        except Exception as e:
            print('[GPIO] gpiozero init failed:', e)
            return False

    def _try_evdev(self) -> bool:
        if not _EVDEV: return False
        paths = list_devices()
        if not paths:
            return False
        # Heuristic: pick gpio-keys like devices
        candidates = []
        for p in paths:
            try:
                dev = InputDevice(p)
                name = dev.name.lower()
                if 'gpio' in name or 'keys' in name or 'tft' in name or 'adafruit' in name:
                    candidates.append(p)
            except Exception:
                pass
        if not candidates:
            candidates = paths[:1]
        print('[GPIO] Using evdev on:', candidates)
        def reader(p):
            try:
                dev = InputDevice(p)
                for event in dev.read_loop():
                    if event.type == ecodes.EV_KEY:
                        key = event.code; val = event.value  # 1=down, 0=up
                        if val != 1:  # only on key-down
                            continue
                        # Map common keys
                        if key in (ecodes.KEY_UP, ecodes.KEY_ENTER, ecodes.KEY_VOLUMEUP, ecodes.KEY_KPENTER):
                            self.on_top()
                        elif key in (ecodes.KEY_DOWN, ecodes.KEY_VOLUMEDOWN, ecodes.KEY_PAGEDOWN, ecodes.KEY_NEXT):
                            self.on_bottom()
                        # Debug
                        print('[EVDEV] key code', key)
            except Exception as e:
                print('[EVDEV] reader error:', e)
        for p in candidates:
            threading.Thread(target=reader, args=(p,), daemon=True).start()
        return True

    def _keyboard_loop(self):
        try:
            import termios, tty
            fd = sys.stdin.fileno(); old = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                while True:
                    ch = sys.stdin.read(1)
                    if ch == 't': self.on_top()
                    elif ch == 'b': self.on_bottom()
                    elif ch == 'q': os._exit(0)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            while True:
                s = input().strip().lower()
                if s=='t': self.on_top()
                elif s=='b': self.on_bottom()
                elif s=='q': os._exit(0)

# ---- Core Assistant ----
class Assistant:
    IDLE='IDLE'; AWAIT_TIME='AWAIT_TIME'; SCHEDULED='SCHEDULED'; RINGING='RINGING'; SNOOZE='SNOOZE'
    def __init__(self):
        self.display = Display()
        self.speaker = Speaker()
        self.listener = Listener(MODEL_DIR) if _ASR_AVAILABLE and os.path.isdir(MODEL_DIR) else None
        self.state=self.IDLE
        self.event_q: queue.Queue[tuple[str, object | None]] = queue.Queue()
        self.schedule_dt: datetime | None = None
        self._ring_deadline: float | None = None
        self._timer_thread=None
        self._reprompt=0
        self.buttons = ButtonDriver(self._on_top, self._on_bottom)

    def run(self):
        print('=== Medication Voice Assistant (v2) ===')
        print('WORKDIR:', WORKDIR)
        print('Vosk model:', MODEL_DIR, '(ok)' if os.path.isdir(MODEL_DIR) else '(missing)')
        print('Display enabled:', self.display.available)
        print('ASR available:', bool(self.listener))
        print('---------------------------------------')
        self.display.show_idle()
        print('[STATE] IDLE - waiting for TOP button')
        try:
            while True:
                try:
                    evt, data = self.event_q.get(timeout=0.2)
                except queue.Empty:
                    self._poll()
                    continue
                self._dispatch(evt, data)
        except KeyboardInterrupt:
            print('\n[EXIT] KeyboardInterrupt')

    def _poll(self):
        if self.state==self.RINGING and self._ring_deadline is not None:
            if time.time() >= self._ring_deadline:
                self._ring_deadline=None
                self.event_q.put(('WINDOW_TIMEOUT', None))

    def _dispatch(self, evt:str, data):
        print('[EVT]', evt, 'in', self.state)
        if evt=='BTN_TOP': self._on_top()
        elif evt=='BTN_BOTTOM': self._on_bottom()
        elif evt=='TIME_REACHED': self._on_time_reached()
        elif evt=='WINDOW_TIMEOUT': self._on_window_timeout()
        elif evt=='SNOOZE_TIMEOUT': self._on_snooze_timeout()

    def _on_top(self):
        if self.state==self.IDLE:
            self.speaker.say("Starting a new medication plan. When should I remind you?")
            self.display.show_listening()
            self.state=self.AWAIT_TIME; self._reprompt=0
            self._capture_and_schedule()
        elif self.state==self.RINGING:
            self.speaker.say("Okay, I'll remind you again in 30 seconds.")
            self.display.show_snoozed()
            self._start_snooze()
            self.state=self.SNOOZE

    def _on_bottom(self):
        if self.state==self.RINGING:
            self.speaker.say('Good job. Finished!')
            self.display.show_finished()
            self.schedule_dt=None
            self.state=self.IDLE
            self.display.show_idle()

    def _on_time_reached(self):
        if self.state==self.SCHEDULED:
            self._ring_once(); self.state=self.RINGING

    def _on_window_timeout(self):
        if self.state==self.RINGING:
            self._start_snooze(); self.state=self.SNOOZE

    def _on_snooze_timeout(self):
        if self.state==self.SNOOZE:
            self._ring_once(); self.state=self.RINGING

    def _capture_and_schedule(self):
        if self.listener is not None:
            utter=self.listener.listen(max_sec=ASR_LISTEN_WINDOW_SEC)
        else:
            try:
                utter=input("[INPUT] Type time (e.g., 'in 3 minutes', 'at 8:05 pm'): ").strip()
            except EOFError:
                utter=None
        dt, delta = parse_time_expr(utter or '')
        if dt is None:
            self._reprompt+=1
            if self._reprompt<=1:
                self.speaker.say("Sorry, I didn't catch the time. Please say it again.")
                self.display.show_listening()
                if self.listener is not None:
                    utter=self.listener.listen(max_sec=ASR_LISTEN_WINDOW_SEC)
                else:
                    try:
                        utter=input('[INPUT] Type time again: ').strip()
                    except EOFError:
                        utter=None
                dt, delta = parse_time_expr(utter or '')
            if dt is None:
                self.speaker.say("I still couldn't parse that time. Let's try again later.")
                self.display.show_error('Parse failed')
                time.sleep(1.0)
                self.display.show_idle(); self.state=self.IDLE
                return
        self.schedule_dt=dt
        if _is_relative(utter):
            self.speaker.say(f"Got it. I'll remind you in {delta}.")
        else:
            at_str = dt.astimezone(LOCAL_TZ).strftime('%I:%M %p').lstrip('0') if (LOCAL_TZ and dt.tzinfo) else dt.strftime('%H:%M')
            self.speaker.say(f"Got it. I'll remind you at {at_str}.")
        self.display.show_scheduled(dt)
        self.state=self.SCHEDULED
        self._start_schedule_waiter()

    def _start_schedule_waiter(self):
        target=self.schedule_dt
        if not target: return
        def waiter():
            while True:
                if self.state!=self.SCHEDULED: return
                if now_tz() >= target:
                    self.event_q.put(('TIME_REACHED', None)); return
                time.sleep(0.2)
        threading.Thread(target=waiter, daemon=True).start()

    def _ring_once(self):
        self.display.show_ringing()
        self.speaker.say("It's time to take your medicine.")
        self._ring_deadline = time.time() + RING_WINDOW_SEC

    def _start_snooze(self):
        def snoozer():
            time.sleep(SNOOZE_SEC)
            self.event_q.put(('SNOOZE_TIMEOUT', None))
        threading.Thread(target=snoozer, daemon=True).start()

# ---- helpers ----

def _is_relative(utter:str | None) -> bool:
    if not utter: return False
    return bool(re.search(r"\b(in|after)\b", utter, re.I))

# ---- main ----
if __name__=='__main__':
    print('=== Medication Voice Assistant ===')
    print('WORKDIR:', WORKDIR)
    print('Vosk model:', MODEL_DIR, '(ok)' if os.path.isdir(MODEL_DIR) else '(missing)')
    print('Display available:', _DISPLAY_AVAILABLE)
    print('ASR available:', _ASR_AVAILABLE)
    print('GPIO pins (gpio backend): TOP=', TOP_BUTTON_PIN, ' BOTTOM=', BOTTOM_BUTTON_PIN)
    print('----------------------------------')
    app = Assistant()
    app.run()
