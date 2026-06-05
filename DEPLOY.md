# HireLab Screener — GitHub + Railway Deployment Guide

---

## PART 1: Push to GitHub

### Step 1 — Install Git (if not already installed)
Download from: https://git-scm.com/download/win  
Accept all defaults during install. Restart your terminal/command prompt after.

### Step 2 — Create a GitHub account
Go to https://github.com and sign up (free).

### Step 3 — Create a new GitHub repository
1. Click the **+** icon (top right) → **New repository**
2. Name it: `hirelab-screener`
3. Set it to **Private** (recommended — your code stays hidden)
4. Do NOT check "Add README" or any other options
5. Click **Create repository**
6. GitHub will show you a page with commands — keep this page open

### Step 4 — Open Command Prompt in the project folder
1. Open the `hirelab-screener` folder in File Explorer
2. Click the address bar at the top, type `cmd`, press Enter
   — This opens Command Prompt directly in that folder

### Step 5 — Push your code to GitHub
Run these commands one by one (paste into the Command Prompt):

```
git init
git add .
git commit -m "Initial deploy"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/hirelab-screener.git
git push -u origin main
```

> Replace `YOUR_USERNAME` with your actual GitHub username.  
> GitHub will ask for your username and password (use a Personal Access Token as password — see below).

### Getting a GitHub Personal Access Token (for password)
1. GitHub → Settings → Developer Settings → Personal Access Tokens → Tokens (classic)
2. Click **Generate new token (classic)**
3. Give it a name, set expiry to 90 days, check **repo** scope
4. Copy the token — use it as your password when Git asks

---

## PART 2: Deploy on Railway

### Step 1 — Create Railway account
Go to https://railway.app and sign up using your GitHub account.

### Step 2 — Create a new project
1. Click **New Project**
2. Select **Deploy from GitHub repo**
3. Authorize Railway to access your GitHub
4. Select `hirelab-screener`

### Step 3 — Set Environment Variables (CRITICAL)
In Railway dashboard → your service → **Variables** tab, add:

| Variable          | Value                        | Notes                              |
|-------------------|------------------------------|------------------------------------|
| `SECRET_KEY`      | any long random string       | e.g. `hirelab-xK9mP2qZ8nR5vT7w`   |
| `APP_PASSWORD`    | your chosen login password   | Users will type this to log in     |
| `DATA_DIR`        | `/data`                      | Points to persistent volume        |

> Do NOT put your API keys (Claude, DeepSeek, OpenAI) in Railway env vars.  
> Those are entered inside the app's Settings page after login.

### Step 4 — Add a Persistent Volume (for your data)
Without this, your database resets every deploy!

1. Railway dashboard → your service → **Volumes** tab
2. Click **Add Volume**
3. Mount path: `/data`
4. Click **Create**

### Step 5 — Deploy
Railway auto-deploys when you push to GitHub. You'll see a build log.  
Once it says **Deployed**, click the generated URL (e.g. `hirelab-screener.up.railway.app`).

---

## PART 3: After Deployment

1. Open your Railway URL in a browser
2. Enter your `APP_PASSWORD` to log in
3. Go to ⚙️ **Settings** inside the app
4. Add your API keys:
   - **Claude API Key** — from https://console.anthropic.com/api_keys
   - **DeepSeek API Key** — from https://platform.deepseek.com/api_keys
   - **OpenAI API Key** — from https://platform.openai.com/api_keys (for call recording)
5. Save settings — you're live!

---

## Updating Your App Later

When you make code changes:

```
git add .
git commit -m "Describe your change"
git push
```

Railway auto-detects the push and re-deploys. Your data on `/data` is untouched.

---

## Troubleshooting

**App crashes on start?**
- Check Railway logs (dashboard → Deployments → click the build → View Logs)
- Most common cause: missing `DATA_DIR` or `SECRET_KEY` env vars

**Data disappears after redeploy?**
- Make sure you added the Volume at `/data` in Railway

**Login page not showing?**
- `APP_PASSWORD` env var is empty → app runs without login (fine for private use)
- Set `APP_PASSWORD` in Railway Variables to enable the login screen

**Port errors?**
- Railway injects `$PORT` automatically — your app already uses it correctly

---

## Railway Free Plan Limits
- $5 free credit/month (enough for light use)
- App sleeps after inactivity (wakes up on first request, ~5 sec delay)
- For always-on: upgrade to Railway Hobby plan ($5/month)
