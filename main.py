import asyncio
import logging
import os
import re
import random
import tempfile
import traceback
from typing import List, Optional

from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
import httpx
import shutil
import subprocess

# Try to use tls_client if available (preferred for Cloudflare bypass).
try:
    import tls_client  # type: ignore
    _HAS_TLS_CLIENT = True
except Exception:
    tls_client = None  # type: ignore
    _HAS_TLS_CLIENT = False

log = logging.getLogger("animepahe_api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Config
BASE = "https://animepahe.pw"
NODE_BIN = os.environ.get("NODE_BIN", "node")  # allow override
CHECK_NODE = shutil.which(NODE_BIN) is not None

# Utility
def random_user_agent() -> str:
    agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/16.1 Safari/605.1.15",
        "Mozilla/5.0 (Linux; Android 12; SM-G998B) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    ]
    return random.choice(agents)


class AnimePahe:
    def __init__(self):
        self.base = BASE
        self.headers = {
            "User-Agent": random_user_agent(),
            "Referer": f"{self.base}/",
        }
        self.tls_session = None
        self.httpx_client: Optional[httpx.AsyncClient] = None
        if _HAS_TLS_CLIENT:
            try:
                self.tls_session = tls_client.Session(client_identifier="chrome_120")
                log.info("Using tls_client for requests")
            except Exception:
                self.tls_session = None
        if not self.tls_session:
            # fallback to httpx
            self.httpx_client = httpx.AsyncClient(timeout=30.0, headers=self.headers)
            log.info("Using httpx AsyncClient for requests")

    async def _get_text(self, url: str) -> str:
        """Fetch and return response text (async)."""
        if self.tls_session:
            def _req():
                r = self.tls_session.get(url, headers=self.headers)
                # tls_client returns object with .text and .status_code
                return r.text
            return await asyncio.to_thread(_req)
        else:
            assert self.httpx_client is not None
            r = await self.httpx_client.get(url)
            r.raise_for_status()
            return r.text

    async def _get_json(self, url: str):
        if self.tls_session:
            def _req():
                r = self.tls_session.get(url, headers=self.headers)
                return r.json()
            return await asyncio.to_thread(_req)
        else:
            assert self.httpx_client is not None
            r = await self.httpx_client.get(url)
            r.raise_for_status()
            return r.json()

    async def search(self, query: str):
        """Search anime by title, return list of hits with 'session' field where possible."""
        url = f"{self.base}/api?m=search&q={query}"
        try:
            data = await self._get_json(url)
            results = []
            for a in data.get("data", []):
                results.append({
                    "id": a.get("id"),
                    "title": a.get("title"),
                    "url": f"{self.base}/anime/{a.get('session')}" if a.get("session") else None,
                    "year": a.get("year"),
                    "poster": a.get("poster"),
                    "type": a.get("type"),
                    "session": a.get("session"),
                })
            return results
        except Exception as e:
            log.error("Search failed: %s", e)
            raise

    async def get_episodes(self, anime_session: str):
        """
        Given an `anime_session` (like 'abcdef123'), parse the anime page to discover
        the internal numeric id, then call the release API to list episodes.
        """
        try:
            page = await self._get_text(f"{self.base}/anime/{anime_session}")
            soup = BeautifulSoup(page, "html.parser")
            meta = soup.find("meta", {"property": "og:url"})
            if not meta or not meta.get("content"):
                raise Exception("Could not find session ID in meta tag (og:url)")
            temp_id = meta["content"].rstrip("/").split("/")[-1]
            # Get first page to find last_page
            first_page = await self._get_json(f"{self.base}/api?m=release&id={temp_id}&sort=episode_asc&page=1")
            episodes = first_page.get("data", []) or []
            last_page = int(first_page.get("last_page") or 1)
            if last_page > 1:
                async def fetch_page(p):
                    j = await self._get_json(f"{self.base}/api?m=release&id={temp_id}&sort=episode_asc&page={p}")
                    return j.get("data", []) or []
                tasks = [fetch_page(p) for p in range(2, last_page + 1)]
                pages = await asyncio.gather(*tasks, return_exceptions=True)
                for pg in pages:
                    if isinstance(pg, Exception):
                        log.warning("Episode page fetch failed: %s", pg)
                        continue
                    episodes.extend(pg)
            # map episodes
            out = []
            for e in episodes:
                out.append({
                    "id": e.get("id"),
                    "number": e.get("episode"),
                    "title": e.get("title") or f"Episode {e.get('episode')}",
                    "snapshot": e.get("snapshot"),
                    "session": e.get("session"),
                })
            return out
        except Exception:
            log.exception("get_episodes error")
            raise

    async def get_sources(self, anime_session: str, episode_session: str):
        """
        Parse the play page and extract kwik links or direct kwik api data-src buttons.
        Returns list of sources with url, quality, fansub, audio.
        """
        try:
            html = await self._get_text(f"{self.base}/play/{anime_session}/{episode_session}")
            # primary pattern: buttons with data-src attributes
            # capture data-src, data-fansub, data-resolution, data-audio
            button_pattern = re.compile(
                r'<button[^>]+data-src=\"([^\"]+)\"[^>]*data-fansub=\"([^\"]*)\"[^>]*data-resolution=\"([^\"]*)\"[^>]*data-audio=\"([^\"]*)\"[^>]*>',
                re.IGNORECASE,
            )
            matches = button_pattern.findall(html)
            sources = []
            for src, fansub, resolution, audio in matches:
                src = src.strip()
                if not src:
                    continue
                quality = f"{resolution}p" if resolution and resolution.isdigit() else (resolution or None)
                sources.append({"url": src, "quality": quality, "fansub": fansub or None, "audio": audio or None})

            if not sources:
                # fallback: find kwik links anywhere in the HTML
                kwik_links = re.findall(r"https?://kwik\.(?:si|cx|link)/e/[a-zA-Z0-9_-]+", html)
                kwik_links = list(dict.fromkeys(kwik_links))  # unique preserve order
                for link in kwik_links:
                    sources.append({"url": link, "quality": None, "fansub": None, "audio": None})

            # dedupe by url and sort by quality desc where possible
            unique = {s["url"]: s for s in sources}.values()
            def sort_key(s):
                q = s.get("quality")
                if not q:
                    return 0
                m = re.search(r"(\d+)", str(q))
                return int(m.group(1)) if m else 0
            sorted_sources = sorted(unique, key=sort_key, reverse=True)
            if not sorted_sources:
                raise Exception("No kwik links found on play page")
            return sorted_sources
        except Exception:
            log.exception("get_sources error")
            raise

    async def resolve_kwik_with_node(self, kwik_url: str, node_bin: str = NODE_BIN) -> str:
        """
        Attempt to resolve a kwik link to a .m3u8.
        1) Try to find direct .m3u8 in the HTML.
        2) If not found, try to execute extracted obfuscated script in Node to reveal captured console logs.
        Node must be available; otherwise raise.
        """
        try:
            html = await self._get_text(kwik_url)
            # quick direct m3u8 search
            m3 = re.search(r"https?://[^\s'\"<>]+\.m3u8[^\s'\"<>)]*", html)
            if m3:
                return m3.group(0)

            # Try extracting <script> blocks containing eval
            scripts = re.findall(r"(<script[^>]*>[\s\S]*?</script>)", html, re.IGNORECASE)
            candidate = None
            longest = ""
            for s in scripts:
                if "eval(" in s:
                    # prefer scripts that mention m3u8 or source keywords
                    if "m3u8" in s or "source" in s or "Plyr" in s:
                        candidate = s
                        break
                    if len(s) > len(longest):
                        longest = s
                        candidate = longest
            if not candidate:
                raise Exception("No candidate <script> block found to eval")

            inner_js = re.sub(r"^<script[^>]*>", "", candidate, flags=re.IGNORECASE)
            inner_js = re.sub(r"</script>$", "", inner_js, flags=re.IGNORECASE).strip()

            if not CHECK_NODE:
                raise Exception("Node.js not found on PATH; cannot perform JS eval to resolve .m3u8")

            wrapper = r"""
globalThis.window = { location: {} };
globalThis.document = { cookie: '' };
globalThis.navigator = { userAgent: 'mozilla' };
const __captured = [];
const origLog = console.log;
console.log = (...args) => { __captured.push(args.join(' ')); origLog(...args); };
(function(){
  const origEval = eval;
  eval = (x) => { __captured.push('[EVAL]' + x); return origEval(x); };
})();
"""
            final_js = wrapper + "\n" + inner_js + "\n" + (
                "setTimeout(()=>{for(const c of __captured){console.log('__CAPTURED__START__');"
                "console.log(c);console.log('__CAPTURED__END__');}process.exit(0)},300);"
            )

            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tf:
                tmp_path = tf.name
                tf.write(final_js)
                tf.flush()

            try:
                proc = await asyncio.create_subprocess_exec(
                    node_bin, tmp_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                out = (stdout.decode(errors="ignore") + "\n" + stderr.decode(errors="ignore"))
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

            m = re.search(r"https?://[^\s'\"<>]+\.m3u8[^\s'\"<>)]*", out)
            if m:
                return m.group(0)

            # As a last effort scan captured logs blocks
            m = re.search(r"__CAPTURED__START__\n([\s\S]{0,4000}?)\n__CAPTURED__END__", out)
            if m:
                inner = m.group(1)
                mm = re.search(r"https?://[^\s'\"<>]+\.m3u8[^\s'\"<>)]*", inner)
                if mm:
                    return mm.group(0)

            raise Exception(f"Could not resolve .m3u8. Node output:\n{out[:2000]}")
        except Exception:
            log.exception("resolve_kwik_with_node error")
            raise


# FastAPI app
app = FastAPI(title="AnimePahe API (improved)")

pahe = AnimePahe()

@app.on_event("shutdown")
async def shutdown_event():
    if pahe.httpx_client:
        await pahe.httpx_client.aclose()

@app.get("/search")
async def api_search(q: str):
    try:
        return await pahe.search(q)
    except Exception as e:
        log.error("API /search error: %s", e)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/episodes")
async def api_episodes(session: str):
    try:
        return await pahe.get_episodes(session)
    except Exception as e:
        log.error("API /episodes error: %s", e)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/sources")
async def api_sources(anime_session: str, episode_session: str):
    try:
        return await pahe.get_sources(anime_session, episode_session)
    except Exception as e:
        log.error("API /sources error: %s", e)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/m3u8")
async def api_resolve_kwik(url: str):
    try:
        m3u8 = await pahe.resolve_kwik_with_node(url)
        return {"m3u8": m3u8}
    except Exception as e:
        log.error("API /m3u8 error: %s", e)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
