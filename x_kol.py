#!/usr/bin/env python3
"""
Watch a list of X accounts for Solana / 0x CAs.

One Grok x_search call per poll (not per handle). Posts new CAs to Discord.
This is earlier than confluence: a caller can tweet before 2 tracked wallets buy.
"""

from __future__ import annotations

import json
import os
import re
import time
import logging
import sys
from datetime import datetime, timezone

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
    force=True,
)
log = logging.getLogger("xkol")

XAI_API_KEY = os.environ["XAI_API_KEY"].strip()
KOL_WEBHOOK = os.environ["KOL_WEBHOOK"].strip()
ALERT_WEBHOOK = os.environ.get("KOL_ALERT_WEBHOOK", "").strip() or KOL_WEBHOOK
SCRAPE_RETRIES = int(os.environ.get("KOL_SCRAPE_RETRIES", "5"))
MANUAL_HANDLES = [
    h.strip().lstrip("@").lower()
    for h in os.environ.get("KOL_HANDLES", "").split(",")
    if h.strip()
]
MAX_HANDLES = int(os.environ.get("KOL_MAX_HANDLES", "40"))
REFRESH_HOURS = float(os.environ.get("KOL_REFRESH_HOURS", "12"))
POLL_SECONDS = int(os.environ.get("KOL_POLL_SECONDS", "180"))
LOOKBACK_MIN = int(os.environ.get("KOL_LOOKBACK_MIN", "20"))
XAI_MODEL = os.environ.get("XAI_MODEL", "grok-4-fast").strip()
XAI_URL = os.environ.get("XAI_URL", "https://api.x.ai/v1/responses").strip()
STATE_FILE = os.environ.get("KOL_STATE", "/app/kol_seen.json")
MIN_FOLLOWERS = int(os.environ.get("KOL_MIN_FOLLOWERS", "0"))

SOL_CA = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
ETH_CA = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
QUOTE = {
    "so11111111111111111111111111111111111111112",
    "epjfwdd5aufqssqem2qn1xzybapc8g4weggkzwytdt1v",
    "es9vmfrzacermjfrf4h2fyd4kconky11mcce8benwnyb",
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "0xdac17f958d2ee523a2206206994597c13d831ec7",
}


HANDLE_RE = re.compile(r"(?:twitter\.com|x\.com)/([A-Za-z0-9_]{2,30})", re.I)
AT_RE = re.compile(r"@([A-Za-z0-9_]{2,30})")
_handle_cache = {"ts": 0.0, "handles": []}


def _norm_handle(h: str) -> str | None:
    h = (h or "").strip().lstrip("@").lower()
    if h.lower() in {"i", "intent", "share", "home", "search", "mwx_ai"}:
        return None
    if re.fullmatch(r"[A-Za-z0-9_]{2,30}", h):
        return h
    return None


def alert_scrape(site: str, err: str):
    log.error("%s scrape failed after %s tries: %s", site, SCRAPE_RETRIES, err)
    try:
        requests.post(
            ALERT_WEBHOOK,
            json={
                "embeds": [
                    {
                        "title": f"Unable to scrape {site}",
                        "description": f"Tried {SCRAPE_RETRIES} times.\n```{err[:1500]}```",
                        "color": 0xED4245,
                    }
                ]
            },
            timeout=15,
        )
    except Exception:
        log.exception("alert webhook failed")


def fetch_kolscan() -> list[str]:
    """Public KolScan leaderboard HTML — twitter/x links when present."""
    last_err = "unknown"
    html = ""
    for attempt in range(1, SCRAPE_RETRIES + 1):
        try:
            r = requests.get(
                "https://kolscan.io/leaderboard",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=20,
            )
            r.raise_for_status()
            html = r.text
            last_err = ""
            break
        except Exception as e:
            last_err = str(e)
            log.warning("kolscan try %s/%s %s", attempt, SCRAPE_RETRIES, e)
            time.sleep(min(2 * attempt, 10))
    out = []
    if last_err:
        alert_scrape("KolScan", last_err)
    else:
        for m in HANDLE_RE.findall(html):
            h = _norm_handle(m)
            if h:
                out.append(h)
        if not out:
            alert_scrape("KolScan", "page loaded but no twitter/x.com handles found")
    # unique, keep order
    seen = set()
    uniq = []
    for h in out:
        if h not in seen:
            seen.add(h)
            uniq.append(h)
    log.info("kolscan handles=%s", len(uniq))
    return uniq


