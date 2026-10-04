#!/usr/bin/env python3
"""중고 AI 비교 알리미 — 웹 서비스판 (Supabase 프로젝트 기반)
원하는 레벨·스타일을 글로 적어두면, 여러 중고 사이트 매물들을 AI가 하나하나 읽고 '나한테 맞는 정도 + 가격 가치'로
점수를 매겨 서로 비교하고, 상위권 매물이 새로 뜨면 이메일로 알려줍니다."""
import json, os, re, smtplib, sys, time, urllib.parse
from email.message import EmailMessage
from html import escape as esc
from datetime import datetime, timezone, timedelta
import requests, yaml
import generic

BASE = "https://www.daangn.com"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
      "Accept-Language": "ko-KR,ko;q=0.9"}
KST = timezone(timedelta(hours=9))


class Blocked(Exception):
    pass


def won(n):
    return f"{int(n):,}원"


# =============== 당근 ===============
def get_json(url):
    r = requests.get(url, headers=UA, timeout=25)
    if r.status_code in (403, 429, 503):
        raise Blocked(f"HTTP {r.status_code}")
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        raise Blocked("JSON이 아닌 응답(봇 차단 페이지일 가능성)")

def resolve_region(text, cache):
    """'역삼동-6035' 형식이면 그대로, '강남구 역삼동' 같은 이름이면 당근 지역 API로 변환."""
    text = text.strip()
    if re.fullmatch(r".+-\d+", text):
        return text
    if text in cache:
        return cache[text]
    tokens = text.split()
    data = get_json(f"{BASE}/kr/api/v1/regions/keyword?keyword=" + urllib.parse.quote(tokens[-1]))
    locs = data.get("locations") or []
    full = lambda x: " ".join(str(x.get(k) or "") for k in ("name1", "name2", "name3", "name"))
    cands = [x for x in locs if all(t in full(x) for t in tokens)] or locs
    if not cands:
        raise SystemExit(f"[설정 오류] 동네를 찾을 수 없어요: {text}")
    slug = f"{cands[0]['name']}-{cands[0]['id']}"
    print(f"  동네 '{text}' → {full(cands[0]).strip()} ({slug})")
    cache[text] = slug
    return slug

def _thumb(a):
    for k in ("thumbnail", "thumbnailUrl", "image", "imageUrl"):
        v = a.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    imgs = a.get("images") or []
    if imgs and isinstance(imgs[0], dict):
        return imgs[0].get("url") or ""
    if imgs and isinstance(imgs[0], str):
        return imgs[0]
    return ""

def search(keyword, region_slug):
    params = [("in", region_slug), ("q", keyword), ("only_on_sale", "true"),
              ("_data", "routes/kr.search.buy-sell._index")]
    url = f"{BASE}/kr/search/buy-sell/?" + urllib.parse.urlencode(params)
    data = get_json(url)
    arts = data.get("buySellArticles") or ((data.get("allPage") or {}).get("fleamarketArticles")) or []
    if not arts:
        # 당근은 요청이 몰리면 오류 대신 '빈 결과'를 줄 때가 있어 잠시 쉬었다가 한 번 더
        time.sleep(20)
        data = get_json(url)
        arts = data.get("buySellArticles") or ((data.get("allPage") or {}).get("fleamarketArticles")) or []
        print(f"  당근 {region_slug} '{keyword}': 빈 결과 → 재시도 {len(arts)}건")
    # 검색 결과에 먼 지역 글도 섞여 오므로 '내 동네 + 당근이 알려주는 인근 동네'만 남김(직거래용)
    near = {r.get("name") for r in [data.get("searchRegion") or {}] + (data.get("nearbyRegions") or [])} - {None}
    if near:
        arts = [a for a in arts if (a.get("region") or {}).get("name") in near]
    out = []
    for a in arts:
        href = a.get("href") or a.get("webUrl") or ""
        link = href if href.startswith("http") else BASE + href
        try:
            price = int(float(a.get("price") or 0))
        except (TypeError, ValueError):
            price = 0
        out.append({"id": "dg:" + str(a.get("id") or link), "src": "당근", "title": (a.get("title") or "").strip(),
                    "content": a.get("content") or "", "price": price,
                    "region": (a.get("region") or {}).get("name", ""),
                    "status": a.get("status"), "url": link, "thumb": _thumb(a)})
    return out

