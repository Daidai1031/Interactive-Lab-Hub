#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from gtts import gTTS
import subprocess
import os
import tempfile

# 如果输入中文名字，确保输出终端支持 UTF-8，否则会报错
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 在这里改名字和语言 
NAME = "黄昀柔"   # 改名字
LANG = "zh"      # en = 英文, zh = 中文

if LANG == "zh":
    message = f"哈哈哈哈，{NAME}！欢迎回到你的树莓派，祝你实验顺利呀！"
    tts_lang = "zh-CN"
else:
    message = f"Hello, {NAME}! Welcome back to your Raspberry Pi. Have a great lab!"
    tts_lang = "en"

print(f"[hello_tts_gtts] Generating speech: {message}")

# 生成临时 mp3 文件
with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmpf:
    mp3_path = tmpf.name

try:
    # 用 gTTS 合成语音
    tts = gTTS(text=message, lang=tts_lang)
    tts.save(mp3_path)

    # 播放语音 (install: sudo apt install mpg123)
    subprocess.run(["mpg123", "-q", mp3_path], check=True)

finally:
    if os.path.exists(mp3_path):
        os.remove(mp3_path)

print("[hello_tts_gtts] Done.")
