"""
Klomu Disposables Order Log — backend.

One FastAPI service that:
  - serves the web app (static/index.html)
  - exposes a small JSON API
  - reads order-sheet photos with Claude (key stays server-side)
  - stores everything in SQLite (put the file on a Render Disk so it survives deploys)

Reading the insights is public. Anything that costs money (scanning) or
changes data (saving config, logging a record, editing the Checks pile) is
gated by a shared passcode (WRITE_TOKEN). Leave WRITE_TOKEN blank to open
those up too — not recommended while the app is public.
"""

import os
import re
import json
import base64
import difflib
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, UploadFile, File, HTTPException, Header
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from anthropic import Anthropic

# ----------------------------------------------------------------------------
# Configuration (all from environment variables on Render)
# ----------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "orderlog.db"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
WRITE_TOKEN = os.environ.get("WRITE_TOKEN", "")          # staff passcode for writes
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

app = FastAPI(title="Klomu Order Log")


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                kitchens TEXT NOT NULL DEFAULT '[]',
                items    TEXT NOT NULL DEFAULT '[]'
            );
            INSERT OR IGNORE INTO config (id, kitchens, items) VALUES (1, '[]', '[]');

            CREATE TABLE IF NOT EXISTS records (
                id         TEXT PRIMARY KEY,
                date       TEXT,
                kitchen    TEXT,
                items      TEXT NOT NULL,
                source     TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS unsuccessful (
                id         TEXT PRIMARY KEY,
                reason     TEXT,
                draft      TEXT,
                image      TEXT,
                created_at TEXT
            );
            """
        )


init_db()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------------
# Auth: writes require the shared passcode (if one is set)
# ----------------------------------------------------------------------------
def require_write(x_app_token: str = Header(default="")):
    if WRITE_TOKEN and x_app_token != WRITE_TOKEN:
        raise HTTPException(status_code=401, detail="Enter the staff passcode to do that.")


# ----------------------------------------------------------------------------
# Claude vision: read a disposables order sheet
# ----------------------------------------------------------------------------
def build_prompt(kitchens, items):
    kitchen_list = "; ".join(kitchens) if kitchens else "(none configured)"
    item_list = "; ".join(items) if items else "(none configured)"
    return (
        "You are reading a warehouse order sheet for DISPOSABLE CATERING SUPPLIES "
        "from a photo. Staff hand in these sheets to collect stock. Typical items are "
        "disposable plates, napkins / serviettes, cutlery (forks, knives, spoons, sporks), "
        "cups, lids, straws, food containers and clamshells, foil, cling film, bin bags and gloves.\n\n"
        f"VALID KITCHENS: {kitchen_list}\n"
        f"VALID ITEMS: {item_list}\n\n"
        "Read the sheet and extract:\n"
        "- kitchen: the kitchen / location name written on the sheet.\n"
        "- date: the date, usually in the bottom-right corner.\n"
        "- items: every line item with its quantity as a number. Quantities are often "
        "written in packs, cases, sleeves, boxes or rolls (e.g. \"Napkins x3 cases\"). "
        "Record the number exactly as written; the unit is part of the valid item name, "
        "so match to the closest valid entry and put the count in \"quantity\".\n\n"
        "For the kitchen and for each item, set \"matched\" to the EXACT valid entry from "
        "the lists above (copied character for character). Match on the meaningful words, "
        "IGNORING case, punctuation, and a leading \"the\" — e.g. \"Hill Larder\", \"hill larder\" "
        "and \"THE HILL LARDER\" are the SAME entry. Allow for handwriting, abbreviations and "
        "minor typos. When two entries are close, choose the one sharing the most words with what "
        "is written. Only use null if nothing on the list is plausibly the same thing. Prefer "
        "recommending the closest valid entry over null.\n\n"
        "Return ONLY a JSON object, no markdown fences, no commentary:\n"
        '{"kitchen":{"raw":"<text seen>","matched":"<valid kitchen or null>"},'
        '"date":{"raw":"<text seen>","value":"<YYYY-MM-DD or null>"},'
        '"items":[{"raw":"<text seen>","matched":"<valid item or null>","quantity":<number>}]}'
    )


_STOP = {"the", "a", "an", "of", "and", "for", "with", "x", "1", "&"}


def _norm(s):
    s = (s or "").lower().strip()
    s = re.sub(r"^the\s+", "", s)          # ignore a leading "the"
    s = re.sub(r"[^a-z0-9 ]+", " ", s)     # punctuation -> space
    return re.sub(r"\s+", " ", s).strip()


def _tokens(s):
    return [t for t in _norm(s).split() if t and t not in _STOP]


def best_match(raw, candidates, floor=0.34):
    """Resolve a messy string to the closest valid entry using word overlap.
    Always recommends the best candidate above `floor`, else None."""
    if not raw:
        return None
    rn = _norm(raw)
    for c in candidates:                    # exact normalized match wins
        if _norm(c) == rn:
            return c
    rset = set(_tokens(raw))
    best, best_score = None, 0.0
    for c in candidates:
        cset = set(_tokens(c))
        if not rset or not cset:
            continue
        overlap = len(rset & cset)
        word_score = overlap / min(len(rset), len(cset))     # shared significant words
        seq = difflib.SequenceMatcher(None, rn, _norm(c)).ratio()
        score = 0.72 * word_score + 0.28 * seq
        if score > best_score:
            best, best_score = c, score
    return best if best_score >= floor else None


def _resolve(parsed, kitchens, item_names):
    """Force kitchen and each item onto a real catalogue entry so prices apply."""
    valid_k = set(kitchens)
    k = parsed.get("kitchen") or {}
    if k.get("matched") not in valid_k:
        k["matched"] = best_match(k.get("matched") or k.get("raw"), kitchens, floor=0.30)
    parsed["kitchen"] = k
    valid_i = set(item_names)
    for it in parsed.get("items") or []:
        if it.get("matched") not in valid_i:
            it["matched"] = best_match(it.get("matched") or it.get("raw"), item_names, floor=0.42)
    return parsed


def read_sheet(image_bytes: bytes, media_type: str, kitchens, items) -> dict:
    if client is None:
        raise HTTPException(status_code=503, detail="The reader is not configured (no API key set).")
    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64", "media_type": media_type, "data": b64}},
                        {"type": "text", "text": build_prompt(kitchens, items)},
                    ],
                }
            ],
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Reader unavailable: {e}")

    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise HTTPException(status_code=422, detail="Could not read the sheet clearly.")
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="Could not read the sheet clearly.")
    return _resolve(parsed, kitchens, [i["name"] for i in items] if items and isinstance(items[0], dict) else list(items))


# ----------------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------------
class ConfigIn(BaseModel):
    kitchens: list[str] = []
    items: list[dict] = []   # [{ "name": "Paper plates 9in (case of 1000)" }]


class RecordItem(BaseModel):
    name: str
    qty: float
    unit: str = "box"      # "box" or "part"
    pieces: float = 0      # total individual pieces taken
    value: float = 0       # GBP value of this line


class RecordIn(BaseModel):
    id: str
    dateISO: str
    kitchen: str
    items: list[RecordItem]
    source: str = "scan"


class UnsuccessfulIn(BaseModel):
    id: str
    reason: str
    draft: dict | None = None
    image: str | None = None   # data URL


# ----------------------------------------------------------------------------
# API — config
# ----------------------------------------------------------------------------
@app.get("/api/config")
def get_config():
    with db() as conn:
        row = conn.execute("SELECT kitchens, items FROM config WHERE id = 1").fetchone()
    return {"kitchens": json.loads(row["kitchens"]), "items": json.loads(row["items"])}


@app.put("/api/config")
def put_config(cfg: ConfigIn, x_app_token: str = Header(default="")):
    require_write(x_app_token)
    with db() as conn:
        conn.execute(
            "UPDATE config SET kitchens = ?, items = ? WHERE id = 1",
            (json.dumps(cfg.kitchens), json.dumps([dict(i) for i in cfg.items])),
        )
    return {"ok": True}


# ----------------------------------------------------------------------------
# API — scan (costs money; gated)
# ----------------------------------------------------------------------------
@app.post("/api/scan")
async def scan(file: UploadFile = File(...), x_app_token: str = Header(default="")):
    require_write(x_app_token)
    cfg = get_config()
    data = await file.read()
    media_type = file.content_type or "image/jpeg"
    return read_sheet(data, media_type, cfg["kitchens"], [i["name"] for i in cfg["items"]])


# ----------------------------------------------------------------------------
# API — records (read public, write gated)
# ----------------------------------------------------------------------------
@app.get("/api/records")
def get_records(date: str | None = None):
    q = "SELECT * FROM records"
    args = ()
    if date:
        q += " WHERE date = ?"
        args = (date,)
    q += " ORDER BY created_at DESC"
    with db() as conn:
        rows = conn.execute(q, args).fetchall()
    return [
        {
            "id": r["id"], "dateISO": r["date"], "kitchen": r["kitchen"],
            "items": json.loads(r["items"]), "source": r["source"], "scannedAt": r["created_at"],
        }
        for r in rows
    ]


@app.post("/api/records")
def add_record(rec: RecordIn, x_app_token: str = Header(default="")):
    require_write(x_app_token)
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO records (id, date, kitchen, items, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (rec.id, rec.dateISO, rec.kitchen,
             json.dumps([i.model_dump() for i in rec.items]), rec.source, now_iso()),
        )
    return {"ok": True}


# ----------------------------------------------------------------------------
# API — unsuccessful / Checks (read public, write gated)
# ----------------------------------------------------------------------------
@app.get("/api/unsuccessful")
def get_unsuccessful():
    with db() as conn:
        rows = conn.execute("SELECT * FROM unsuccessful ORDER BY created_at DESC").fetchall()
    return [
        {
            "id": r["id"], "reason": r["reason"],
            "draft": json.loads(r["draft"]) if r["draft"] else None,
            "image": r["image"], "scannedAt": r["created_at"],
        }
        for r in rows
    ]


@app.post("/api/unsuccessful")
def add_unsuccessful(u: UnsuccessfulIn, x_app_token: str = Header(default="")):
    require_write(x_app_token)
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO unsuccessful (id, reason, draft, image, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (u.id, u.reason, json.dumps(u.draft) if u.draft else None, u.image, now_iso()),
        )
    return {"ok": True}


@app.delete("/api/unsuccessful/{uid}")
def delete_unsuccessful(uid: str, x_app_token: str = Header(default="")):
    require_write(x_app_token)
    with db() as conn:
        conn.execute("DELETE FROM unsuccessful WHERE id = ?", (uid,))
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True, "reader_configured": client is not None, "writes_protected": bool(WRITE_TOKEN)}


# ----------------------------------------------------------------------------
# Serve the web app (must be mounted LAST so /api routes win)
# ----------------------------------------------------------------------------
app.mount("/", StaticFiles(directory="static", html=True), name="static")