def _find_content(o):
    if isinstance(o, dict):
        v = o.get("content")
        if isinstance(v, str) and len(v) > 10:
            return v
        for x in o.values():
            r = _find_content(x)
            if r:
                return r
    elif isinstance(o, list):
        for x in o:
            r = _find_content(x)
            if r:
                return r
    return ""

def fetch_detail(url):
    try:
        return _find_content(get_json(url.rstrip("/") + "/?_data=routes%2Fkr.buy-sell.%24buy_sell_id"))
    except Blocked:
        raise
    except Exception as e:
        print("  상세 조회 실패:", e)
        return ""


# =============== 번개장터 ===============
def bunjang_search(keyword):
    params = {"q": keyword, "order": "date", "page": 0, "n": 60,
              "stat_device": "w", "req_ref": "search", "version": 5}
    data = get_json("https://api.bunjang.co.kr/api/1/find_v2.json?" + urllib.parse.urlencode(params))
    out = []
    for a in data.get("list") or []:
        if a.get("ad") or str(a.get("type", "")).upper() in ("ADVERTISE", "AD"):
            continue
        if str(a.get("status", "0")) not in ("0", ""):
            continue  # 0 = 판매중
        pid = str(a.get("pid") or "")
        if not pid:
            continue
        try:
            price = int(float(a.get("price") or 0))
        except (TypeError, ValueError):
            price = 0
        img = str(a.get("product_image") or "").replace("{res}", "400")
        out.append({"id": "bj:" + pid, "src": "번개장터", "title": (a.get("name") or "").strip(),
                    "content": "", "price": price, "region": a.get("location") or "택배",
                    "status": None, "url": f"https://m.bunjang.co.kr/products/{pid}", "thumb": img})
    return out

def bunjang_detail(pid):
    data = get_json(f"https://api.bunjang.co.kr/api/pms/v3/products-detail/{pid}?viewerUid=-1")
    p = ((data.get("data") or {}).get("product")) or {}
    return p.get("description") or ""


# =============== Next.js 페이지 공용 추출기 (중고나라·N플리마켓) ===============
def get_next_data(url):
    r = requests.get(url, headers={**UA, "Accept": "text/html"}, timeout=25)
    if r.status_code in (403, 429, 503):
        raise Blocked(f"HTTP {r.status_code}")
    r.raise_for_status()
    r.encoding = "utf-8"
    m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if m:
        return json.loads(m.group(1))
    # Next.js App Router 등: 페이지 안의 큰 JSON 덩어리들을 모두 시도
    blobs = []
    # App Router(RSC) 스트림: self.__next_f.push([1,"..."]) 조각을 이어 붙인 뒤 "id:JSON" 줄마다 해석
    rsc = "".join(json.loads('"' + c + '"') for c in
                  re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', r.text, re.S))
    for line in rsc.split("\n"):
        m = re.match(r"[0-9a-z]+:(.+)", line)
        if m and m.group(1)[:1] in "[{":
            try:
                blobs.append(json.loads(m.group(1)))
            except ValueError:
                pass
    for m in re.finditer(r'<script[^>]*type="application/(?:ld\+)?json"[^>]*>(.*?)</script>', r.text, re.S):
        try:
            blobs.append(json.loads(m.group(1)))
        except ValueError:
            pass
    return blobs

TITLE_KEYS = ("title", "productTitle", "name", "productName", "subject")
PRICE_KEYS = ("price", "productPrice", "salePrice", "sellPrice")
ID_KEYS = ("seq", "productSeq", "productId", "id", "itemId", "articleId")

def find_products(o, out=None):
    """JSON 트리 안에서 '제목+가격+id'를 가진 객체를 모두 찾아냄."""
    out = [] if out is None else out
    if isinstance(o, dict):
        t = next((o[k] for k in TITLE_KEYS if isinstance(o.get(k), str) and o.get(k).strip()), None)
        p = next((o[k] for k in PRICE_KEYS if isinstance(o.get(k), (int, float, str)) and str(o.get(k)).replace(",", "").replace(".", "").isdigit()), None)
        i = next((o[k] for k in ID_KEYS if o.get(k) not in (None, "")), None)
        if t and p is not None and i is not None:
            out.append(o)
        for v in o.values():
            find_products(v, out)
    elif isinstance(o, list):
        for v in o:
            find_products(v, out)
    return out

def _first_img(o):
    for k in ("url", "imageUrl", "imgUrl", "thumbnail", "thumbnailUrl", "image", "productImage"):
        v = o.get(k)
        if isinstance(v, str) and v.startswith("http") and re.search(r"\.(jpe?g|png|webp)", v, re.I):
            return v
    return ""

def _longest_text(o, best=""):
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, str) and any(x in k.lower() for x in ("description", "content", "detail")) and len(v) > len(best):
                best = v
            else:
                best = _longest_text(v, best)
    elif isinstance(o, list):
        for v in o:
            best = _longest_text(v, best)
    return best

