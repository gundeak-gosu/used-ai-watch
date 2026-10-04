// 당근 검색 중계 — 당근은 해외(GitHub 서버) 접속을 간헐적으로 막아서, 서울 리전에서 대신 검색해 돌려준다.
// 호출: 봇만 (BOT_TOKEN 확인). 요청 시 x-region: ap-northeast-2 헤더로 서울에서 실행.
import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "npm:@supabase/supabase-js@2";

const BASE = "https://www.daangn.com";
const UA = {
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
  "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
  "Accept-Language": "ko-KR,ko;q=0.9",
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

async function getJson(url: string) {
  const r = await fetch(url, { headers: UA, signal: AbortSignal.timeout(20000) });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return await r.json();
}

async function resolveRegion(text: string) {
  // '역삼동-6035' 형식이면 그대로, '강남구 역삼동' 같은 이름이면 당근 지역 API로 변환
  if (/.+-\d+$/.test(text.trim())) return { slug: text.trim(), full: text.trim() };
  const tokens = text.trim().split(/\s+/);
  const data = await getJson(`${BASE}/kr/api/v1/regions/keyword?keyword=${encodeURIComponent(tokens[tokens.length - 1])}`);
  const locs: any[] = data.locations || [];
  const full = (x: any) => ["name1", "name2", "name3", "name"].map((k) => x[k] || "").join(" ");
  const c = locs.filter((x) => tokens.every((t) => full(x).includes(t)))[0] || locs[0];
  if (!c) return null;
  return { slug: `${c.name}-${c.id}`, full: full(c).trim() };
}

async function search(keyword: string, slug: string) {
  const qs = new URLSearchParams({ in: slug, q: keyword, only_on_sale: "true", _data: "routes/kr.search.buy-sell._index" });
  const url = `${BASE}/kr/search/buy-sell/?${qs}`;
  let data = await getJson(url);
  let arts: any[] = data.buySellArticles || [];
  let retried = false;
  if (!arts.length) {  // 빈 결과면 잠시 후 한 번 더
    await new Promise((r) => setTimeout(r, 4000));
    data = await getJson(url); arts = data.buySellArticles || []; retried = true;
  }
  const raw = arts.length;
  const near = new Set([data.searchRegion || {}, ...(data.nearbyRegions || [])].map((r: any) => r.name).filter(Boolean));
  if (near.size) arts = arts.filter((a) => near.has((a.region || {}).name));
  return { raw, retried, articles: arts.map((a) => ({
    id: a.id, href: a.href, title: a.title, content: a.content, price: a.price, status: a.status,
    region: (a.region || {}).name || "", thumbnail: a.thumbnail || "" })) };
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "POST only" }, 405);
  const body = await req.json().catch(() => ({}));
  const admin = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
  const { error } = await admin.rpc("bot_ping", { p_token: String(body.token || "") });
  if (error) return json({ error: "unauthorized" }, 401);
  try {
    if (body.action === "region") {
      const r = await resolveRegion(String(body.region || ""));
      return r ? json(r) : json({ error: "region not found" }, 404);
    }
    const r = await search(String(body.keyword || ""), String(body.slug || ""));
    return json({ ...r, served_from: Deno.env.get("SB_REGION") || "?" });
  } catch (e) {
    return json({ error: String(e) }, 502);
  }
});
