"""素材採集：把使用者貼上的網址變成可蒸餾的文字。

為什麼不是「全丟給 Firecrawl 就好」：
  Firecrawl 對某些站台明確拒收（error: "we do not support this site"），
  已知包含 facebook.com / instagram.com / threads.net / linkedin.com / x.com 的**頁面抓取**。
  那是它的政策，不是那些頁面不公開。

因此這裡用三條路互補：
  1. Firecrawl /scrape  — 支援的站台（部落格、Medium、新聞、Wikipedia…），會渲染 JS
  2. Firecrawl /search  — 繞過站台限制：它抓的是「搜尋結果頁」，可以撈到
                          被索引的 FB/IG 公開內容摘要
  3. 直接抓取           — 對 m.facebook.com 之類回傳 metadata 的頁面做最後嘗試

抓不到的時候，就誠實說抓不到，並給出可行的替代做法（本人匯出資料），不假裝成功。
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FIRECRAWL_BASE = "https://api.firecrawl.dev/v1"

# 這些站台的「頁面抓取」被 Firecrawl 政策拒收
BLOCKED_HOSTS = {
    "facebook.com": "Facebook",
    "www.facebook.com": "Facebook",
    "m.facebook.com": "Facebook",
    "instagram.com": "Instagram",
    "www.instagram.com": "Instagram",
    "threads.net": "Threads",
    "www.threads.net": "Threads",
    "linkedin.com": "LinkedIn",
    "www.linkedin.com": "LinkedIn",
}

# 這些站台 Firecrawl 抓得到，而且對蒸餾特別有價值
GOOD_HOSTS = {
    "youtube.com": "YouTube",
    "www.youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "medium.com": "Medium",
    "substack.com": "Substack",
    "wordpress.com": "部落格",
    "blogspot.com": "部落格",
    "github.com": "GitHub",
    "wikipedia.org": "Wikipedia",
    "zh.wikipedia.org": "Wikipedia",
    "x.com": "X/Twitter",
    "twitter.com": "X/Twitter",
}


@dataclass
class SourceDoc:
    """一份採集到的素材。"""

    url: str
    kind: str = "網頁"
    status: str = "pending"   # ok | blocked | empty | error | search_fallback
    title: str = ""
    content: str = ""
    note: str = ""
    chars: int = 0
    fetched_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url, "kind": self.kind, "status": self.status,
            "title": self.title, "content": self.content, "note": self.note,
            "chars": self.chars, "fetched_at": self.fetched_at,
        }

    @property
    def usable(self) -> bool:
        return self.status in ("ok", "search_fallback") and self.chars >= 200


# --------------------------------------------------------------------------
# Firecrawl
# --------------------------------------------------------------------------


class Firecrawl:
    def __init__(self, api_key: str | None = None, timeout: int = 150) -> None:
        self.api_key = api_key or os.environ.get("FIRECRAWL_API_KEY", "").strip()
        load_env_if_needed()
        self.api_key = self.api_key or os.environ.get("FIRECRAWL_API_KEY", "").strip()
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.available:
            raise RuntimeError("沒有 FIRECRAWL_API_KEY")
        req = urllib.request.Request(
            f"{FIRECRAWL_BASE}{endpoint}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if "do not support this site" in body:
                raise BlockedSite("Firecrawl 政策拒收此站") from e
            raise RuntimeError(f"Firecrawl HTTP {e.code}: {body[:300]}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Firecrawl 連線失敗：{e.reason}") from e

    def scrape(self, url: str) -> dict[str, Any]:
        return self._post("/scrape", {
            "url": url,
            "formats": ["markdown"],
            "onlyMainContent": True,
            "waitFor": 1500,
        })

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        d = self._post("/search", {"query": query, "limit": limit})
        return d.get("data") or []


class BlockedSite(RuntimeError):
    pass


_ENV_LOADED = False


def load_env_if_needed() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    try:
        from .persona.provider import load_env_file

        load_env_file()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 網址判斷
# --------------------------------------------------------------------------


def classify(url: str) -> str:
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    return GOOD_HOSTS.get(host) or BLOCKED_HOSTS.get(host) or "網頁"


def is_blocked(url: str) -> bool:
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    return host in BLOCKED_HOSTS


def _clean(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)          # 圖片
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)        # 連結留文字
    text = re.sub(r"^\s*[-*+]\s+", "・", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _looks_like_wall(text: str) -> bool:
    """判斷抓回來的是不是登入牆／JS 空殼，而不是真內容。"""
    t = text.strip()
    if len(t) < 200:
        return True
    walls = ("Log in", "登入", "Sign up", "註冊", "You must log in",
             "enable JavaScript", "請啟用 JavaScript")
    hits = sum(1 for w in walls if w in t)
    return hits >= 2 and len(t) < 800


# 這些是「頁面本身是錯誤頁」，不是內容。抓到了要當失敗，不能餵進蒸餾。
_ERROR_PAGE_MARKERS = (
    "Error 401 (Bad Request)",
    "Error 403 (Forbidden)",
    "Error 404 (Not Found)",
    "Error 500 (Server Error)",
    "That’s an error.",
    "That's an error.",
    "The server cannot process the request",
    "404 Not Found",
    "This page isn’t working",
    "This page isn't working",
)


def _looks_like_error_page(text: str) -> bool:
    head = (text or "")[:1200]
    return any(m in head for m in _ERROR_PAGE_MARKERS)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def fetch_one(url: str, fc: Firecrawl | None = None, *, allow_search_fallback: bool = True) -> SourceDoc:
    """採集單一網址。永遠回傳 SourceDoc，不丟例外（失敗記在 status/note）。"""
    fc = fc or Firecrawl()
    url = url.strip()
    kind = classify(url)
    doc = SourceDoc(url=url, kind=kind, fetched_at=time.strftime("%Y-%m-%dT%H:%M:%S"))

    if not fc.available:
        doc.status = "error"
        doc.note = "沒有 FIRECRAWL_API_KEY，請先寫進 .env"
        return doc

    # 被政策擋掉的站台：直接走搜尋代理，不要浪費一次註定失敗的呼叫
    if is_blocked(url):
        doc.note = f"{BLOCKED_HOSTS.get(re.sub(r'^https?://','',url).split('/')[0].lower(), kind)} 的頁面抓取被 Firecrawl 政策拒收，改用搜尋代理"
        if allow_search_fallback:
            return _search_fallback(url, doc, fc)
        doc.status = "blocked"
        return doc

    try:
        d = fc.scrape(url)
    except BlockedSite:
        doc.note = "Firecrawl 政策拒收此站，改用搜尋代理"
        if allow_search_fallback:
            return _search_fallback(url, doc, fc)
        doc.status = "blocked"
        return doc
    except RuntimeError as e:
        doc.status = "error"
        doc.note = str(e)[:200]
        return doc

    if not d.get("success"):
        err = str(d.get("error", ""))[:200]
        if "do not support" in err:
            if allow_search_fallback:
                return _search_fallback(url, doc, fc)
            doc.status = "blocked"
            doc.note = err
            return doc
        doc.status = "error"
        doc.note = err or "Firecrawl 回報失敗"
        return doc

    data = d.get("data") or {}
    md = _clean(data.get("markdown") or "")
    meta = data.get("metadata") or {}
    doc.title = (meta.get("title") or "")[:120]

    if _looks_like_error_page(md):
        doc.status = "error"
        doc.content = md
        doc.chars = len(md)
        doc.note = ("抓回來的是網站錯誤頁（Google/YouTube 對自動化請求常見 401/404），"
                    "不是內容，已排除。")
        return doc

    if _looks_like_wall(md):
        doc.status = "empty"
        doc.content = md
        doc.chars = len(md)
        doc.note = ("抓到的是登入牆或 JS 空殼，不是內容。"
                    "若這是你的帳號，建議改用「本人匯出資料」後貼上。")
        return doc

    doc.status = "ok"
    doc.content = md
    doc.chars = len(md)
    return doc


def _search_fallback(url: str, doc: SourceDoc, fc: Firecrawl) -> SourceDoc:
    """用 Firecrawl search 撈這個網址被索引到的公開內容。"""
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    handle = url.rstrip("/").split("/")[-1]
    queries = [f"site:{host} {handle}", f"{handle} {host}"]
    collected: list[str] = []
    titles: list[str] = []

    for q in queries:
        try:
            results = fc.search(q, limit=6)
        except Exception as e:  # noqa: BLE001
            doc.status = "error"
            doc.note = f"搜尋代理失敗：{e}"[:200]
            return doc
        for r in results:
            t = (r.get("title") or "").strip()
            desc = (r.get("description") or "").strip()
            u = r.get("url") or ""
            if not (t or desc):
                continue
            if host not in u and handle not in (t + desc):
                continue
            block = f"### {t}\n{u}\n{desc}"
            if block not in collected:
                collected.append(block)
                titles.append(t)
        if len(collected) >= 6:
            break

    if not collected:
        doc.status = "blocked"
        doc.note = ("Firecrawl 拒收此站，搜尋代理也找不到公開內容。"
                    "若這是你的帳號，請用「本人匯出資料」："
                    "Facebook 設定 → 你的 Facebook 資訊 → 下載你的資訊 → JSON。")
        return doc

    doc.status = "search_fallback"
    doc.title = titles[0][:120] if titles else doc.title
    doc.content = _clean("\n\n".join(collected))
    doc.chars = len(doc.content)
    doc.note = (f"頁面抓取被政策拒收，改用搜尋代理撈到 {len(collected)} 筆公開索引內容。"
                "品質低於原文，僅能作為輔助素材。")
    return doc


def fetch_many(urls: list[str], fc: Firecrawl | None = None) -> list[SourceDoc]:
    fc = fc or Firecrawl()
    out: list[SourceDoc] = []
    for u in urls:
        u = u.strip()
        if not u:
            continue
        if not u.startswith(("http://", "https://")):
            u = "https://" + u
        out.append(fetch_one(u, fc))
    return out


# --------------------------------------------------------------------------
# 組成蒸餾素材
# --------------------------------------------------------------------------

KIND_WEIGHT = {
    "部落格": 5, "Medium": 5, "Substack": 5, "Wikipedia": 4,
    "YouTube": 4, "GitHub": 4, "X/Twitter": 3, "網頁": 3,
    "Facebook": 1, "Instagram": 1, "Threads": 1, "LinkedIn": 1,
}


def rank_docs(docs: list[SourceDoc]) -> list[SourceDoc]:
    """可用素材的固定排序。prompt 的 [S1] 與報告的來源清單共用這一份順序。"""
    usable = [d for d in docs if d.usable]
    usable.sort(key=lambda d: (-KIND_WEIGHT.get(d.kind, 2), -d.chars))
    return usable


def render_docs_for_prompt(docs: list[SourceDoc], per_doc_limit: int = 6000) -> str:
    """把素材組成餵給 LLM 的文字。每份素材標上代號，讓 LLM 可以引用來源。"""
    blocks: list[str] = []
    for i, d in enumerate(rank_docs(docs), 1):
        tag = f"S{i}"
        body = d.content[:per_doc_limit]
        if len(d.content) > per_doc_limit:
            body += f"\n\n（以下略，原文共 {d.chars} 字）"
        blocks.append(
            f"### [{tag}] 來源：{d.kind} — {d.title or d.url}\n"
            f"網址：{d.url}\n"
            f"狀態：{d.status}{'（' + d.note + '）' if d.note else ''}\n"
            f"內容：\n\"\"\"\n{body}\n\"\"\""
        )
    return "\n\n".join(blocks)


def coverage_of(docs: list[SourceDoc]) -> dict[str, Any]:
    ranked = rank_docs(docs)
    by_status: dict[str, int] = {}
    for d in docs:
        by_status[d.status] = by_status.get(d.status, 0) + 1
    by_kind: dict[str, int] = {}
    for d in ranked:
        by_kind[d.kind] = by_kind.get(d.kind, 0) + 1
    return {
        "urls": len(docs),
        "usable": len(ranked),
        "total_chars": sum(d.chars for d in ranked),
        "by_status": by_status,
        "by_kind": by_kind,
        "sources": [
            {"tag": f"S{i}", "kind": d.kind, "title": d.title, "url": d.url,
             "chars": d.chars, "status": d.status, "note": d.note}
            for i, d in enumerate(ranked, 1)
        ],
        "excluded": [
            {"kind": d.kind, "url": d.url, "status": d.status, "note": d.note}
            for d in docs if not d.usable
        ],
    }


# --------------------------------------------------------------------------
# 本地檔案：文字、email 匯出、社群資料匯出
# --------------------------------------------------------------------------


def parse_file(filename: str, data: bytes) -> SourceDoc:
    """把上傳的檔案變成 SourceDoc。不支援的格式誠實回報，不假裝成功。"""
    name = Path(filename).name
    ext = Path(name).suffix.lower()
    doc = SourceDoc(url=f"file://{name}", kind="檔案", fetched_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    doc.title = name

    try:
        raw = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            raw = data.decode("big5")       # 台灣常見
        except UnicodeDecodeError:
            try:
                raw = data.decode("latin-1")
            except UnicodeDecodeError:
                doc.status = "error"
                doc.note = "無法解碼這個檔案（不是文字檔？）"
                return doc

    if ext in (".eml",):
        doc.kind = "Email"
        doc.content = _clean(_parse_eml(raw))
    elif ext in (".mbox", ".mbx"):
        doc.kind = "Email"
        doc.content = _clean(_parse_mbox(raw))
    elif ext in (".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".srt", ".vtt"):
        doc.kind = {".csv": "CSV", ".tsv": "CSV", ".srt": "字幕", ".vtt": "字幕"}.get(ext, "文字檔")
        doc.content = _clean(raw)
    elif ext == ".json":
        doc.kind = "JSON"
        doc.content = _clean(_parse_json_export(raw))
    elif ext == ".html" or ext == ".htm":
        doc.kind = "網頁存檔"
        body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
        doc.content = _clean(re.sub(r"<[^>]+>", " ", body))
    elif ext == ".pdf":
        doc.status = "error"
        doc.note = ("PDF 需要額外工具解析，目前不支援。"
                    "請把文字複製出來貼上，或另存成 .txt。")
        return doc
    else:
        # 未知副檔名：當純文字試試看，太短就回報失敗
        doc.kind = "未知格式"
        doc.content = _clean(raw)
        if doc.chars < 100:
            doc.status = "error"
            doc.chars = len(doc.content)
            doc.note = f"不支援的格式（{ext or '無副檔名'}），且內容過短。"
            return doc

    doc.chars = len(doc.content)
    if doc.chars < 100:
        doc.status = "empty"
        doc.note = "檔案內容過短，無法作為素材。"
    else:
        doc.status = "ok"
        doc.note = f"本地檔案（{ext or '純文字'}），{doc.chars} 字。"
    return doc


def _parse_eml(raw: str) -> str:
    import email
    from email import policy

    msg = email.message_from_string(raw, policy=policy.default)
    parts = [
        f"主旨：{msg.get('subject', '')}",
        f"寄件者：{msg.get('from', '')}",
        f"收件者：{msg.get('to', '')}",
        f"日期：{msg.get('date', '')}",
    ]
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    body += part.get_content() + "\n"
                except Exception:  # noqa: BLE001
                    continue
    else:
        try:
            body = msg.get_content()
        except Exception:  # noqa: BLE001
            body = str(msg.get_payload())
    return "\n".join(parts) + "\n\n" + (body or "")


def _parse_mbox(raw: str) -> str:
    import mailbox
    import tempfile

    out: list[str] = []
    tmp = ""
    try:
        with tempfile.NamedTemporaryFile("w+", suffix=".mbox", delete=False,
                                         encoding="utf-8") as tf:
            tf.write(raw)
            tmp = tf.name
        m = mailbox.mbox(tmp)
        for i, msg in enumerate(m):
            if i >= 300:
                out.append(f"（只取前 300 封，全部共 {len(m)} 封）")
                break
            subj = msg.get("subject", "")
            frm = msg.get("from", "")
            try:
                body = msg.get_payload(decode=True)
                body = body.decode("utf-8", "replace") if body else ""
            except Exception:  # noqa: BLE001
                body = ""
            out.append(f"### {subj}\n寄件者：{frm}\n{body[:2000]}")
        m.close()
    except Exception as e:  # noqa: BLE001
        return f"（mbox 解析失敗：{e}）\n\n" + raw[:5000]
    finally:
        if tmp:
            try:
                Path(tmp).unlink(missing_ok=True)
            except OSError:
                pass
    return "\n\n".join(out)


def _parse_json_export(raw: str) -> str:
    """試著從 JSON 匯出檔裡撈出文字。認不得就原文保留（讓 LLM 自己看）。"""
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return raw

    chunks: list[str] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 12 or len("\n".join(chunks)) > 400_000:
            return
        if isinstance(node, dict):
            for k in ("data", "messages", "posts", "status", "content", "text",
                      "title", "caption", "body", "comment"):
                if k in node and isinstance(node[k], (list, dict)):
                    walk(node[k], depth + 1)
            for k, v in node.items():
                if isinstance(v, str) and len(v) > 20:
                    chunks.append(v)
                elif isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node[:5000]:
                walk(v, depth + 1)
        elif isinstance(node, str) and len(node) > 20:
            chunks.append(node)

    walk(d)
    seen: set[str] = set()
    uniq = [c for c in chunks if not (c in seen or seen.add(c))]
    return "\n\n".join(uniq) if uniq else raw


def suggest_urls(handle: str, fc: Firecrawl | None = None) -> list[dict[str, str]]:
    """給一個代號，用搜尋找出他可能存在的公開頁面，讓使用者勾選。"""
    fc = fc or Firecrawl()
    if not fc.available:
        return []
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for q in (f"{handle}", f"{handle} blog youtube"):
        try:
            for r in fc.search(q, limit=8):
                u = r.get("url") or ""
                host = re.sub(r"^https?://", "", u).split("/")[0].lower()
                if not u or host in seen:
                    continue
                seen.add(host)
                out.append({
                    "url": u,
                    "title": (r.get("title") or "")[:80],
                    "kind": classify(u),
                    "blocked": "yes" if is_blocked(u) else "",
                })
        except Exception:  # noqa: BLE001
            continue
    return out[:12]
