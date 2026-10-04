// 물건에 맞는 전문 중고 사이트를 AI가 웹검색으로 찾아 추천하고, 실제로 검색이 되는지 확인한 뒤 사이트 목록에 등록한다.
// 호출: 로그인한 사용자만 (verify_jwt). 1인당 하루 10회.
import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import Anthropic from "npm:@anthropic-ai/sdk";
import { createClient } from "npm:@supabase/supabase-js@2";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { ...CORS, "Content-Type": "application/json" } });

const DAILY_LIMIT = 10;
const GENERAL = ["daangn.com", "bunjang.co.kr", "joongna.com", "fleamarket.naver.com"];
const PRICE_RE = /(?:₩|\$|US\s?\$|USD)\s?[\d,]{2,}|[\d]{1,3}(?:,\d{3})+\s*원|\d{4,}\s*원/g;

const SYSTEM = `너는 한국 사용자를 돕는 중고거래 사이트 전문가야. 사용자가 찾는 물건에 특화된 중고 거래처를 웹검색으로 찾아.
- 당근·번개장터·중고나라·N플리마켓 같은 종합 중고앱은 제외하고, 그 물건 분야 전문 중고몰·중고장터·리퍼/전시품 몰·전문 커뮤니티 장터를 찾아.
- 국내 사이트를 우선하되, 해외 직구가 흔한 분야면 해외 사이트도 1~2개 포함해.
- 각 사이트의 '검색 결과 페이지' 주소 형식을 실제로 확인해서 검색어 자리에 {q}를 넣어. (예: https://example.com/search?keyword={q})
  로그인해야만 보이는 곳, 검색 기능이 없는 곳, 매물 대신 글만 있는 곳은 빼.
- 3~6개를 추천하고, 마지막에 아래 형식의 JSON 배열만 \`\`\`json 코드블록으로 출력해. 다른 설명은 코드블록 밖에 짧게.
[{"name": "사이트 이름", "home_url": "https://...", "search_url": "https://...{q}...", "category": "분야(예: 실험·연구장비, 카메라, 자전거)", "scope": "국내" 또는 "해외", "why": "추천 이유 40자 이내"}]`;

function slug(u: string) {
  try {
    return "ai-" + new URL(u).hostname.replace(/^www\./, "").replace(/[^a-z0-9]+/gi, "-").slice(0, 40).toLowerCase();
  } catch {
    return "";
  }
}