def nextjs_search(src, search_url, item_url, keyword, prefix):
    data = get_next_data(search_url.format(q=urllib.parse.quote(keyword)))
    out, seen = [], set()
    for o in find_products(data):
        pid = str(next(o[k] for k in ID_KEYS if o.get(k) not in (None, "")))
        if pid in seen:
            continue
        seen.add(pid)
        state = str(o.get("state") or o.get("status") or o.get("productState") or "").upper()
        if any(x in state for x in ("SOLD", "RESERV", "COMPLETE", "CLOSE")):
            continue
        link = next((o[k] for k in ("url", "link", "productUrl", "webUrl") if isinstance(o.get(k), str) and o[k].startswith("http") and not re.search(r"\.(jpe?g|png|webp)", o[k], re.I)), None)
        try:
            price = int(float(str(next(o[k] for k in PRICE_KEYS if o.get(k) is not None)).replace(",", "")))
        except (ValueError, StopIteration):
            price = 0
        out.append({"id": f"{prefix}:{pid}", "src": src,
                    "title": next(o[k] for k in TITLE_KEYS if isinstance(o.get(k), str)).strip(),
                    "content": "", "price": price,
                    "region": next((o[k] for k in ("mainLocationName", "locationName", "location", "region", "address") if isinstance(o.get(k), str) and o[k]), "택배"),
                    "status": None, "url": link or item_url.format(id=pid), "thumb": _first_img(o)})
    return out

DESC_KEYS = ("productDescription", "description", "content")

def _product_desc(o):
    """상품 객체(가격이 같이 있는 dict) 안의 본문을 우선 찾음 — 안내문·메타태그의 description은 건너뜀."""
    if isinstance(o, dict):
        if any(k in o for k in PRICE_KEYS) or "productDescription" in o:
            for k in DESC_KEYS:
                if isinstance(o.get(k), str) and len(o[k]) > 10:
                    return o[k]
        for v in o.values():
            r = _product_desc(v)
            if r:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _product_desc(v)
            if r:
                return r
    return ""

def nextjs_detail(url):
    data = get_next_data(url)
    return _product_desc(data) or _longest_text(data)


# =============== N플리마켓 (공개 검색 API) ===============
NF_API = "https://apis.fleamarket.naver.com/product/reader/v1/market-products/search"

def naver_flea_search(keyword):
    data = get_json(NF_API + "?" + urllib.parse.urlencode({"query": keyword, "start": 1, "limit": 40}))
    out = []
    for a in ((data.get("result") or {}).get("marketProducts")) or []:
        if a.get("saleStatus") not in (None, "ON_SALE"):
            continue
        pid = a.get("marketProductId")
        if not pid:
            continue
        out.append({"id": "nf:" + pid, "src": "N플리마켓", "title": (a.get("title") or "").strip(),
                    "content": "", "price": int(a.get("price") or 0), "region": "택배/직거래",
                    "status": None, "url": f"https://fleamarket.naver.com/market-products/{pid}",
                    "thumb": a.get("productImageUrl") or ""})
    return out


SOURCE_DEFAULTS = {
    "daangn": True,
    "bunjang": True,
    "joongna": {"search_url": "https://web.joongna.com/search/{q}?sort=RECENT_SORT",
                "item_url": "https://web.joongna.com/product/{id}"},
    "naver_flea": True,
}

