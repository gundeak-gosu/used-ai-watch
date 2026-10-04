#!/usr/bin/env python3
"""당근 수집기 — 한국 가정/사무실 인터넷이 연결된 PC에서 30분마다 실행.
당근은 클라우드 서버(GitHub·AWS)에는 빈 결과를 주는 경우가 많아서, 이 PC가 대신 검색해 Supabase에 올려두면
GitHub 봇이 그 결과를 씁니다.

설정 파일: %USERPROFILE%\\.used-ai-watch\\agent.json
  {"supabase_url": "...", "supabase_key": "...(공개 키)", "bot_token": "...(봇 토큰)"}
실행: python daangn_agent.py
"""
import json, os, sys, time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import requests
import bot
from daangn_browser import DaangnBrowser

CONF_DIR = Path.home() / ".used-ai-watch"
LOG = CONF_DIR / "agent.log"


def log(*a):
    line = datetime.now().strftime("%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
    print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def rpc(conf, fn, **args):
    r = requests.post(f"{conf['supabase_url'].rstrip('/')}/rest/v1/rpc/{fn}", timeout=60,
                      headers={"apikey": conf["supabase_key"], "Content-Type": "application/json"},
                      json={"p_token": conf["bot_token"], **args})
    r.raise_for_status()
    return r.json() if r.text else None


def main():
    conf = json.loads((CONF_DIR / "agent.json").read_text(encoding="utf-8"))
    jobs = rpc(conf, "bot_daangn_jobs")
    log(f"당근 수집 시작: {len(jobs)}건")
    regions, ok = {}, 0
    browser = DaangnBrowser()
    try:
        ok = _collect(conf, jobs, regions, browser)
    finally:
        browser.close()
    log(f"당근 수집 끝: {ok}/{len(jobs)} 성공")


def _collect(conf, jobs, regions, browser):
    ok = 0
    for j in jobs:
        rg, kw = j["region"], j["keyword"]
        try:
            slug = bot.resolve_region(rg, regions)
            items = browser.search(kw, slug)
            # 웹 목록·AI 평가에 필요한 값만 (본문은 AI 평가용으로 앞부분만)
            slim = [{k: it[k] for k in ("id", "src", "title", "price", "region", "status", "url", "thumb")}
                    | {"content": (it.get("content") or "")[:700]} for it in items]
            rpc(conf, "bot_daangn_put", p_region=rg, p_keyword=kw, p_articles=slim, p_resolved=slug)
            log(f"  {rg} / {kw} → {slug}: {len(slim)}건")
            ok += 1
        except BaseException as e:  # 한 건 실패해도 나머지 계속
            log(f"  {rg} / {kw} 실패: {e}")
        time.sleep(4)
    return ok


if __name__ == "__main__":
    CONF_DIR.mkdir(exist_ok=True)
    main()
