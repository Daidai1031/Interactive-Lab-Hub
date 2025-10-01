#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Simple Ollama Demo for Lab 3
Basic example of integrating Ollama with voice I/O

This script demonstrates:
1. Text input to Ollama
2. Voice input to Ollama  
3. Voice output from Ollama
"""

import requests
import json
import subprocess
import sys
import os
import time

# Set UTF-8 encoding for output
if sys.stdout.encoding != 'UTF-8':
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
if sys.stderr.encoding != 'UTF-8':
    import codecs
    sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')

def speak_text(text):
    """Simple text-to-speech using espeak"""
    # Clean text to avoid encoding issues
    clean_text = text.encode('ascii', 'ignore').decode('ascii')
    print(f"Assistant: {clean_text}")
    subprocess.run(['espeak', f'"{clean_text}"'], shell=True, check=False)

def query_ollama(prompt, model="phi3:mini", timeout=60):
    """Short & fast reply from Ollama"""
    try:
        t0 = time.perf_counter()
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": model,
                # ① 提示词里直接要求简短
                "prompt": f"Answer concisely in <= 20 words. {prompt}",
                "stream": False,
                # ② 控制生成参数（让回答短、稳定、快）
                "options": {
                    "num_predict": 60,     # 生成上限（越小越短）
                    "temperature": 0.2,    # 更确定
                    "top_p": 0.9,
                    "top_k": 40,
                    "repeat_penalty": 1.1  # 减少啰嗦
                },
                # （可选）遇到空行停止，进一步缩短
                "stop": ["\n\n"]
            },
            timeout=timeout
        )
        elapsed = time.perf_counter() - t0
        print(f"[query_ollama] elapsed: {elapsed:.2f}s (timeout={timeout}s)")
        if response.status_code == 200:
            return response.json().get('response', 'No response')
        else:
            return f"Error: HTTP {response.status_code} – {response.text[:200]}"
    except Exception as e:
        return f"Error: {e}"

def text_chat_demo():
    """Simple text-based chat with Ollama"""
    print("\n=== TEXT CHAT DEMO ===")
    print("Type 'quit' to exit")
    
    while True:
        user_input = input("\nYou: ")
        if user_input.lower() in ['quit', 'exit']:
            break
            
        print("Thinking...")
        response = query_ollama(user_input)
        print(f"Ollama: {response}")

def voice_response_demo():
    """Demo: Text input, voice output"""
    print("\n=== VOICE RESPONSE DEMO ===")
    print("Type your message, Ollama will respond with voice")
    print("Type 'quit' to exit")
    
    while True:
        user_input = input("\nYour message: ")
        if user_input.lower() in ['quit', 'exit']:
            break
            
        print("Thinking...")
        response = query_ollama(user_input)
        speak_text(response)

def check_ollama():
    """Check if Ollama is running and model is available"""
    try:
        response = requests.get("http://localhost:11434/api/tags")
        if response.status_code == 200:
            models = response.json().get('models', [])
            model_names = [m['name'] for m in models]
            print(f"Ollama is running. Available models: {model_names}")
            return True
        else:
            print("Ollama is not responding")
            return False
    except Exception as e:
        print(f"Cannot connect to Ollama: {e}")
        print("Make sure Ollama is running with: ollama serve")
        return False

def main():
    """Main demo menu"""
    print("Ollama Lab 3 Demo")
    print("=" * 30)
    
    # Check Ollama connection
    if not check_ollama():
        return
    
    while True:
        print("\nChoose a demo:")
        print("1. Text Chat (type to Ollama)")
        print("2. Voice Response (Ollama speaks responses)")
        print("3. Test Ollama (simple query)")
        print("4. Exit")
        
        choice = input("\nEnter choice (1-4): ")
        
        if choice == "1":
            text_chat_demo()
        elif choice == "2":
            voice_response_demo()
        elif choice == "3":
            response = query_ollama("Say hello and introduce yourself briefly")
            print(f"Ollama: {response}")
        elif choice == "4":
            print("Goodbye!")
            break
        else:
            print("Invalid choice. Please try again.")

if __name__ == "__main__":
    main()