def collect(cfg, w, ctx):
    """프로젝트가 고른 사이트들을 검색. 한 사이트가 실패해도 나머지는 계속.
    같은 회차에 여러 프로젝트가 같은 검색을 하면 ctx['cache']에서 재사용(사이트 부담·시간 절약).
    반환: (매물 목록, 사이트별 결과 {site_id: 건수 또는 오류 문구})"""
    off = {k for k, v in (cfg.get("sources") or {}).items() if v is False}
    kws = w["keywords"]
    delay = cfg.get("request_delay_sec", 3)
    items, keys = [], set()
    def add(lst):
        for it in lst:
            k = re.sub(r"\W", "", it["title"].lower())[:30] + str(it["price"])  # 사이트 간 중복글 제거
            if it["id"] not in keys and k not in keys:
                keys.update((it["id"], k)); items.append(it)
    jobs = []  # (site_id, 캐시키 보조, 검색함수)
    for sd in w.get("site_defs") or []:
        sid, kind = sd["id"], sd["kind"]
        if kind in off:
            continue
        if kind == "daangn":
            for rg in w.get("regions") or []:
                jobs.append((sid, rg, lambda kw, rg=rg: search(kw, resolve_region(rg, ctx["region_cache"]))))
        elif kind == "bunjang":
            jobs.append((sid, "", bunjang_search))
        elif kind == "joongna":
            d = SOURCE_DEFAULTS["joongna"]
            jobs.append((sid, "", lambda kw, d=d: nextjs_search("중고나라", d["search_url"], d["item_url"], kw, "jn")))
        elif kind == "naver_flea":
            jobs.append((sid, "", naver_flea_search))
        elif kind == "generic" and sd.get("search_url"):
            jobs.append((sid, "", lambda kw, sd=sd: generic.search(sd["id"], sd["name"], sd["search_url"], kw)))
    stats = {}
    for sid, rg, fn in jobs:
        try:
            for kw in kws:
                ck = (sid, rg, kw)
                if ck not in ctx["cache"]:
                    ctx["cache"][ck] = fn(kw)
                    time.sleep(delay)
                got = [dict(it, site=sid) for it in ctx["cache"][ck]]
                stats[sid] = (stats[sid] if isinstance(stats.get(sid), int) else 0) + len(got)
                add(got)
        except SystemExit as e:  # 동네 이름 오류 등 설정 문제
            stats[sid] = str(e)[:60]
            print(f"  ⚠️ {sid}: {e}")
        except Exception as e:
            stats.setdefault(sid, "검색 실패")
            print(f"  ⚠️ {sid} 실패: {e}")
    if any(sd["kind"] == "daangn" for sd in w.get("site_defs") or []) and not w.get("regions"):
        stats["daangn"] = "동네 미입력"
    print("  사이트별 매물:", ", ".join(f"{k} {v}" for k, v in stats.items()) or "없음")
    return items, stats

def fetch_content(it):
    try:
        if it["src"] == "당근":
            return fetch_detail(it["url"])
        if it["src"] == "번개장터":
            return bunjang_detail(it["id"].split(":", 1)[1])
        if it["src"] in ("중고나라", "N플리마켓"):
            return nextjs_detail(it["url"])
        return generic.detail(it["url"])
    except Exception as e:
        print("  상세 조회 실패:", it["src"], e)
        return ""


# =============== AI 평가 ===============
SYSTEM = """너는 한국 중고거래(당근) 매물을 감정하는 전문가야. 사용자가 원하는 조건(레벨·스타일·예산)을 보고
각 매물을 평가해. 반드시 JSON 배열만 출력하고 다른 말은 쓰지 마.
각 원소: {"id": 매물id 문자열,
 "relevant": 사용자가 찾는 물건 자체가 맞으면 true (부품만·액세서리·다른 물건·삽니다 글이면 false),
 "model": 브랜드+모델명(모르면 "불명"),
 "specs": 핵심 스펙 요약(모델 연식·크기·용량·구성품 등, 25자 이내),
 "fit": 0~100 사용자 레벨·스타일 적합도,
 "value": 0~100 가격 가치(모델·연식·상태 대비 중고 시세를 고려해 쌀수록 높게),
 "summary": 한줄 평(30자 이내, 장점 위주),
 "cons": 주의할 점(20자 이내, 없으면 "")}
같은 물건이 여러 사이트에 올라올 수 있어. 사용자가 거래 방식(직거래/택배) 선호를 말했다면 fit에 반영해.
글에 정보가 부족하면 추측하지 말고 fit을 보수적으로 줘. 상태가 나쁘거나 정품 의심이면 value를 낮춰."""

