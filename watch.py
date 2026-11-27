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
import re
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

# 世界のベスト (Invesco World Best Equity Open, monthly, no hedge).
# NAV source: Jiji fund summary page (server-rendered HTML, no key/JS).
# Fund code 18312991. NAV updates ~22:00 JST on weekdays; the page itself
# gives the day change, and the latest distribution (monthly, 150 yen) with
# its settlement date, so a 5-day move can be adjusted for distributions.
FUND_FCODE = "18312991"
FUND_NAME = "世界のベスト（毎月決算型・ヘッジなし）"
FUND_URL = ("https://t2.jiji.com/linkbox?FWCS=tiger&pageID=LB1143_FUND_SUMMARY"
            "&userID=keiyo-bk_v2&fcode=18312991&tab=1")
FUND_DAY_THRESHOLD = 3.0    # % move in one day
FUND_WEEK_THRESHOLD = 5.0   # % move over 5 trading days (distribution-adjusted)

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


def fetch_fund_nav():
    """Return (date, nav, day_change, dist_amount, dist_date) from Jiji."""
    raw = fetch(FUND_URL).decode("utf-8", "replace")
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw))
    m_date = re.search(r"直近情報（(\d{4}/\d{2}/\d{2})）", text)
    m_nav = re.search(r"基準価額 ([\d,]+)円（\s*([+-]\d+)", text)
    m_dist = re.search(r"直近分配金 ([\d,]+)円（決算日：(\d{4}/\d{2}/\d{2})）", text)
    if not (m_date and m_nav):
        raise ValueError("fund NAV parse failed")
    nav = int(m_nav.group(1).replace(",", ""))
    change = int(m_nav.group(2))
    dist = int(m_dist.group(1).replace(",", "")) if m_dist else 0
    dist_date = m_dist.group(2) if m_dist else ""
    return m_date.group(1), nav, change, dist, dist_date


def check_fund(state):
    hist = state.setdefault("fund", [])
    try:
        date, nav, change, dist, dist_date = fetch_fund_nav()
    except Exception as e:
        print("fund fetch error", e)
        return
    if hist and hist[-1]["date"] == date:
        print("fund already recorded for", date)
        return
    first = not hist
    day = (change / (nav - change) * 100) if nav != change else 0.0
    week = None
    if len(hist) >= 4:
        base = hist[-4]
        adj = nav + (dist if dist_date and dist_date > base["date"] else 0)
        week = (adj / base["nav"] - 1) * 100
    hist.append({"date": date, "nav": nav, "dist": dist, "dist_date": dist_date})
    state["fund"] = hist[-6:]
    if first:
        print("fund seeded:", date, nav)
        return
    if abs(day) >= FUND_DAY_THRESHOLD or (week is not None and abs(week) >= FUND_WEEK_THRESHOLD):
        week_txt = f"、5日比 {week:+.1f}%" if week is not None else ""
        notify(f"[ファンド] {FUND_NAME}が動きました。基準価額 {nav:,}円、"
               f"前日比 {day:+.1f}%（{change:+,}円）{week_txt}。"
               f"分配金 {dist:,}円（決算日 {dist_date}）。理由を調べてください。",
               event_type="fund_move")
    else:
        print(f"fund ok: {date} {nav:,} yen day {day:+.2f}%"
              + (f" week {week:+.2f}%" if week is not None else ""))


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
    elif mode == "fund":
        check_fund(state)
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
