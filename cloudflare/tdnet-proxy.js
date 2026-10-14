// Cloudflare Worker: TDnet の適時開示一覧ページ(I_list_NNN_YYYYMMDD.html)だけを中継する。
// TDnet はブラウザから直接読めない(CORS の許可が無い)ので、決算速報サイトの「更新」ボタンからここ経由で読む。
// 一覧ページ以外の URL は中継しない。ブラウザから読めるのは下の ALLOW のサイトだけ。
const ALLOW = ["https://vix96343-source.github.io", "http://localhost:8765"];

export default {
  async fetch(request) {
    const origin = request.headers.get("Origin") || "";
    const cors = {
      "Access-Control-Allow-Origin": ALLOW.includes(origin) ? origin : ALLOW[0],
      "Access-Control-Allow-Methods": "GET",
      "Vary": "Origin",
    };
    if (request.method === "OPTIONS") return new Response(null, { headers: cors });
    // /list/20261014/001 → TDnet の 2026/10/14 の一覧 1 ページ目
    const m = new URL(request.url).pathname.match(/^\/list\/(\d{8})\/(\d{3})$/);
    if (request.method !== "GET" || !m) return new Response("not found", { status: 404, headers: cors });
    const r = await fetch(`https://www.release.tdnet.info/inbs/I_list_${m[2]}_${m[1]}.html`, {
      headers: { "User-Agent": "kessan-hantei/0.1 (personal research tool)" },
    });
    return new Response(await r.text(), {
      status: r.status,
      headers: { ...cors, "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
    });
  },
};
