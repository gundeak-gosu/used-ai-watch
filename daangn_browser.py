"""당근 검색 — 실제 브라우저(Playwright, 헤드리스 크롬)로.
당근은 브라우저 쿠키가 없는 요청에는 '빈 결과'를 주는 경우가 많아서, 홈을 한 번 연 뒤(쿠키 받기) 검색 페이지를 열고
페이지 안에 들어 있는 매물 데이터(window.__remixContext)를 읽는다."""
import time, urllib.parse

BASE = "https://www.daangn.com"
ROUTE = "routes/kr.search.buy-sell._index"


class DaangnBrowser:
    def __init__(self):
        self._pw = self._browser = self._page = None

    def _open(self):
        if self._page:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        ctx = self._browser.new_context(locale="ko-KR", timezone_id="Asia/Seoul")
        self._page = ctx.new_page()
        self._page.goto(f"{BASE}/kr/", wait_until="domcontentloaded", timeout=45000)  # 쿠키 받기
        self._page.wait_for_timeout(2500)

    def close(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        finally:
            self._pw = self._browser = self._page = None

    def _load(self, keyword, slug):
        url = f"{BASE}/kr/search/buy-sell/?" + urllib.parse.urlencode({"in": slug, "q": keyword, "only_on_sale": "true"})
        self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
        return self._page.evaluate(
            f"() => (window.__remixContext && window.__remixContext.state.loaderData['{ROUTE}']) || null") or {}

    def search(self, keyword, slug):
        """bot.search()와 같은 모양의 매물 목록을 돌려줌(내 동네 + 인근 동네만)."""
        self._open()
        data = self._load(keyword, slug)
        if not data.get("buySellArticles"):
            time.sleep(4)
            data = self._load(keyword, slug)
        arts = data.get("buySellArticles") or []
        near = {r.get("name") for r in [data.get("searchRegion") or {}] + (data.get("nearbyRegions") or [])} - {None}
        if near:
            arts = [a for a in arts if (a.get("region") or {}).get("name") in near]
        out = []
        for a in arts:
            href = a.get("href") or ""
            link = href if href.startswith("http") else BASE + href
            try:
                price = int(float(a.get("price") or 0))
            except (TypeError, ValueError):
                price = 0
            out.append({"id": "dg:" + str(a.get("id") or link), "src": "당근", "title": (a.get("title") or "").strip(),
                        "content": a.get("content") or "", "price": price,
                        "region": (a.get("region") or {}).get("name", ""),
                        "status": a.get("status"), "url": link, "thumb": a.get("thumbnail") or ""})
        return out
