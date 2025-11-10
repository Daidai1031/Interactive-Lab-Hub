# main.py  — Focus Coach (Single User, TM Image + mp3 + emoji)

import os, time, subprocess, collections
from dataclasses import dataclass
from typing import List
import cv2
import numpy as np
from PIL import Image, ImageTk
import tkinter as tk

# ========= Config =========
MODEL_PATH = "model/model.tflite"
LABELS_PATH = "model/labels.txt"

CAM_INDEX = 0
INPUT_SIZE = (224, 224)          # Teachable Machine Image model
CONF_THRESH = 0.40

SLIDE_SEC = 3                     # seconds for majority vote window
FPS_TARGET = 8

FOCUSED_CLASSES = {"typing", "writing"}
NONFOCUSED_CLASSES = {"idle", "phone", "drink"}

REWARD_STREAK = 3                 # 3 consecutive focused -> reward sound
REMIND_STREAK = 2                 # 2 consecutive non-focused -> reminder

BUCKET_SECONDS = 10                # 10-second summary icon
LOG_PATH = "session_log.csv"

# mp3 sounds
REWARD_MP3 = "sounds/reward.mp3"
REMIND_MP3 = "sounds/reminder.mp3"

# Emoji icons (no icons/ folder needed)
EMOJI = {
    "typing": "⌨️",
    "writing": "✍️",
    "drink": "🥤",
    "phone": "📱",
    "idle": "💭",
    "unknown": "❓"
}

# ========= TFLite loader =========
try:
    from tflite_runtime.interpreter import Interpreter
except ImportError:
    from tensorflow.lite.python.interpreter import Interpreter  # fallback

def load_labels(path):
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

def play_sound(path):
    """
    Play .mp3 using mpg123 (install: sudo apt install mpg123)
    """
    try:
        subprocess.Popen(["mpg123", "-q", path])
    except Exception as e:
        print("mpg123 failed:", e)

@dataclass
class StatePacket:
    ts: float
    cls: str
    conf: float