async function probe(searchUrl: string, keyword: string) {
  // 실제로 검색 결과 페이지가 열리고 가격처럼 보이는 글자가 몇 개 이상 있는지 확인
  try {
    const url = searchUrl.replace("{q}", encodeURIComponent(keyword));
    const r = await fetch(url, {
      headers: { "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
                 "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8" },
      signal: AbortSignal.timeout(12000),
    });
    if (!r.ok) return { ok: false, prices: 0, note: `HTTP ${r.status}` };
    const html = await r.text();
    const prices = (html.match(PRICE_RE) || []).length;
    return { ok: prices >= 3, prices, note: prices >= 3 ? "검색 확인됨" : "가격 정보를 못 읽음(직접 확인 필요)" };
  } catch (e) {
    return { ok: false, prices: 0, note: "접속 실패" };
  }
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return json({ error: "POST만 지원해요." }, 405);

  const admin = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
  const token = (req.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
  const { data: { user } } = await admin.auth.getUser(token);
  if (!user) return json({ error: "로그인이 필요해요." }, 401);

  const since = new Date(Date.now() - 24 * 3600 * 1000).toISOString();
  const { count } = await admin.from("recommend_log").select("id", { count: "exact", head: true })
    .eq("user_id", user.id).gte("created_at", since);
  if ((count ?? 0) >= DAILY_LIMIT) return json({ error: `사이트 추천은 하루 ${DAILY_LIMIT}번까지예요. 내일 다시 시도해 주세요.` }, 429);

  const body = await req.json().catch(() => ({}));
  const name = String(body.name || "").slice(0, 40);
  const keywords: string[] = (Array.isArray(body.keywords) ? body.keywords : []).map(String).slice(0, 5);
  const want = String(body.want || "").slice(0, 1000);
  if (!name || !keywords.length) return json({ error: "프로젝트 이름과 키워드를 먼저 입력해 주세요." }, 400);

  const apiKey = Deno.env.get("ANTHROPIC_API_KEY");
  if (!apiKey) return json({ error: "서버에 AI 키가 설정되지 않았어요(관리자 설정 필요)." }, 500);
  const ws = Deno.env.get("ANTHROPIC_WORKSPACE_ID");
  const client = new Anthropic({ apiKey, ...(ws ? { defaultHeaders: { "anthropic-workspace-id": ws } } : {}) });

  await admin.from("recommend_log").insert({ user_id: user.id });

  const messages: Anthropic.Beta.BetaMessageParam[] = [{
    role: "user",
    content: `찾는 물건: ${name}\n검색 키워드: ${keywords.join(", ")}\n원하는 조건: ${want || "(없음)"}`,
  }];
  let res: Anthropic.Beta.BetaMessage | null = null;
  try {
    for (let i = 0; i < 4; i++) {
      res = await client.beta.messages.create({
        model: Deno.env.get("RECOMMEND_MODEL") || "claude-opus-5-5",
        max_tokens: 16000,
        system: SYSTEM,
        messages,
        tools: [
          { type: "web_search_20260209", name: "web_search", max_uses: 6 },
          { type: "web_fetch_20260209", name: "web_fetch", max_uses: 8 },
        ],
        betas: ["server-side-fallback-2026-07-01"],
        // @ts-ignore: 'default' 대체 모델(안전 분류기 거절 시 서버가 다른 모델로 재시도)
        fallbacks: "default",
      } as any);
      if (res.stop_reason !== "pause_turn") break;
      messages.push({ role: "assistant", content: res.content });
    }
  } catch (e) {
    console.error("anthropic error", e);
    return json({ error: "AI 추천 중 오류가 났어요. 잠시 후 다시 시도해 주세요." }, 502);
  }
  if (!res || res.stop_reason === "refusal") return json({ error: "AI가 이 요청에 답하지 못했어요." }, 502);

  const text = res.content.filter((b: any) => b.type === "text").map((b: any) => b.text).join("\n");
  const m = text.match(/```json\s*([\s\S]*?)```/) || text.match(/(\[[\s\S]*\])/);
  let recs: any[] = [];
  try { recs = JSON.parse(m ? m[1] : "[]"); } catch { recs = []; }

  const out = [];
  for (const r of recs.slice(0, 6)) {
    const search_url = String(r.search_url || "");
    if (!/^https?:\/\//i.test(search_url) || !search_url.includes("{q}")) continue;
    if (GENERAL.some((d) => search_url.includes(d))) continue;
    const id = slug(r.home_url || search_url);
    if (!id) continue;
    const p = await probe(search_url, keywords[0]);
    const row = {
      id, name: String(r.name || id).slice(0, 40), kind: "generic", search_url,
      home_url: r.home_url || null, category: String(r.category || name).slice(0, 30),
      description: String(r.why || "").slice(0, 80), scope: r.scope === "해외" ? "해외" : "국내",
      verified: p.ok, created_by: null,
    };
    // 이미 있는 사이트면 그대로 두고(다른 사람이 쓰는 중일 수 있음) 확인 결과만 반영
    const { data: exist } = await admin.from("sites").select("id, verified").eq("id", id).maybeSingle();
    if (!exist) await admin.from("sites").insert(row);
    else if (p.ok && !exist.verified) await admin.from("sites").update({ verified: true }).eq("id", id);
    out.push({ ...row, verified: p.ok || !!exist?.verified, check: p.note });
  }
  return json({ sites: out });
});