def ai_evaluate(cfg, w, items, top_ctx):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY 시크릿이 없어요.")
    listing = [{"id": i["id"], "사이트": i["src"],
                "거래": "동네 직거래" if i["src"] == "당근" else "택배/직거래(전국)",
                "제목": i["title"], "가격": i["price"], "지역": i["region"],
                "본문": i["content"][:700]} for i in items]
    user = (f"[내가 원하는 것]\n{w['want']}\n\n"
            f"[현재 상위 매물 참고(비교 기준)]\n{json.dumps(top_ctx, ensure_ascii=False)}\n\n"
            f"[평가할 매물]\n{json.dumps(listing, ensure_ascii=False)}")
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    if os.environ.get("ANTHROPIC_WORKSPACE_ID"):  # 워크스페이스에 묶이지 않은 조직 키일 때 필요
        headers["anthropic-workspace-id"] = os.environ["ANTHROPIC_WORKSPACE_ID"].strip()
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=120, headers=headers,
        json={"model": cfg.get("ai_model", "claude-haiku-4-5-20251001"), "max_tokens": 4000,
              "system": SYSTEM, "messages": [{"role": "user", "content": user}]})
    if r.status_code != 200:
        print("  AI 호출 실패:", r.text[:300])
        return {}
    text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    text = re.sub(r"```(json)?", "", text).strip()
    m = re.search(r"\[.*\]", text, re.S)
    try:
        arr = json.loads(m.group(0) if m else text)
    except Exception:
        print("  AI 응답 해석 실패:", text[:300])
        return {}
    return {str(e.get("id")): e for e in arr if isinstance(e, dict)}


# =============== 이메일 ===============
SITE_URL = os.environ.get("SITE_URL") or "https://gundeak-gosu.github.io/used-ai-watch/"

def mail_env():
    user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
    if not user or not pw:
        raise SystemExit("SMTP_USER / SMTP_PASS 시크릿이 없어요.")
    return (os.environ.get("SMTP_HOST") or "smtp.gmail.com", int(os.environ.get("SMTP_PORT") or 465), user, pw)

def _send(to, subject, html):
    host, port, user, pw = mail_env()
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"중고 AI 알리미 <{user}>", to
    html += (f'<p style="color:#999;font-size:12px;margin-top:24px">중고 AI 알리미 · '
             f'<a href="{SITE_URL}">내 프로젝트 관리 / 마감하기</a></p>')
    msg.set_content(re.sub(r"<[^>]+>", "", html))
    msg.add_alternative(html, subtype="html")
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                s.login(user, pw); s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(); s.login(user, pw); s.send_message(msg)
        return True
    except Exception as e:
        print("  메일 전송 실패:", e)
        return False

def _btn(url, label="매물 보기"):
    return (f'<a href="{esc(url)}" style="display:inline-block;padding:8px 14px;background:#ff6f0f;color:#fff;'
            f'border-radius:6px;text-decoration:none;font-weight:bold">{label}</a>')

def send_text(to, text, url):
    lines = text.split("\n")
    body = "<br>".join(esc(x) for x in lines[1:])
    html = (f'<div style="font-family:sans-serif;font-size:15px;line-height:1.6">'
            f'<h3 style="margin:0 0 8px">{esc(lines[0])}</h3><p>{body}</p>{_btn(url)}</div>')
    return _send(to, lines[0][:80], html)

def send_compare(to, header, ranked, n=3):
    """상위 매물을 사진·점수·장단점과 함께 표로 나란히 비교."""
    rows = []
    for k, it in enumerate(ranked[:n], 1):
        ev = it["ev"]
        img = (f'<img src="{esc(it["thumb"])}" width="96" height="96" style="object-fit:cover;border-radius:8px">'
               if it.get("thumb") else "")
        rows.append(
            f'<tr><td style="padding:10px;vertical-align:top">{img}</td>'
            f'<td style="padding:10px;vertical-align:top"><b>{k}위 · {it["score"]}점</b> '
            f'<span style="color:#888">(적합 {ev.get("fit")} / 가치 {ev.get("value")})</span><br>'
            f'<b>{esc(ev.get("model", ""))}</b> · {won(it["price"])} · [{esc(it["src"])}] {esc(it.get("region") or "")}<br>'
            f'<span style="color:#555">{esc(ev.get("specs", ""))}</span><br>'
            f'👍 {esc(ev.get("summary", ""))}' + (f'<br>⚠️ {esc(ev["cons"])}' if ev.get("cons") else "") +
            f'<br><a href="{esc(it["url"])}">매물 보기 →</a></td></tr>')
    table = ('<table style="border-collapse:collapse">' + "".join(rows) + "</table>") if rows \
        else "<p>(아직 조건에 맞는 매물이 없어요. 새로 올라오면 알려드릴게요.)</p>"
    html = f'<div style="font-family:sans-serif;font-size:14px;line-height:1.5"><h3>{esc(header)}</h3>{table}</div>'
    return _send(to, header[:80], html)