class FocusCoachApp:
    def __init__(self):
        # --- Model ---
        self.labels = load_labels(LABELS_PATH)
        self.interpreter = Interpreter(MODEL_PATH)
        self.interpreter.allocate_tensors()
        self.input_details = self.interpreter.get_input_details()
        self.output_details = self.interpreter.get_output_details()

        # --- Camera ---
        self.cap = cv2.VideoCapture(CAM_INDEX)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        # --- Smoothing & streaks ---
        self.slide = collections.deque(maxlen=SLIDE_SEC * FPS_TARGET)
        self.streak_focused = 0
        self.streak_nonfocused = 0

        # --- 5-min bucket ---
        self.bucket_start = time.time()
        self.bucket_counts = collections.Counter()
        self.timeline_icons: List[str] = []

        # --- UI (Tkinter) ---
        self.root = tk.Tk()
        self.root.title("Focus Coach (Single User)")
        self.root.geometry("420x520")

        self.video_label = tk.Label(self.root)
        self.video_label.pack(pady=4)

        self.ring = tk.Canvas(self.root, width=240, height=240, highlightthickness=0)
        self.ring.pack()

        self.icon_var = tk.StringVar(value="–")
        self.status_lbl = tk.Label(self.root, textvariable=self.icon_var, font=("Arial", 28))
        self.status_lbl.pack(pady=6)

        self.timeline_var = tk.StringVar(value="")
        self.timeline_lbl = tk.Label(self.root, textvariable=self.timeline_var, font=("Arial", 18))
        self.timeline_lbl.pack(pady=6)

        self.timer_var = tk.StringVar(value="00:00:00")
        self.timer_lbl = tk.Label(self.root, textvariable=self.timer_var, font=("Arial", 14))
        self.timer_lbl.pack(pady=2)

        self.start_time = time.time()
        self._last_log_ts = 0.0

        self.root.after(0, self.update_loop)

    # ---------- Inference ----------
    def preprocess(self, frame_bgr):
        img = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        img = cv2.resize(img, INPUT_SIZE)
        x = np.asarray(img, dtype=np.float32) / 255.0
        x = np.expand_dims(x, axis=0)
        return x

    def infer(self, frame_bgr):
        x = self.preprocess(frame_bgr)
        self.interpreter.set_tensor(self.input_details[0]['index'], x)
        self.interpreter.invoke()
        logits = self.interpreter.get_tensor(self.output_details[0]['index'])[0]
        idx = int(np.argmax(logits))
        conf = float(np.max(logits))
        label = self.labels[idx] if idx < len(self.labels) else "unknown"
        if conf < CONF_THRESH:
            label = "unknown"
        
        return label, conf

    # ---------- Smoothing & streaks ----------
    def smooth_label(self, label, conf):
        self.slide.append(StatePacket(time.time(), label, conf))
        votes = [p.cls for p in self.slide if p.cls != "unknown"]
        if not votes:
            votes = [p.cls for p in self.slide]
        if not votes:
            return "unknown"
        return collections.Counter(votes).most_common(1)[0][0]

    def update_streaks(self, majority_label):
        if majority_label in FOCUSED_CLASSES:
            self.streak_focused += 1
            self.streak_nonfocused = 0
        elif majority_label in NONFOCUSED_CLASSES:
            self.streak_nonfocused += 1
            self.streak_focused = 0
        else:
            self.streak_focused = 0
            self.streak_nonfocused = 0

        if self.streak_focused == REWARD_STREAK:
            play_sound(REWARD_MP3)
        if self.streak_nonfocused == REMIND_STREAK:
            play_sound(REMIND_MP3)

    # ---------- 5-min summary ----------
    def update_bucket(self, label):
        self.bucket_counts[label] += 1
        if time.time() - self.bucket_start >= BUCKET_SECONDS:
            c = self.bucket_counts.copy()
            if "unknown" in c and len(c) > 1:
                del c["unknown"]
            dom = c.most_common(1)[0][0] if c else "unknown"
            self.timeline_icons.append(EMOJI.get(dom, "❓"))
            self.timeline_var.set(" ".join(self.timeline_icons))
            self.bucket_start = time.time()
            self.bucket_counts = collections.Counter()

    # ---------- UI drawing ----------
    def draw_ring(self, focused: bool):
        self.ring.delete("all")
        self.ring.create_oval(10, 10, 230, 230, width=20, outline="#3a3a3a")
        color = "#27ae60" if focused else "#f39c12"  # green / orange
        self.ring.create_oval(10, 10, 230, 230, width=20, outline=color)

    def update_timer(self):
        dt = int(time.time() - self.start_time)
        h = dt // 3600
        m = (dt % 3600) // 60
        s = dt % 60
        self.timer_var.set(f"{h:02d}:{m:02d}:{s:02d}")

    # ---------- Logging ----------
    def log_once_per_sec(self, label, conf):
        now = time.time()
        if int(now) != int(self._last_log_ts):
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"{int(now)},{label},{conf:.3f}\n")
            self._last_log_ts = now

    # ---------- Main loop ----------
    def update_loop(self):
        ok, frame = self.cap.read()
        if not ok:
            self.root.after(100, self.update_loop)
            return

        label, conf = self.infer(frame)
        print(f"raw: {label} ({conf:.2f})")
        majority = self.smooth_label(label, conf)
        self.update_streaks(majority)
        self.update_bucket(majority)

        focused_now = majority in FOCUSED_CLASSES
        self.draw_ring(focused_now)

        # small preview
        disp = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        disp = cv2.resize(disp, (320, 240))
        imgtk = ImageTk.PhotoImage(image=Image.fromarray(disp))
        self.video_label.imgtk = imgtk
        self.video_label.configure(image=imgtk)

        icon = EMOJI.get(majority, "❓")
        text = "🟢 Focused" if focused_now else "🟠 Break"
        self.icon_var.set(f"{icon}  {text}")

        self.update_timer()
        self.log_once_per_sec(majority, conf)

        delay = int(1000 / FPS_TARGET)
        self.root.after(delay, self.update_loop)

    def run(self):
        self.root.mainloop()
        self.cap.release()

if __name__ == "__main__":
    # Make sure mpg123 is installed for mp3 playback:
    # sudo apt install mpg123
    app = FocusCoachApp()
    app.run()
