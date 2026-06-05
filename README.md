# HireLab Screener — Setup & Update Guide

## WHERE IS YOUR DATA? (Read this first)

Your data is NEVER inside the app folder. It is stored here:

  Windows:   C:\Users\YourName\HireLab\
  Mac/Linux: ~/HireLab/

This means:
  - You can delete the hirelab-screener/ folder anytime
  - You can extract a new version anywhere
  - Your data will NEVER be affected by updates

---

## First Time Setup

1. Extract ZIP anywhere (Desktop, D:\, anywhere)
2. Double-click Start.bat
3. Open browser: http://localhost:5000
4. Click gear icon (Settings) → add Claude API Key and DeepSeek API Key

---

## How to UPDATE (Important — read carefully)

STEP 1: Click Backup.bat (always do this first — takes 2 seconds)

STEP 2: Download new ZIP from Claude

STEP 3: Extract new ZIP — it will overwrite hirelab-screener/ folder
         Your data at C:\Users\YourName\HireLab\ is untouched

STEP 4: Double-click Start.bat — done, your data is all there

---

## Files in this folder (app code only — safe to overwrite)

  server.py        - Backend server
  index.html       - App interface
  Start.bat        - Launch the app
  Backup.bat       - Manual backup before updates
  requirements.txt - Python packages

## Your data (NEVER touched by updates)

  C:\Users\YourName\HireLab\hirelab.db     - All your data
  C:\Users\YourName\HireLab\cvs\           - CV files
  C:\Users\YourName\HireLab\backups\       - Auto backups (last 7 days)

---

## Getting API Keys

Claude:   https://console.anthropic.com/api_keys
DeepSeek: https://platform.deepseek.com/api_keys (10x cheaper, for candidate parsing)