# =============== Supabase (봇 전용 함수 호출) ===============
def sb_rpc(fn, **args):
    url, key, tok = (os.environ.get(k) for k in ("SUPABASE_URL", "SUPABASE_KEY", "BOT_TOKEN"))
    if not (url and key and tok):
        raise SystemExit("SUPABASE_URL / SUPABASE_KEY / BOT_TOKEN 시크릿이 없어요.")
    r = requests.post(f"{url.rstrip('/')}/rest/v1/rpc/{fn}", timeout=60,
                      headers={"apikey": key, "Content-Type": "application/json"},
                      json={"p_token": tok, **args})
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase {fn} 실패: {r.status_code} {r.text[:200]}")
    return r.json() if r.text else None


# =============== 메인 ===============
def norm(s):
    return re.sub(r"\s+", "", s or "").lower()

def items_for_web(items, cand, evals, pw, exc, lo, hi):
    """웹 '매물 목록' 화면용 — 모든 매물을 추천순(AI 점수)으로, 평가 전·제외된 것은 사유와 함께 뒤에."""
    cand_ids = {i["id"] for i in cand}
    out = []
    for i in items:
        e = {"id": i["id"], "site": i.get("site"), "src": i["src"], "title": i["title"][:90], "price": i["price"],
             "url": i["url"], "thumb": i.get("thumb") or "", "region": i.get("region") or ""}
        if i.get("kept"):
            e["kept"] = True
        ev = evals.get(i["id"])
        if i["id"] not in cand_ids:
            if not lo <= i["price"] <= hi:
                e["state"], e["why"] = "filtered", "가격 범위 밖"
            elif str(i["status"] or "").upper() in ("CLOSED", "SOLD", "SOLD_OUT", "RESERVED"):
                e["state"], e["why"] = "filtered", "판매완료·예약중"
            else:
                hit = next((x for x in exc if x in norm(i["title"] + i["content"])), "")
                e["state"], e["why"] = "filtered", f"제외어 '{hit}'" if hit else "제외됨"
        elif ev is None:
            e["state"] = "pending"
        else:
            e.update({k: ev.get(k) for k in ("model", "specs", "summary", "cons", "fit", "value")})
            e["score"] = round((1 - pw) * ev.get("fit", 0) + pw * ev.get("value", 0))
            e["state"] = "fit" if ev.get("relevant") else "unrelated"
        out.append(e)
    order = {"fit": 0, "unrelated": 1, "pending": 2, "filtered": 3}
    out.sort(key=lambda e: (order[e["state"]], -(e.get("score") or 0), e["price"]))
    return out

def top_for_web(ranked, n=5):
    keys = ("model", "specs", "summary", "cons", "fit", "value")
    return [{**{k: it["ev"].get(k) for k in keys}, "score": it["score"], "price": it["price"], "src": it["src"],
             "region": it.get("region"), "url": it["url"], "thumb": it.get("thumb")} for it in ranked[:n]]