def fetch_mwx() -> list[str]:
    """MWX has no public API — pull @handles from their recent leaderboard tweets."""
    prompt = (
        "Use x_search on account @mwx_ai only, last 7 days. "
        "Find KOL Leaderboard posts. Extract every @handle they ranked "
        "(the callers, not mwx_ai itself). "
        "Return one handle per line, no extra text. Example:\nApex_sol1\nJakee_cryptt"
    )
    text = ""
    last_err = "unknown"
    for attempt in range(1, SCRAPE_RETRIES + 1):
        try:
            r = requests.post(
                XAI_URL,
                headers={
                    "Authorization": f"Bearer {XAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": XAI_MODEL,
                    "tools": [
                        {
                            "type": "x_search",
                            "allowed_x_handles": ["mwx_ai"],
                        }
                    ],
                    "input": prompt,
                },
                timeout=90,
            )
            r.raise_for_status()
            data = r.json()
            text = data.get("output_text") or ""
            if not text:
                text = "\n".join(
                    c.get("text") or ""
                    for item in data.get("output") or []
                    if item.get("type") == "message"
                    for c in item.get("content") or []
                )
            last_err = ""
            break
        except Exception as e:
            last_err = str(e)
            log.warning("mwx try %s/%s %s", attempt, SCRAPE_RETRIES, e)
            time.sleep(min(2 * attempt, 10))
    if last_err:
        alert_scrape("MWX / @mwx_ai", last_err)
        return []
    if not text.strip():
        alert_scrape("MWX / @mwx_ai", "x_search returned empty text")
        return []
    out = []
    for line in text.splitlines():
        for raw in AT_RE.findall(line) + [line.strip()]:
            h = _norm_handle(raw)
            if h:
                out.append(h)
    seen = set()
    uniq = []
    for h in out:
        if h not in seen:
            seen.add(h)
            uniq.append(h)
    if not uniq:
        alert_scrape("MWX / @mwx_ai", "response had no @handles to parse")
    log.info("mwx handles=%s", len(uniq))
    return uniq


def refresh_handles() -> list[str]:
    now = time.time()
    if _handle_cache["handles"] and now - _handle_cache["ts"] < REFRESH_HOURS * 3600:
        return _handle_cache["handles"]
    merged = []
    seen = set()
    for h in MANUAL_HANDLES + fetch_kolscan() + fetch_mwx():
        if h and h not in seen:
            seen.add(h)
            merged.append(h)
    merged = merged[:MAX_HANDLES]
    _handle_cache.update(ts=now, handles=merged)
    log.info("watchlist size=%s %s", len(merged), merged[:15])
    return merged


def chunks(items: list[str], n: int):
    for i in range(0, len(items), n):
        yield items[i : i + n]


def load_seen() -> set[str]:
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return {str(x).lower() for x in (data if isinstance(data, list) else [])}
    except Exception:
        return set()


def save_seen(seen: set[str]):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(sorted(seen)[-400:], f)
    except Exception as e:
        log.warning("save state: %s", e)


