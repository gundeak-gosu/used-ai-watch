"""당근 검색 점검 — 이 서버에서 브라우저 방식과 일반 요청 방식이 각각 몇 건을 가져오는지 비교."""
import time
import bot
from daangn_browser import DaangnBrowser

TESTS = [("둔산동-5793", "드라이버"), ("부평동-6519", "드라이버"), ("역삼동-6035", "골프")]
b = DaangnBrowser()
try:
    for slug, q in TESTS:
        t = time.time()
        n_browser = len(b.search(q, slug))
        try:
            n_plain = len(bot.search(q, slug))
        except Exception as e:
            n_plain = f"오류 {e}"
        print(f"{slug} '{q}': 브라우저 {n_browser}건 / 일반 요청 {n_plain}건 ({time.time()-t:.0f}초)")
        time.sleep(3)
finally:
    b.close()