def run_project(cfg, p, ctx, now):
    pw = cfg.get("price_weight", 0.4)
    delay = cfg.get("request_delay_sec", 3)
    name, to = p["name"], p["notify_email"]
    st = p.get("state") or {}
    evals = dict(st.get("evals") or {})
    seen = set(st.get("seen") or [])
    first_run = not st.get("initialized")
    w = {"keywords": p["keywords"], "want": p["want"], "regions": p.get("regions") or [],
         "site_defs": p.get("site_defs") or []}

    items, stats = collect(cfg, w, ctx)
    ids = {i["id"] for i in items}  # 이번 회차에 실제로 본 매물

    # 누적: 한 번 찾은 매물은 기억해 두고, 이번에 안 보여도(사이트 일시 차단·검색결과 밀림) 며칠은 계속 비교에 포함
    keep_sec = cfg.get("keep_days", 3) * 86400
    t = int(time.time())
    catalog = dict(st.get("catalog") or {})
    for i in items:
        old = catalog.get(i["id"]) or {}
        catalog[i["id"]] = {k: i.get(k) for k in ("site", "src", "title", "price", "url", "thumb", "region", "status")}
        catalog[i["id"]].update(first=old.get("first", t), last=t)
    kept = []
    for cid, c in list(catalog.items()):
        if cid in ids:
            continue
        if t - c.get("last", 0) > keep_sec:
            catalog.pop(cid)  # 오래 안 보이면(판매완료·삭제 추정) 정리
            continue
        kept.append(dict(c, id=cid, content="", kept=True))
    if len(catalog) > 1500:
        for cid in sorted(catalog, key=lambda k: catalog[k].get("last", 0))[: len(catalog) - 1500]:
            catalog.pop(cid)
    now_counts = {k: v for k, v in stats.items() if isinstance(v, int)}
    items = items + kept
    # 사이트 칩에는 '지금 비교 중인 매물 수'(이번에 본 것 + 유지 중인 것)를 보여줌
    for sid in stats:
        n_all = sum(1 for i in items if i.get("site") == sid)
        if isinstance(stats[sid], int) or n_all:
            stats[sid] = n_all
    problems = []
    for sid, v in now_counts.items():
        n_kept = sum(1 for i in kept if i.get("site") == sid)
        if v == 0 and n_kept:
            sname = next((d["name"] for d in w["site_defs"] if d["id"] == sid), sid)
            problems.append(f"{sname} 이번 확인 0건(이전 결과 {n_kept}개 유지)")
    exc = [norm(x) for x in (p.get("exclude") or []) + cfg.get("default_exclude", [])]
    lo, hi = p.get("min_price") or 1, p.get("max_price") or 10**12
    cand = [i for i in items
            if str(i["status"] or "").upper() not in ("CLOSED", "SOLD", "SOLD_OUT", "RESERVED")
            and not any(x in norm(i["title"] + i["content"]) for x in exc)
            and lo <= i["price"] <= hi]

    # 새 매물만 AI가 읽고 평가 (이미 평가한 건 재사용 → 비용 절약). 첫 회차는 넉넉히.
    new = [i for i in cand if i["id"] not in evals][: (cfg.get("first_run_ai_max", 120) if first_run
                                                         else cfg.get("ai_max_per_run", 30))]
    for i in new:
        if len(i["content"]) < 30:
            if i["id"] not in ctx["detail"]:
                ctx["detail"][i["id"]] = fetch_content(i)
                time.sleep(delay)
            i["content"] = ctx["detail"][i["id"]] or i["content"]

    def ranked_now():
        out = []
        for i in cand:
            ev = evals.get(i["id"])
            if ev and ev.get("relevant"):
                i = dict(i, ev=ev)
                i["score"] = round((1 - pw) * ev.get("fit", 0) + pw * ev.get("value", 0))
                out.append(i)
        return sorted(out, key=lambda x: (-x["score"], x["price"]))

    ai_fail = False
    for b in range(0, len(new), 10):
        ctx_top = [{"model": x["ev"].get("model"), "가격": x["price"], "점수": x["score"]} for x in ranked_now()[:5]]
        res = ai_evaluate(cfg, w, new[b:b + 10], ctx_top)
        ai_fail |= not res
        for i in new[b:b + 10]:
            if i["id"] in res:
                evals[i["id"]] = res[i["id"]]
    ranked = ranked_now()
    print(f"  매물 {len(items)}개 → 1차 통과 {len(cand)}개 → AI 신규 평가 {len(new)}개 → 적합 {len(ranked)}개")

    if first_run and new and not any(i["id"] in evals for i in new):
        # 첫 회차인데 AI 평가 전부 실패 → 시작 처리하지 않고 다음 회차에 다시
        print("  ⚠️ AI 평가가 모두 실패해 첫 회차를 다음에 다시 시도합니다.")
        return {"state": dict(st, catalog=catalog), "top": None, "note": "첫 분석 재시도 대기 중(AI 오류)", "stats": stats}

    # 알림
    top_n, min_score = 3, p.get("min_score") or 65
    if first_run:
        send_compare(to, f"🛒 [{name}] 감시 시작! 현재 TOP3", ranked)
    else:
        prev_best = st.get("best")
        for rank, it in enumerate(ranked[:top_n]):
            if it["id"] in seen or it["score"] < min_score:
                continue
            ev = it["ev"]
            head = "🏆 새 1위 등장!" if rank == 0 and it["id"] != prev_best else f"🆕 {rank+1}위로 진입!"
            cmp_ = ""
            if rank > 0:
                b = ranked[0]
                cmp_ = f"\n1위({b['src']} {b['score']}점 {won(b['price'])})보다 {b['score']-it['score']}점 낮음"
            elif len(ranked) > 1:
                b = ranked[1]
                cmp_ = f"\n기존 1위 {b['ev'].get('model','')[:14]}({won(b['price'])})보다 {it['score']-b['score']}점↑"
            text = (f"🛒[{name}] {head}\n{ev.get('model','')} · {won(it['price'])} [{it['src']}]\n"
                    f"{it['score']}점(적합 {ev.get('fit')}/가치 {ev.get('value')})\n"
                    f"👍 {ev.get('summary','')}" + (f"\n⚠️ {ev['cons']}" if ev.get("cons") else "") + cmp_)
            send_text(to, text, it["url"])
            print("  📩", head, ev.get("model"), it["price"])

    # 하루 한 번 비교 요약
    hour, today = cfg.get("daily_summary_hour"), now.strftime("%Y-%m-%d")
    summary_sent = st.get("summary_sent")
    if hour is not None and now.hour >= hour and summary_sent != today and not first_run:
        send_compare(to, f"📊 [{name}] 오늘의 TOP3 비교", ranked)
        summary_sent = today

    # AI 평가가 실패해 점수가 없는 매물은 '본 것'으로 치지 않음(첫 회차 기존 매물은 전부 본 것 처리)
    pending = set() if first_run else {i["id"] for i in cand if i["id"] not in evals}
    seen = list(seen | (ids - pending))[-1500:]
    if len(evals) > 600:  # 오래된 평가 정리
        keep = {i["id"] for i in cand} | set(catalog)
        for k in list(evals)[: len(evals) - 600]:
            if k not in keep:
                evals.pop(k, None)
    state = {"initialized": True, "evals": evals, "seen": seen, "summary_sent": summary_sent,
             "best": ranked[0]["id"] if ranked else st.get("best"), "catalog": catalog}
    note = (f"매물 {len(items)}개 비교 중(이번 확인 {len(ids)}개) · 적합 {len(ranked)}개"
            + (" · ⚠️ AI 평가 일부 실패" if ai_fail else "") + "".join(f" · ⚠️ {x}" for x in problems))
    return {"state": state, "top": top_for_web(ranked), "note": note, "stats": stats,
            "items": items_for_web(items, cand, evals, pw, exc, lo, hi)}

