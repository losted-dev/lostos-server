"""Web search for the assistant without any paid API: DuckDuckGo's HTML endpoint for the results, then the text of the
first pages, so that the language model can answer from facts (Gemini's own Google Search tool needs a paid plan)."""
import html
import re
import urllib.parse

import aiohttp

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
BAD_HOSTS = ("youtube.com", "youtu.be", "facebook.com", "instagram.com", "tiktok.com", "vk.com/video")


def _clean(t: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>|<nav.*?</nav>|<header.*?</header>|<footer.*?</footer>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def _real_url(href: str) -> str:
    """Bing wraps result links: .../ck/a?...&u=a1<base64 of the real address>"""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(html.unescape(href)).query)
    u = q.get("u", [""])[0]
    if u.startswith("a1"):
        import base64
        try:
            return base64.urlsafe_b64decode(u[2:] + "=" * (-len(u[2:]) % 4)).decode("utf-8", "ignore")
        except Exception:
            pass
    return html.unescape(href)


async def _results(s: aiohttp.ClientSession, query: str):
    async with s.get("https://www.bing.com/search", params={"q": query, "setlang": "ru", "cc": "RU"},
                     headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"}) as r:
        page = await r.text()
    out = []
    for blk in re.findall(r'<li class="b_algo".*?</li>', page, flags=re.S):
        m = re.search(r'<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', blk, flags=re.S)
        if not m:
            continue
        sn = re.search(r'<p[^>]*>(.*?)</p>', blk, flags=re.S)
        out.append((_real_url(m.group(1)), _clean(m.group(2)), _clean(sn.group(1)) if sn else ""))
    return out


async def _page_text(s: aiohttp.ClientSession, url: str, limit: int) -> str:
    try:
        async with s.get(url, headers={"User-Agent": UA}, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if "text/html" not in r.headers.get("Content-Type", ""):
                return ""
            body = (await r.content.read(600_000)).decode(r.charset or "utf-8", errors="ignore")
    except Exception:
        return ""
    return _clean(body)[:limit]


async def _tavily(query: str) -> str:
    """Tavily (free plan: 1000 searches a month, https://tavily.com): an answer plus the best snippets, made for models."""
    import os
    key = os.environ.get("TAVILY_API_KEY") or ""
    if not key:
        try:
            for line in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), encoding="utf-8"):
                if line.startswith("TAVILY_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    if not key:
        return ""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.post("https://api.tavily.com/search", headers={"Authorization": "Bearer " + key},
                          json={"query": query, "search_depth": "advanced", "max_results": 5, "include_answer": True}) as r:
            if r.status != 200:
                raise RuntimeError(f"Tavily {r.status}: {(await r.text())[:120]}")
            d = await r.json()
    out = []
    if d.get("answer"):
        out.append("Краткий ответ поиска: " + d["answer"])
    for i, x in enumerate(d.get("results", [])[:5]):
        host = urllib.parse.urlparse(x.get("url") or "").netloc
        out.append(f"{i + 1}. {x.get('title') or ''} ({host}): {(x.get('content') or '')[:600]}")
    return "Результаты поиска:\n" + "\n".join(out) if out else ""


async def search(query: str) -> str:
    try:
        r = await _tavily(query)
        if r:
            return r
    except Exception as e:
        print("tavily:", e, flush=True)
    return await _scrape(query)


async def _scrape(query: str) -> str:
    """Titles + snippets of the best results and the text of the first two pages."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        res = [r for r in await _results(s, query) if not any(h in r[0] for h in BAD_HOSTS)][:5]
        if not res:
            return "ничего не нашла по этому запросу"
        parts = [f"{i + 1}. {t} - {sn} ({urllib.parse.urlparse(u).netloc})" for i, (u, t, sn) in enumerate(res)]
        texts = []
        for u, t, _ in res[:2]:
            tx = await _page_text(s, u, 1800)
            if tx:
                texts.append(f"Текст страницы «{t}»: {tx}")
    return "Результаты поиска:\n" + "\n".join(parts) + ("\n\n" + "\n\n".join(texts) if texts else "")