"""범용 사이트 읽기 — 공식 API가 없는 사이트(전문 중고몰·사용자 추가 사이트)용.
검색 결과 페이지에서 '링크 + 가격'이 같이 있는 덩어리를 매물로 보고 뽑아냅니다.
(구조화 데이터(JSON-LD, Next.js)가 있으면 그걸 먼저 씁니다.)"""
import hashlib, json, re, urllib.parse
import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
      "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"}

PRICE_RE = re.compile(r"(?:₩|\$|US\s?\$|USD)\s?([\d,]+(?:\.\d+)?)|([\d]{1,3}(?:,\d{3})+|\d{4,})\s*(?:원|KRW)")
SKIP_TEXT = re.compile(r"^(장바구니|관심|찜|구매|바로구매|상세보기|more|view|자세히|share|공유|\d+)$", re.I)
LABEL_RE = re.compile(r"^\s*(판매가|소비자가|정가|할인가|가격|price|상품명|제조사|모델명|연식)\s*[:：]?", re.I)
USD_KRW = 1400  # 달러 표기 사이트는 대략 원화로 환산(비교용)


class Blocked(Exception):
    pass


def fetch(url):
    r = requests.get(url, headers=UA, timeout=25)
    if r.status_code in (403, 429, 503):
        raise Blocked(f"HTTP {r.status_code}")
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r.text, r.url


def parse_price(text):
    m = PRICE_RE.search(text or "")
    if not m:
        return 0
    if m.group(1):  # 달러
        return int(float(m.group(1).replace(",", "")) * USD_KRW)
    return int(m.group(2).replace(",", ""))


def _jsonld_items(soup, base):
    out = []
    def walk(o):
        if isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            t = o.get("@type")
            if t == "Product" or (isinstance(t, list) and "Product" in t):
                offers = o.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = offers.get("price") or offers.get("lowPrice") or 0
                try:
                    price = int(float(str(price).replace(",", "")))
                except ValueError:
                    price = 0
                if str(offers.get("priceCurrency", "KRW")).upper() == "USD":
                    price *= USD_KRW
                img = o.get("image")
                img = img[0] if isinstance(img, list) and img else img
                url = o.get("url") or offers.get("url") or ""
                if o.get("name") and url:
                    out.append({"title": o["name"], "price": price, "url": urllib.parse.urljoin(base, url),
                                "thumb": img if isinstance(img, str) else "", "content": o.get("description") or ""})
            for v in o.values():
                walk(v)
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(s.string or ""))
        except ValueError:
            pass
    return out


def _card_items(soup, base):
    """링크마다 가까운 상위 요소(카드)를 올라가며 가격이 보이는 덩어리를 찾음."""
    host = urllib.parse.urlparse(base).netloc
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "javascript", "mailto", "tel")):
            continue
        url = urllib.parse.urljoin(base, href)
        if urllib.parse.urlparse(url).netloc != host or url in seen:
            continue
        card = a
        for _ in range(5):
            txt = card.get_text(" ", strip=True)
            if PRICE_RE.search(txt) and len(txt) < 600:
                break
            card = card.parent
            if card is None or card.name in ("body", "html"):
                card = None
                break
        else:
            card = None
        if card is None:
            continue
        price = parse_price(card.get_text(" ", strip=True))
        texts = [t.strip() for t in card.stripped_strings if not PRICE_RE.fullmatch(t.strip())]
        img = card.find("img")
        cands = [a.get("title") or "", a.get_text(" ", strip=True), img.get("alt", "") if img else ""] + texts
        cands = [c for c in cands if len(c) >= 4 and not SKIP_TEXT.match(c) and not PRICE_RE.search(c)
                 and not LABEL_RE.match(c)]
        if not cands or price <= 0:
            continue
        title = max(cands[:4] or cands, key=len)[:120]
        thumb = ""
        if img:
            thumb = img.get("data-src") or img.get("data-original") or img.get("src") or ""
            thumb = urllib.parse.urljoin(base, thumb) if thumb and not thumb.startswith("data:") else ""
        seen.add(url)
        out.append({"title": title, "price": price, "url": url, "thumb": thumb, "content": ""})
    # 같은 카드를 가리키는 링크가 여러 개면(사진+제목) 하나만
    uniq, keys = [], set()
    for it in out:
        k = (re.sub(r"\W", "", it["title"].lower()), it["price"])
        if k not in keys:
            keys.add(k); uniq.append(it)
    return uniq


ID_PARAM = re.compile(r"(^|_)(no|id|pid|uid|idx|seq|num|code|goods|item|product|branduid|it_id)$", re.I)
TRACK_PARAM = re.compile(r"^(_|utm|ref|gclid|fbclid|sid$|session|pos$|ss$|sort|page|cate|cat_no|keyword|q$|search)", re.I)

def stable_url(url):
    """같은 매물인지 판단할 주소 — 검색할 때마다 바뀌는 꼬리표(_sid, _pos, utm 등)는 떼고 상품번호류만 남김."""
    u = urllib.parse.urlsplit(url)
    keep = sorted((k, v) for k, v in urllib.parse.parse_qsl(u.query)
                  if ID_PARAM.search(k) and not TRACK_PARAM.match(k))
    return urllib.parse.urlunsplit((u.scheme, u.netloc.lower().removeprefix("www."), u.path.rstrip("/"),
                                    urllib.parse.urlencode(keep), ""))


def search(site_id, site_name, search_url, keyword):
    """search_url 안의 {q}를 키워드로 바꿔 검색. {q}가 없으면 그 목록 페이지를 읽고 제목에 키워드가 든 것만."""
    has_q = "{q}" in search_url
    url = search_url.replace("{q}", urllib.parse.quote(keyword)) if has_q else search_url
    html, final = fetch(url)
    soup = BeautifulSoup(html, "html.parser")
    items = _jsonld_items(soup, final) or _card_items(soup, final)
    if not has_q:
        words = [w for w in re.split(r"\s+", keyword.lower()) if w]
        items = [i for i in items if all(w in i["title"].lower().replace(" ", "") or w in i["title"].lower() for w in words)]
    out = []
    for i in items[:60]:
        h = hashlib.md5(stable_url(i["url"]).encode()).hexdigest()[:12]
        out.append({"id": f"{site_id}:{h}", "src": site_name, "title": i["title"], "content": i.get("content") or "",
                    "price": i["price"], "region": "택배/방문", "status": None, "url": i["url"], "thumb": i["thumb"]})
    return out


def detail(url):
    """상세 페이지 본문(설명) 추출 — 가장 긴 설명 덩어리."""
    html, final = fetch(url)
    soup = BeautifulSoup(html, "html.parser")
    for s in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        s.decompose()
    for sel in ("[itemprop=description]", "#prdDetail", ".product-detail", ".detail", "#detail", ".desc", ".description"):
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 30:
            return el.get_text("\n", strip=True)[:2000]
    m = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    return (m.get("content") if m else "") or ""