def run():
    with open("settings.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    now = datetime.now(KST)
    projects = sb_rpc("bot_list_projects")
    # 오래 안 돈 프로젝트부터(시간 초과 시에도 공평하게)
    projects.sort(key=lambda p: p.get("last_run_at") or "")
    print(f"진행 중인 프로젝트 {len(projects)}개")
    ctx = {"region_cache": {}, "cache": {}, "detail": {}}
    budget = time.time() + cfg.get("run_budget_min", 20) * 60
    for p in projects:
        if time.time() > budget:
            print("⏱ 시간 예산 초과 — 나머지는 다음 회차에")
            break
        print(f"▶ {p['name']} ({p['id'][:8]})")
        try:
            r = run_project(cfg, p, ctx, now)
        except Exception as e:
            print("  ❌ 처리 실패:", e)
            r = {"state": p.get("state") or {}, "top": None, "note": f"⚠️ 처리 오류: {str(e)[:80]}", "stats": None}
        sb_rpc("bot_save_project", p_project_id=p["id"], p_state=r["state"], p_top=r["top"], p_note=r["note"],
               p_site_stats=r.get("stats"), p_items=r.get("items"))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "mailtest":
        ok = send_text(sys.argv[2], "✅ 중고 AI 알리미 메일 연결 테스트\n이 메일이 보이면 설정 완료예요!", SITE_URL)
        raise SystemExit(0 if ok else "메일 전송 실패 — SMTP 설정을 확인하세요.")
    run()
