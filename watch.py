#!/usr/bin/env python3
"""Nanachi watcher: releases / stock moves / JMA warnings -> MuseKnock custom webhook.

State lives in state.json in the same repo; the GitHub Actions workflow
commits it back after each run. On the very first run for a source
(state missing), the watcher seeds state WITHOUT notifying, so enabling
it never floods the chat with old news.

Env:
  HOOK_TOKEN  - shared secret for the MuseKnock custom webhook
  HOOK_URL    - default https://museknock.k71887188.workers.dev/v1/hooks/custom
  MODE        - releases | stocks | weather   (or argv[1])
"""
import json
import os
import sys
import urllib.request
import urllib.error

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
HOOK_URL = os.environ.get("HOOK_URL", "https://museknock.k71887188.workers.dev/v1/hooks/custom")

RELEASE_REPOS = [
    "obsidianmd/obsidian-releases",   # Obsidian
    "adbar/trafilatura",              # Trafilatura
    "Aenvo/MuseLink",                 # MuseLink
    "mvanhorn/last30days-skill",      # last30days
]
STOCKS = {"4023.T": "クレハ", "4503.T": "アステラス製薬", "1605.T": "INPEX"}
STOCK_DAY_THRESHOLD = 3.0    # % move in one day
STOCK_WEEK_THRESHOLD = 5.0   # % move over 5 trading days
JMA_WARNING_URL = "https://www.jma.go.jp/bosai/warning/data/warning/110000.json"  # Saitama
# JMA warning kinds that count as 警報級 (code -> name). 注意報 codes are 2-digit
# advisories too; we notify on 警報 (status 発表/継続 with these codes) and 特別警報.
WARNING_CODES = {
    "32": "暴風雪警報", "33": "大雨警報", "34": "洪水警報", "35": "大雪警報",
    "36": "暴風警報", "37": "波浪警報", "38": "高潮警報",
}


def fetch(url, ua="nanachi-watch/1.0"):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def fetch_json(url, ua="nanachi-watch/1.0"):
    return json.loads(fetch(url, ua).decode("utf-8"))


def notify(message, source="custom", event_type="notification"):
    token = os.environ.get("HOOK_TOKEN", "")
    if not token:
        print("HOOK_TOKEN not set; would notify:", message)
        return False
    body = json.dumps({"message": message}).encode("utf-8")
    req = urllib.request.Request(HOOK_URL, data=body, method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Event-Type", event_type)
    # Cloudflare rejects the default Python-urllib signature (error 1010);
    # an explicit UA is required for the webhook call to reach the Worker.
    req.add_header("User-Agent", "nanachi-watch/1.0 (+https://github.com/K71887188/nanachi-watch)")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print("notified:", r.status, message[:80])
            return True
    except urllib.error.HTTPError as e:
        print("notify failed:", e.code, e.read().decode("utf-8", "replace")[:200])
        return False


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


def check_releases(state):
    seen = state.setdefault("releases", {})
    first = not seen
    for repo in RELEASE_REPOS:
        try:
            rels = fetch_json(f"https://api.github.com/repos/{repo}/releases?per_page=1")
        except Exception as e:
            print(repo, "fetch error", e)
            continue
        if not rels:
            continue
        tag = rels[0].get("tag_name", "")
        url = rels[0].get("html_url", "")
        if seen.get(repo) == tag:
            continue
        seen[repo] = tag
        if not first:
            notify(f"[リリース] {repo} が {tag} を公開しました。 {url}",
                   event_type="release")


def check_stocks(state):
    prev = state.setdefault("stocks", {})
    first = not prev
    for sym, name in STOCKS.items():
        try:
            d = fetch_json(
                f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d",
                ua="Mozilla/5.0")
            closes = [c for c in d["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c]
        except Exception as e:
            print(sym, "fetch error", e)
            continue
        if len(closes) < 2:
            continue
        last = closes[-1]
        day = (last / closes[-2] - 1) * 100
        week = (last / closes[0] - 1) * 100
        key = f"{sym}:{d['chart']['result'][0]['timestamp'][-1]}"
        if prev.get(sym) == key:
            continue
        prev[sym] = key
        if first:
            continue
        if abs(day) >= STOCK_DAY_THRESHOLD or abs(week) >= STOCK_WEEK_THRESHOLD:
            notify(f"[株価] {name}（{sym}）が動きました。終値 {last:,.0f}円、"
                   f"前日比 {day:+.1f}%、5日比 {week:+.1f}%。理由を調べてください。",
                   event_type="stock_move")


def check_weather(state):
    prev = state.setdefault("weather", {})
    try:
        d = fetch_json(JMA_WARNING_URL)
    except Exception as e:
        print("JMA fetch error", e)
        return
    active = {}
    for at in d.get("areaTypes", []):
        for area in at.get("areas", []):
            for w in area.get("warnings", []):
                code = w.get("code")
                status = w.get("status", "")
                if code in WARNING_CODES and status in ("発表", "継続"):
                    active[f"{area['code']}:{code}"] = WARNING_CODES[code]
    first = not prev and not state.get("weather_seeded")
    state["weather_seeded"] = True
    new = {k: v for k, v in active.items() if k not in prev}
    state["weather"] = active
    if first:
        return
    for k, name in new.items():
        notify(f"[気象警報] 埼玉県で{name}が発表されています（エリアコード {k.split(':')[0]}）。"
               f"詳細と影響を確認してください。", event_type="weather_warning")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MODE", "")
    state = load_state()
    if mode == "releases":
        check_releases(state)
    elif mode == "stocks":
        check_stocks(state)
    elif mode == "weather":
        check_weather(state)
    elif mode == "test":
        if not notify("[テスト] nanachi-watch から MuseKnock への疎通テストです。", event_type="test"):
            save_state(state)
            return 1
    else:
        print("unknown mode:", mode)
        return 1
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
