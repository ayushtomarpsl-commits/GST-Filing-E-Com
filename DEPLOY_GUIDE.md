# Deploy Guide — put GST Helper on the internet for free

This guide is for you if you have never deployed a website before.
Follow the steps in order. Every step is one click or one command.

The app already has a "public mode". When the server sets the environment
variable `PUBLIC_MODE=1`, the app:

- listens on the host's port (`$PORT`) instead of only your computer,
- turns off the "paste a folder path" feature (a public server cannot and
  must not read paths on a visitor's computer),
- switches the UI to the browse-folder **upload** flow,
- deletes every visitor's generated files automatically within 1 hour.

You do not need to change any code. The files `render.yaml` and
`requirements.txt` in this folder already set everything up.

---

## A. Before you publish — protect your own data (IMPORTANT)

This project folder contains your **real GST business data**: the
`tax report/` folder, generated CSV/JSON files, and zips. These must
**never** go into a public repository.

The `.gitignore` file in this folder already blocks them. Check it worked:

1. Open a terminal in this folder.
2. Run:

   ```bash
   git status
   ```

3. Look at the list of files. You should **NOT** see any of these:
   - `tax report/` (or anything inside it)
   - `flipkart_b2cs_*.csv`
   - `gstr1_*.json` or `gstr3b_*.json`
   - `_amazon_upload.xlsx`
   - any `.zip` file

   If they are missing from the list — good. `.gitignore` is hiding them
   from git on purpose.

4. **Never** run `git add -f` (force add) on those files. The `-f` flag
   overrides `.gitignore` and would publish your business data.

### One-time cleanup: 4 old data files are already in git

Earlier commits in this repo already contain these real report files:

- `GSTR return report.xlsx`
- `Sales Report.xlsx`
- `b2cs_upload.csv`
- `gstr1_b2cs.json`

`.gitignore` cannot remove files that are already tracked. Before you push,
run this once to untrack them (the files stay on your disk — only git
stops tracking them):

```bash
git rm --cached "GSTR return report.xlsx" "Sales Report.xlsx" b2cs_upload.csv gstr1_b2cs.json
git commit -m "Stop tracking personal report files"
```

Note: the old commits still hold copies of those files in the history.
That is why this guide tells you to make the GitHub repository **Private**
in Step 2 below — do that and the history is not visible to anyone else.
(The deploy steps work exactly the same with a Private repo.)

---

## B. Path 1 (recommended): Render.com

Render gives you a free, always-on-enough web service with automatic
deploys from GitHub. Total time: about 15 minutes.

### Step 1 — create a GitHub account (skip if you have one)

1. Go to https://github.com and click **Sign up**.
2. Use your email, pick a username, verify the email. It is free.

### Step 2 — push this folder to a new GitHub repository

In a terminal, inside this project folder:

```bash
git add -A
git commit -m "GST Helper - ready to deploy"
```

(`git add -A` is safe now — `.gitignore` keeps your personal report files out.
Check the `git status` output from section A one more time if unsure.)

Then create the repository on GitHub:

1. Go to https://github.com and click the **+** (top right) → **New repository**.
2. Name it, for example, `GST-Filing-E-Com`.
3. Choose **Private** (recommended — see the cleanup note in section A;
   old commits contain your report files). Do **not** tick "Add a README".
4. Click **Create repository**.
5. GitHub now shows a page titled *"…or push an existing repository from the
   command line"*. Copy those 3 commands and run them in your terminal.
   They look like this:

   ```bash
   git remote add origin https://github.com/YOURNAME/GST-Filing-E-Com.git
   git branch -M main
   git push -u origin main
   ```

### Step 3 — deploy on Render

1. Go to https://render.com and click **Get Started** → sign up with
   **GitHub** (one click, free, no card needed).
2. In the Render dashboard click **New +** → **Blueprint**.
3. Connect your GitHub account if asked, then pick your
   `GST-Filing-E-Com` repository.
4. Render reads the `render.yaml` file automatically and shows one web
   service named **gst-helper**. Click **Deploy** (or **Apply**).
5. Wait 2–5 minutes while it installs and starts.

### Step 4 — open your site

Your site is live at:

```
https://gst-helper.onrender.com
```

(The name comes from `name: gst-helper` in `render.yaml`. If that name is
already taken by someone else, Render lets you pick another — the URL becomes
`https://<your-name>.onrender.com`.)

### Honest limits of the free plan

- **Sleeping:** if nobody visits for about 15 minutes, the service goes to
  sleep. The next visitor waits about **50 seconds** while it wakes up.
  After that it is fast again. This is normal on the free plan.
- **Hours:** you get 750 free hours per month — enough to keep this one
  service available all month.
- **Auto-deploy:** every time you `git push`, Render rebuilds and redeploys
  the site by itself.

---

## C. Path 2 (alternative): PythonAnywhere

PythonAnywhere's free "Beginner" plan also works and never sleeps, but needs
a small manual renewal every 3 months (see limits below).

1. Go to https://www.pythonanywhere.com → **Pricing & signup** →
   **Create a Beginner account** (free, no card).
2. After login, open **Consoles** → **Bash**.
3. In the Bash console, clone your GitHub repo and set up Python:

   ```bash
   git clone https://github.com/YOURNAME/GST-Filing-E-Com.git
   mkvirtualenv --python=python3.12 gst
   cd GST-Filing-E-Com
   pip install -r requirements.txt
   ```

   (If `python3.12` is not offered, use the newest version they list,
   e.g. `--python=python3.11`.)
4. Go to the **Web** tab → **Add a new web app** → click through →
   choose **Manual configuration** → pick the same Python version.
5. On the Web tab page:
   - **Virtualenv:** enter `gst` (it fills in the full path itself).
   - **WSGI configuration file:** click the link to edit it, delete
     everything, and paste (replace `YOURUSER` with your username):

     ```python
     import sys
     sys.path.insert(0, '/home/YOURUSER/GST-Filing-E-Com')
     import os
     os.environ['PUBLIC_MODE'] = '1'
     from web_app import app as application
     ```

   - **Static files:** add a mapping —
     URL: `/static/` → Directory: `/home/YOURUSER/GST-Filing-E-Com/static`
6. Click the green **Reload** button at the top of the Web tab.

Your site is live at:

```
https://YOURUSER.pythonanywhere.com
```

### Honest limits of the free plan

- Every 3 months you must click the **"Run until…"** button on the Web tab
  to renew the app, or PythonAnywhere disables it. They email you a reminder.
- Modest daily CPU quota — fine for a few filings a day, not for heavy use.

---

## D. Free domain — the honest answer

- The subdomains you get above — `something.onrender.com` and
  `YOURUSER.pythonanywhere.com` — are **free forever**. They are the
  realistic free option. Share that link; it works fine.
- **Truly free custom domains no longer exist.** Freenom (the old free
  `.tk`/`.ml` provider) is dead. Anyone promising a "free .com" is either
  a trial or a trick.
- A real domain like `yourshop.in` or `yourshop.com` costs roughly
  **₹100–800 per year** at registrars such as GoDaddy, Hostinger, or
  Cloudflare. Even on Render's free plan you can attach it:
  Render dashboard → your service → **Settings** → **Custom Domains** →
  add the domain and follow the DNS instructions shown.

---

## E. Updating the live site later

When you change the code:

```bash
git add -A
git commit -m "describe your change"
git push
```

- **Render:** redeploys automatically within a few minutes. Nothing to click.
- **PythonAnywhere:** open a Bash console, run
  `cd GST-Filing-E-Com && git pull`, then press **Reload** on the Web tab.

---

## F. Privacy note for your visitors

Put a line like this on the site or share it with users (feel free to copy):

> **Privacy:** The files you upload are used only to build your GST JSONs.
> They are processed on the server, and all generated files are deleted
> automatically within 1 hour. Nothing is stored permanently and nothing is
> shared with anyone.

That is also how the app really behaves in public mode: uploads are handled
per request, results live in a temporary job folder, and the app deletes job
folders older than 1 hour on its own.
