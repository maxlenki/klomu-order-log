# Klomu — Disposables Order Log

Photograph a disposables order sheet, the app reads the kitchen, items and date,
you confirm, and it logs to a shared tally that feeds the budget. Anyone can view
the insights; scanning and editing need a staff passcode.

It's one small Python service: FastAPI serves the web app, calls Claude to read
the photos, and stores everything in SQLite. The Anthropic key never leaves the
server.

---

## Before you start

You need three things:

1. A **GitHub account** (free) — Render deploys from a repo.
2. An **Anthropic API key** — sign up at https://platform.claude.com, create a key,
   and **set a monthly spend limit** in the console (so the bill can never surprise
   you). New accounts get $5 of free credit.
3. Your domain **klomu.co** (you have this).

---

## Step 1 — Put the code on GitHub

Create a new repository (e.g. `klomu-order-log`), then upload these files keeping
the structure:

```
app.py
requirements.txt
render.yaml
.gitignore
static/index.html
```

(Drag-and-drop in GitHub's web uploader is fine if you don't use git.)

## Step 2 — Deploy on Render

Easiest route, using the included blueprint:

1. Go to https://render.com, sign up, connect your GitHub.
2. **New + → Blueprint**, pick the repo. Render reads `render.yaml` and sets up the
   web service **and** the disk for you.
3. When prompted, fill the two secrets:
   - `ANTHROPIC_API_KEY` — your key from step before.
   - `WRITE_TOKEN` — invent a passcode for staff (e.g. `klomu-floor-7Q`). This is
     what they type once to scan and log.
4. Click create. First build takes a couple of minutes. You'll get a URL like
   `https://klomu-order-log.onrender.com` — open it, it works.

> If you'd rather not use the blueprint: New + → **Web Service** → connect the repo →
> Runtime **Python** → Build `pip install -r requirements.txt` → Start
> `uvicorn app:app --host 0.0.0.0 --port $PORT`. Then add the same three env vars
> (`ANTHROPIC_API_KEY`, `WRITE_TOKEN`, `DATA_DIR=/var/data`) and attach a **Disk**
> (mount path `/var/data`, 1 GB). The disk is essential — without it the database
> resets every time you redeploy.

## Step 3 — Point klomu.co at it

1. In Render: your service → **Settings → Custom Domains → Add Custom Domain**.
   Add both `klomu.co` and `www.klomu.co`.
2. Render shows the exact DNS records to create. Log in wherever klomu.co is
   registered and add them as shown — typically:
   - an **A record** for the root `klomu.co` pointing to the IP Render gives you, and
   - a **CNAME** for `www` pointing to your `…onrender.com` hostname.
   Use the values Render displays (don't copy them from memory — they're per-account).
3. Save. DNS takes anywhere from a few minutes to a couple of hours. Render then
   issues the HTTPS certificate automatically. Once it goes green, `https://klomu.co`
   is live.

## Step 4 — First run

Open the site, go to **Setup**, enter the staff passcode, paste your kitchens and
your disposable items (one per line, with the pack size in the name), and Save.
Now Scan works. Share `klomu.co` with anyone who should see the insights — they
get the Day view without needing the passcode.

---

## What's public and what isn't (and why)

You asked for the insights to be public, so **viewing** (the Day insights, the
items and kitchen breakdowns, the Checks list) is open to anyone with the link.

But **scanning and editing are protected by the passcode** — deliberately. Two
reasons, both about protecting you:

- The scan endpoint calls Claude, which **costs money**. If it were wide open, a
  bot or a bored stranger could run up your API bill.
- Writes change budget figures. Open writes would let anyone corrupt the tally.

The passcode is invisible to viewers and a one-time entry for staff, so it doesn't
get in the way of the "public insights" you wanted.

## Making it fully private later

When you're ready for the login portal you mentioned, the clean next step is a
proper login in front of everything (accounts, or a single shared login that also
gates viewing). The data model and API are already structured for it — it's an
added layer, not a rewrite. Say the word and I'll build it.

---

## Accuracy, honestly

The reader matches handwriting against your **closed lists** of kitchens and items,
which is far more reliable than reading free text — and every scan is confirmed by
a human before it's logged, with anything doubtful pushed to Checks. So the figures
are accurate by design, not by trusting the read blindly. Quantities (digits) read
more reliably than words; ask staff to write counts clearly. **Run 20–30 real sheets
through first and note your hit rate** — that's your true number.

If your sheets are clean printed text, you can cut the per-scan cost by switching the
model: in Render add an env var `CLAUDE_MODEL=claude-haiku-4-5-20251001`. For
handwriting, leave it on the default (Sonnet).

## Cost

- Render Starter service: **$7/month**, always on (no cold-start lag).
- 1 GB disk: about **$0.25/month**.
- Claude reads: roughly **1–2p per scan** on Sonnet, less on Haiku; first $5 free.

A few weeks of an event lands well under £20. Set the spend limit in the Anthropic
console and you can't be surprised.

## A note on the web app

`static/index.html` is self-contained and builds itself in the browser (it pulls
React from a CDN), so there's no build step to deploy — that's why setup is just
"Python". The trade-off is the very first page load does a second or two of work
before showing. If you later want instant loads, that's a small pre-build step I
can add.