def grok_scan(batch: list[str]) -> str:
    handle_list = ", ".join(f"@{h}" for h in batch)
    prompt = (
        f"Search X for the last {LOOKBACK_MIN} minutes ONLY.\n"
        f"Accounts to watch: {handle_list}\n"
        "Use x_search. Restrict to those handles if the tool allows "
        "(allowed_x_handles). Also run a second search for those handles "
        "plus the words CA, contract, mint, pump.\n"
        "Find Solana base58 mints (32-44 chars) and EVM 0x addresses they posted.\n"
        "For each hit return EXACTLY one line:\n"
        "CA=<address> | @handle | followers=<n or ?> | ticker=<$TICK or ?> | "
        "quote=<short tweet text>\n"
        "Skip quotes, SOL, USDC, wrapped SOL. Skip posts older than the window.\n"
        "If nothing, reply NONE."
    )
    r = requests.post(
        XAI_URL,
        headers={
            "Authorization": f"Bearer {XAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": XAI_MODEL,
            "tools": [
                {
                    "type": "x_search",
                    "allowed_x_handles": batch[:20],
                }
            ],
            "input": prompt,
        },
        timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("output_text"):
        return str(data["output_text"])
    parts = []
    for item in data.get("output") or []:
        if item.get("type") == "message":
            for c in item.get("content") or []:
                if c.get("text"):
                    parts.append(c["text"])
    return "\n".join(parts)


def parse_hits(text: str) -> list[dict]:
    hits = []
    for line in (text or "").splitlines():
        if "CA=" not in line.upper() and not ETH_CA.search(line) and not SOL_CA.search(line):
            continue
        cas = ETH_CA.findall(line) + SOL_CA.findall(line)
        handle = None
        m = re.search(r"@([A-Za-z0-9_]{2,30})", line)
        if m:
            handle = m.group(1)
        fol = None
        fm = re.search(r"followers\s*=\s*([0-9.]+[kKmM]?|\?)", line)
        if fm and fm.group(1) != "?":
            fol = fm.group(1)
        tick = None
        tm = re.search(r"ticker\s*=\s*\$?([A-Za-z0-9]{2,20}|\?)", line, re.I)
        if tm and tm.group(1) != "?":
            tick = tm.group(1)
        for ca in cas:
            key = ca.lower()
            if key in QUOTE:
                continue
            if ca.startswith("0x"):
                ca = ca.lower()
            hits.append(
                {
                    "ca": ca,
                    "handle": handle or "?",
                    "followers": fol or "?",
                    "ticker": tick or "?",
                    "line": line.strip()[:280],
                }
            )
    return hits


def axiom_url(token: str) -> str:
    slug = "robinhood" if token.startswith("0x") else "sol"
    return f"https://axiom.trade/meme/{token}?chain={slug}"


def post(hit: dict):
    ticker = hit["ticker"]
    title = f"KOL CA — ${ticker}" if ticker != "?" else "KOL CA"
    buy = axiom_url(hit["ca"])
    requests.post(
        KOL_WEBHOOK,
        json={
            "embeds": [
                {
                    "title": title,
                    "url": buy,
                    "description": (
                        f"**@{hit['handle']}** · {hit['followers']} followers\n"
                        f"{hit['line']}"
                    )[:1900],
                    "color": 0x1DA1F2,
                    "fields": [
                        {"name": "CA", "value": f"`{hit['ca']}`", "inline": False},
                        {
                            "name": "Axiom",
                            "value": f"[Buy on Axiom]({buy})",
                            "inline": False,
                        },
                    ],
                    "footer": {"text": "X watchlist — not a confluence ping"},
                }
            ]
        },
        timeout=20,
    ).raise_for_status()


def main():
    log.info(
        "kol watcher poll=%ss lookback=%sm model=%s max_handles=%s",
        POLL_SECONDS,
        LOOKBACK_MIN,
        XAI_MODEL,
        MAX_HANDLES,
    )
    seen = load_seen()
    while True:
        try:
            handles = refresh_handles()
            if not handles:
                log.warning("no handles yet — check KolScan/MWX scrape")
            for batch in chunks(handles, 20):
                raw = grok_scan(batch)
                log.info("batch %s grok %s chars", len(batch), len(raw or ""))
                if not raw or raw.strip().upper() == "NONE":
                    continue
                for hit in parse_hits(raw):
                    key = hit["ca"].lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    save_seen(seen)
                    post(hit)
                    log.info("posted %s from @%s", hit["ca"][:8], hit["handle"])
        except Exception:
            log.exception("poll failed")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
