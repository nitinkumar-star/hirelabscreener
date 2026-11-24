# "Connect with Google" — one-time setup (about 15 minutes, ₹0)

After this, a new user connects their Gmail in the ATS with one button:
**Settings → My Account → My Email → Connect with Google → Allow.**
No App Password and no 2-Step Verification setup.

This setup is done **once**, by the Google Workspace admin of `hirelabtalent.com`.
It is free: an *Internal* app needs no Google review.

You need your ATS address. Below it is written as `https://YOUR-ATS`
(for example `https://hirelabscreener.onrender.com`, or your own domain if you use one).

---

## Step 1 — Create a project
1. Open **https://console.cloud.google.com** and sign in with your **@hirelabtalent.com admin** account.
2. At the top left, click the **project picker**, then **New project**.
3. Project name: `HireLab ATS`. Organisation: `hirelabtalent.com`. Click **Create**.
4. Make sure the new project is selected at the top.

## Step 2 — Consent screen (who may sign in)
1. In the left menu (☰), go to **APIs & Services → OAuth consent screen**.
   (In newer screens this is called **Google Auth Platform**. Click **Get started**.)
2. **App name:** `HireLab ATS`. **User support email:** your email. Click **Next**.
3. **Audience:** choose **Internal**. This is the important one: it keeps the app to your own domain and means no Google review. Click **Next**.
4. **Contact email:** your email. Tick the agreement, then click **Create**.

## Step 3 — Permission for Gmail
1. Open **Data access** (or **Scopes**), then **Add or remove scopes**.
2. At the bottom, under **Manually add scopes**, paste:
   ```
   https://mail.google.com/
   ```
   Click **Add to table**. Also tick `openid` and `.../auth/userinfo.email` in the list.
3. Click **Update**, then **Save**.

## Step 4 — Create the login key (OAuth client)
1. Open **Clients** (or **Credentials → Create credentials → OAuth client ID**).
2. **Application type:** `Web application`. **Name:** `HireLab ATS web`.
3. Under **Authorized redirect URIs**, click **Add URI** and paste:
   ```
   https://YOUR-ATS/google-mail/callback
   ```
4. Click **Create**. A box shows the **Client ID** and **Client secret**.
   Copy both (or download the JSON). Keep the secret private and never share it in chat.

## Step 5 — Put the keys in Render
In **Render → your ATS service → Environment**, add:

| Key | Value |
|---|---|
| `GOOGLE_OAUTH_CLIENT_ID` | the Client ID from Step 4 |
| `GOOGLE_OAUTH_CLIENT_SECRET` | the Client secret from Step 4 |
| `GOOGLE_OAUTH_DOMAIN` | `hirelabtalent.com` |
| `PUBLIC_BASE_URL` | `https://YOUR-ATS` (only if it is not set already) |

Click **Save changes**. Render redeploys by itself.

## Step 6 — Try it
1. Log in to the ATS as a recruiter. Go to **Settings → My Account → My Email**, or **Email Box → Connect Gmail**.
2. Click **Connect with Google**, choose your @hirelabtalent.com account, and click **Allow**.
3. You come back to the ATS with "Connected … with Google". Send yourself a test mail.

---

## If something goes wrong
- **"Access blocked: this app is restricted to users within its organisation"**: the person used a Gmail account that is not @hirelabtalent.com. Internal apps only work for your own domain.
- **"redirect_uri_mismatch"**: the address in Step 4 must match exactly, including `https://` and `/google-mail/callback`, with no `/` at the end.
- **"This app is blocked" / admin approval needed**: in **admin.google.com → Security → Access and data control → API controls → Manage third-party app access**, find *HireLab ATS* and set it to **Trusted**.
- **Mail sends but the inbox does not sync**: in **admin.google.com → Apps → Google Workspace → Gmail → End user access**, make sure **IMAP access** is on.
- **"The Google connection was removed or has expired"**: the user removed the app from their Google account or changed their password. They just click **Connect with Google** again.

## What the ATS stores
- Only a *refresh token* is kept, encrypted in the database, in the same way App Passwords were kept. Users never give the ATS their Google password.
- **Disconnect** in Settings removes the token and also revokes it at Google.
- A user can also remove access themselves at **myaccount.google.com/permissions**.

## Cost
- **₹0.** The Google Cloud project, the OAuth client and Gmail sign-in are free.
- There is no Google review for an *Internal* app. Nothing needs a card or billing account.
- Opening the app to other agencies (*External*) later would need Google verification plus a yearly paid security assessment. That is not needed for your own team.
