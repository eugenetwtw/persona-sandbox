"""最小網頁伺服器：零依賴（http.server）。把問卷 → 蒸餾 → 沙盒包成一個網頁介面。

    python3 -m persona_sandbox.server
    # 打開 http://127.0.0.1:8800
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .persona.engine import PersonaCard, apply_correction, distill
from .persona.provider import (
    LLM,
    LLMError,
    available_providers,
    detect_provider,
    load_env_file,
)
from .persona.questions import QUESTIONS, Answer, ResponseSheet
from .store import Store
from .world.scenario import SCENARIO_BY_ID, SCENARIOS
from .world.sandbox import Encounter
from .world.witnesses import WITNESSES

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
OUT = ROOT / "out"

# 進行中的對話（encounter）與已抓取的素材留在記憶體；
# 使用者、對話紀錄、蒸餾結果都存在 SQLite。
ENCOUNTERS: dict[str, Encounter] = {}
SOURCE_POOL: dict[str, list[Any]] = {}
_lock = threading.Lock()
LLM_OPTS: dict[str, Any] = {"provider": None, "model": None, "dry_run": False}
STORE: Store = Store()  # 在 main() 重新指定實際路徑


def _llm() -> LLM:
    return LLM(
        provider=LLM_OPTS["provider"],
        model=LLM_OPTS["model"],
        dry_run=LLM_OPTS["dry_run"],
    )


def _new_session(user_name: str) -> str:
    """建立一個問卷 session，並確保 user 存在。"""
    from .persona.engine import slugify

    slug = slugify(user_name)
    user_id = STORE.get_or_create_user(user_name, slug)
    sid = secrets.token_urlsafe(12)
    STORE.start_session(sid, user_id, "questionnaire", label=user_name,
                        meta={"provider": LLM_OPTS["provider"], "model": LLM_OPTS["model"]})
    STORE.add_event(sid, "system", "app", {"msg": "session 開始", "user": user_name})
    return sid


def _session_row(sid: str) -> dict[str, Any]:
    s = STORE.session_by_sid(sid)
    if s is None:
        raise KeyError("session 不存在，請重新開始")
    with STORE.connect() as c:
        u = c.execute("SELECT display_name, note FROM users WHERE id = ?",
                      (s["user_id"],)).fetchone()
    if u:
        s["display_name"] = u["display_name"]
        s["note"] = u["note"]
    return s


# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "PersonaSandbox/0.1"

    # -- 基礎 -------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # 安靜一點
        if self.server.verbose:  # type: ignore[attr-defined]
            sys.stderr.write(f"[{self.address_string()}] {fmt % args}\n")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data: Any, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, msg: str, code: int = 400) -> None:
        self._json({"error": msg}, code)

    def _read_json(self) -> dict[str, Any]:
        n = int(self.headers.get("content-length") or 0)
        if not n:
            return {}
        ctype = (self.headers.get("content-type") or "").lower()
        # multipart 由各自的 handler 自行讀取，這裡不要碰 body
        if ctype.startswith("multipart/form-data"):
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    # -- 路由 -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path == "/api/providers":
                return self._json(
                    {
                        "providers": available_providers(),
                        "active": (
                            detect_provider(LLM_OPTS["provider"]).name
                            if detect_provider(LLM_OPTS["provider"])
                            else None
                        ),
                        "model": LLM_OPTS["model"],
                        "dry_run": LLM_OPTS["dry_run"]
                        or detect_provider(LLM_OPTS["provider"]) is None,
                    }
                )
            if path == "/api/questions":
                return self._json({"questions": [_q(q) for q in QUESTIONS]})
            if path == "/api/scenarios":
                return self._json(
                    {
                        "scenarios": [
                            {
                                "id": s.id,
                                "name": s.name,
                                "stakes": s.stakes,
                                "max_rounds": s.max_rounds,
                                "witnesses": [
                                    {
                                        "id": w.id,
                                        "name": w.name,
                                        "era": w.era,
                                        "archetype": w.archetype,
                                        "default": w.id in s.hidden_agendas,
                                    }
                                    for w in WITNESSES
                                ],
                            }
                            for s in SCENARIOS
                        ]
                    }
                )
            if path.startswith("/api/session/"):
                sid = path.rsplit("/", 1)[-1]
                row = _session_row(sid)
                dist = STORE.latest_distillation(row["user_id"])
                return self._json(
                    {
                        "session_id": sid,
                        "user": row["display_name"] if "display_name" in row else None,
                        "kind": row["kind"],
                        "started_at": row["started_at"],
                        "ended_at": row["ended_at"],
                        "card_markdown": dist["card_markdown"] if dist else None,
                        "card_version": dist["version"] if dist else None,
                        "coverage": dist["coverage"] if dist else None,
                        "evidence": dist["evidence"] if dist else None,
                        "model": f"{dist['provider']}:{dist['model']}" if dist else None,
                        "answers": STORE.answers_for_session(sid),
                        "events": STORE.events(sid),
                        "turns": STORE.turns(sid),
                    }
                )
            if path == "/api/users":
                return self._json({"users": STORE.list_users()})
            if path == "/api/stats":
                return self._json(STORE.stats())
            if path.startswith("/static/"):
                return self._static(path[len("/static/") :])
            return self._error("not found", 404)
        except KeyError as e:
            return self._error(str(e), 404)
        except LLMError as e:
            return self._error(str(e), 502)
        except Exception as e:  # noqa: BLE001
            return self._error(f"{type(e).__name__}: {e}", 500)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        try:
            body = self._read_json()
            if path == "/api/session":
                sid = _new_session((body.get("name") or "").strip() or "匿名玩家")
                return self._json({"session_id": sid})
            if path == "/api/answer":
                _session_row(body["session_id"])
                sid = body["session_id"]
                qid = body["qid"]
                text = (body.get("text") or "").strip()
                skipped = bool(body.get("skipped")) or not text
                STORE.add_event(
                    sid,
                    "skip" if skipped else "answer",
                    _session_row(sid).get("display_name", "user"),
                    {
                        "qid": qid,
                        "text": text,
                        "follow_ups": [body["follow_up"]] if body.get("follow_up") else [],
                        "skipped": skipped,
                    },
                )
                return self._json({"ok": True})
            if path == "/api/distill":
                row = _session_row(body["session_id"])
                sid = body["session_id"]
                # 覆蓋式寫入，避免重複提交累積
                sheet = ResponseSheet()
                for a in body.get("answers", []):
                    sheet.add(
                        Answer(
                            qid=a["qid"],
                            text=(a.get("text") or "").strip(),
                            follow_ups=a.get("follow_ups") or [],
                            skipped=a.get("skipped") or not (a.get("text") or "").strip(),
                        )
                    )
                name = (body.get("name") or row.get("display_name") or "匿名玩家").strip()
                llm = _llm()
                card = distill(llm, sheet, name)
                path_saved = card.save(OUT / "cards")
                did = STORE.add_distillation(
                    row["user_id"],
                    card.markdown,
                    sid=sid,
                    provider=(llm.spec.name if llm.spec else "dry-run"),
                    model=llm.model,
                    params={
                        "temperature_analysis": 0.3,
                        "temperature_build": 0.7,
                        "prompts": "persona/prompts.py",
                        "dry_run": llm.dry_run,
                    },
                    coverage=card.coverage,
                    evidence=card.evidence_summary(),
                    analysis=card.analysis,
                )
                STORE.add_event(
                    sid, "system", "app",
                    {"msg": "蒸餾完成", "distillation_id": did, "version": None,
                     "model": card.model, "coverage": card.coverage},
                )
                return self._json(
                    {
                        "card_markdown": card.markdown,
                        "coverage": card.coverage,
                        "evidence": card.evidence_summary(),
                        "model": card.model,
                        "dry_run": llm.dry_run,
                        "saved": str(path_saved),
                        "distillation_id": did,
                    }
                )
            if path == "/api/fetch-sources":
                row = _session_row(body["session_id"])
                urls = [u for u in (body.get("urls") or []) if str(u).strip()]
                if not urls:
                    return self._error("請至少貼一個網址")
                from .sources import Firecrawl, coverage_of, fetch_many

                fc = Firecrawl()
                if not fc.available:
                    return self._error("沒有 FIRECRAWL_API_KEY，請先寫進 .env 後重啟")
                docs = fetch_many([str(u) for u in urls], fc)
                # 併入既有素材池（同一 session 可分批貼）
                pool = SOURCE_POOL.setdefault(body["session_id"], [])
                have = {d.url for d in pool}
                pool.extend(d for d in docs if d.url not in have)
                return self._json(
                    {
                        "docs": [d.to_dict() for d in docs],
                        "coverage": coverage_of(pool),
                        "pool_size": len(pool),
                    }
                )
            if path == "/api/upload":
                self._handle_upload(body)
                return
            if path == "/api/distill-material":
                row = _session_row(body["session_id"])
                sid = body["session_id"]
                from .persona.engine import distill_combined, distill_from_material
                from .sources import Firecrawl, coverage_of, fetch_many, rank_docs

                # 素材來源：前端指定，或沿用這個 session 已抓好的素材池
                docs = SOURCE_POOL.get(sid, [])
                urls = [str(u) for u in (body.get("urls") or []) if str(u).strip()]
                if urls:
                    fc = Firecrawl()
                    if not fc.available:
                        return self._error("沒有 FIRECRAWL_API_KEY，請先寫進 .env 後重啟")
                    fresh = fetch_many(urls, fc)
                    have = {d.url for d in docs}
                    docs = docs + [d for d in fresh if d.url not in have]
                    SOURCE_POOL[sid] = docs

                usable = rank_docs(docs)
                if not usable:
                    return self._error(
                        "沒有抓到任何可用素材。Facebook / Instagram 的頁面抓取被 Firecrawl "
                        "政策拒收，請改貼部落格、YouTube、X、Medium，或上傳本機檔案。"
                    )

                # 訪談模式：合併問卷自述與公開素材
                sheet = ResponseSheet()
                for a in body.get("answers", []):
                    sheet.add(Answer(
                        qid=a["qid"],
                        text=(a.get("text") or "").strip(),
                        follow_ups=a.get("follow_ups") or [],
                        skipped=a.get("skipped") or not (a.get("text") or "").strip(),
                    ))
                name = (body.get("name") or row.get("display_name") or "受測者").strip()
                llm = _llm()
                cov = coverage_of(docs)

                if sheet.non_empty():
                    card = distill_combined(llm, name, sheet, docs, cov)
                    route = "material+interview"
                    for a in sheet.answers:
                        STORE.add_event(sid, "skip" if a.skipped else "answer", name,
                                        {"qid": a.qid, "text": a.text,
                                         "follow_ups": a.follow_ups, "skipped": a.skipped})
                else:
                    card = distill_from_material(llm, name, docs, cov)
                    route = "material"

                path_saved = card.save(OUT / "cards")
                did = STORE.add_distillation(
                    row["user_id"], card.markdown, sid=sid,
                    provider=(llm.spec.name if llm.spec else "dry-run"),
                    model=llm.model,
                    params={
                        "route": route,
                        "urls": [d.url for d in usable],
                        "sources": cov.get("sources", []),
                        "excluded": cov.get("excluded", []),
                        "prompts": "persona/prompts.py:MATERIAL_*",
                        "dry_run": llm.dry_run,
                    },
                    coverage=cov,
                    evidence=card.evidence_summary(),
                    analysis=card.analysis,
                )
                STORE.add_event(sid, "system", "app",
                                {"msg": "蒸餾完成", "route": route,
                                 "distillation_id": did, "usable": cov["usable"]})
                return self._json(
                    {
                        "card_markdown": card.markdown,
                        "coverage": cov,
                        "evidence": card.evidence_summary(),
                        "model": card.model,
                        "dry_run": llm.dry_run,
                        "saved": str(path_saved),
                        "distillation_id": did,
                        "route": route,
                    }
                )
            if path == "/api/correct":
                row = _session_row(body["session_id"])
                sid = body["session_id"]
                dist = STORE.latest_distillation(row["user_id"])
                if dist is None:
                    return self._error("還沒有人格卡可以修正")
                feedback = (body.get("feedback") or "").strip()
                if not feedback:
                    return self._error("請輸入修正內容")
                # 從 DB 還原原始問卷素材，修正才有依據
                saved = STORE.answers_for_session(sid)
                sheet = ResponseSheet()
                for a in body.get("answers", []):
                    sheet.add(Answer(qid=a["qid"], text=(a.get("text") or "").strip(),
                                     follow_ups=a.get("follow_ups") or [],
                                     skipped=a.get("skipped") or not (a.get("text") or "").strip()))
                if not sheet.non_empty():
                    for qid, a in saved.items():
                        sheet.add(Answer(qid=qid, text=a.get("text", ""),
                                         follow_ups=a.get("follow_ups") or [],
                                         skipped=a.get("skipped", False)))

                card = PersonaCard(
                    name=row.get("display_name", "玩家"),
                    slug="",
                    markdown=dist["card_markdown"],
                    analysis=dist["analysis"],
                    coverage=dist["coverage"],
                )
                STORE.add_event(sid, "correction", row.get("display_name", "user"),
                                {"feedback": feedback})
                card, new, ack = apply_correction(_llm(), sheet, card, feedback)
                did = STORE.add_distillation(
                    row["user_id"], card.markdown, sid=sid,
                    provider=(_llm().spec.name if _llm().spec else "dry-run"),
                    model=_llm().model,
                    params={"corrections": new, "based_on": dist["version"], "feedback": feedback},
                    coverage=card.coverage, evidence=card.evidence_summary(),
                    analysis=card.analysis,
                )
                card.save(OUT / "cards")
                return self._json(
                    {
                        "card_markdown": card.markdown,
                        "corrections": new,
                        "new_corrections": new,
                        "acknowledgement": ack,
                        "distillation_id": did,
                    }
                )
            if path == "/api/encounter":
                row = _session_row(body["session_id"])
                sid = body["session_id"]
                dist = STORE.latest_distillation(row["user_id"])
                if dist is None:
                    return self._error("還沒有人格卡，請先蒸餾")
                scenario = SCENARIO_BY_ID.get(body.get("scenario_id") or "") or SCENARIOS[0]
                witnesses = body.get("witnesses") or None
                enc = Encounter(
                    llm=_llm(),
                    scenario=scenario,
                    player_name=row.get("display_name", "玩家"),
                    player_card=dist["card_markdown"],
                    witness_ids=witnesses,
                    max_rounds=body.get("max_rounds"),
                )
                enc_sid = secrets.token_urlsafe(12)
                STORE.start_session(
                    enc_sid, row["user_id"], "encounter",
                    label=scenario.name,
                    meta={
                        "scenario_id": scenario.id,
                        "witnesses": [w.id for w in enc.witnesses],
                        "max_rounds": enc.max_rounds,
                        "based_on_distillation": dist["version"],
                    },
                )
                STORE.add_event(enc_sid, "system", "app",
                                {"msg": "進入場景", "scenario": scenario.name})
                ENCOUNTERS[enc_sid] = enc
                return self._json(
                    {
                        "session_id": enc_sid,
                        "scenario": scenario.name,
                        "setting": scenario.setting,
                        "stakes": scenario.stakes,
                        "pressure": list(scenario.pressure),
                        "players": [row.get("display_name", "玩家")] + [w.name for w in enc.witnesses],
                    }
                )
            if path == "/api/turn":
                sid = body["session_id"]
                enc = ENCOUNTERS.get(sid)
                if enc is None:
                    return self._error("還沒開始冒險（或伺服器已重啟，請重新進入場景）")
                turn = enc.step()
                if turn is None:
                    STORE.end_session(sid, {"ended_reason": enc.ended_reason})
                    return self._json({"turn": None, "finished": True,
                                       "ended_reason": enc.ended_reason})
                STORE.add_turn(sid, turn.round_no, turn.speaker, turn.is_player,
                               turn.monologue, turn.public)
                STORE.add_event(sid, "turn", turn.speaker, turn.to_dict())
                return self._json(
                    {
                        "turn": turn.to_dict(),
                        "next_speaker": enc.next_speaker,
                        "round": enc.round_no,
                        "finished": enc.finished,
                        "ended_reason": enc.ended_reason,
                    }
                )
            if path == "/api/finish":
                sid = body["session_id"]
                enc = ENCOUNTERS.get(sid)
                if enc is None:
                    return self._error("還沒開始冒險")
                log = enc.log()
                p = OUT / "logs"
                p.mkdir(parents=True, exist_ok=True)
                from .__main__ import render_log_markdown, slugify

                js = log.save(p / f"{log.scenario_id}-{slugify(log.player_name)}.json")
                md = p / f"{log.scenario_id}-{slugify(log.player_name)}.md"
                md.write_text(render_log_markdown(log), encoding="utf-8")
                STORE.end_session(sid, {"ended_reason": log.ended_reason,
                                        "turns": len(log.turns)})
                STORE.add_event(sid, "system", "app",
                                {"msg": "對話結束", "reason": log.ended_reason,
                                 "turns": len(log.turns)})
                return self._json(
                    {"json": str(js), "markdown": str(md),
                     "markdown_text": render_log_markdown(log),
                     "turns_stored": len(STORE.turns(sid))}
                )
            return self._error("not found", 404)
        except KeyError as e:
            return self._error(f"缺少欄位或 {e}", 400)
        except LLMError as e:
            return self._error(str(e), 502)
        except Exception as e:  # noqa: BLE001
            return self._error(f"{type(e).__name__}: {e}", 500)

    # -- 檔案上傳 ---------------------------------------------------------

    def _handle_upload(self, body: dict[str, Any]) -> None:
        """接收 multipart/form-data，解析出檔案並存進素材池。純標準庫。"""
        ctype = self.headers.get("content-type", "")
        if "multipart/form-data" not in ctype:
            return self._error("請用 multipart/form-data 上傳")
        m = re.search(r"boundary=([^;]+)", ctype)
        if not m:
            return self._error("multipart 缺少 boundary")
        boundary = m.group(1).strip().strip('"').encode()

        n = int(self.headers.get("content-length") or 0)
        if n <= 0:
            return self._error("沒有收到內容")
        if n > 40 * 1024 * 1024:
            return self._error("檔案太大（上限 40 MB）", 413)
        raw = self.rfile.read(n)

        parts = raw.split(b"--" + boundary)
        sid: str | None = None
        files: list[tuple[str, bytes]] = []
        for part in parts:
            if b"\r\n\r\n" not in part:
                continue
            head, _, data = part.partition(b"\r\n\r\n")
            data = data.rstrip(b"\r\n-")
            hs = head.decode("utf-8", "replace")
            name_m = re.search(r'name="([^"]*)"', hs)
            file_m = re.search(r'filename="([^"]*)"', hs)
            if not name_m:
                continue
            if name_m.group(1) == "session_id" and not file_m:
                sid = data.decode("utf-8", "replace").strip()
            elif file_m:
                files.append((file_m.group(1), data))

        if not sid:
            return self._error("缺少 session_id")
        _session_row(sid)
        if not files:
            return self._error("沒有收到檔案")

        from .sources import coverage_of, parse_file

        docs = [parse_file(fn, blob) for fn, blob in files]
        pool = SOURCE_POOL.setdefault(sid, [])
        have = {d.url for d in pool}
        pool.extend(d for d in docs if d.url not in have)
        return self._json(
            {
                "docs": [d.to_dict() for d in docs],
                "coverage": coverage_of(pool),
                "pool_size": len(pool),
            }
        )

    # -- 靜態檔 -----------------------------------------------------------

    def _static(self, rel: str) -> None:
        target = (WEB / rel).resolve()
        if not str(target).startswith(str(WEB.resolve())) or not target.is_file():
            return self._error("not found", 404)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix in (".html", ".js", ".css"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)


def _q(q: Any) -> dict[str, Any]:
    return {
        "id": q.id,
        "text": q.text,
        "intent": q.intent,
        "kind": q.kind,
        "min_chars": q.min_chars,
        "follow_up": (
            {"min_chars": q.follow_up.min_chars, "prompts": list(q.follow_up.prompts)}
            if q.follow_up
            else None
        ),
        "optional": q.optional,
    }


def main(argv: list[str] | None = None) -> int:
    global STORE
    ap = argparse.ArgumentParser(prog="persona_sandbox.server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--provider")
    ap.add_argument("--model")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--db", default=None, help="SQLite 檔案路徑（預設 persona_sandbox.db）")
    args = ap.parse_args(argv)

    LLM_OPTS.update(provider=args.provider, model=args.model, dry_run=args.dry_run)
    load_env_file()
    spec = detect_provider(args.provider)
    mode = "dry-run（無金鑰）" if (args.dry_run or spec is None) else f"{spec.name} / {args.model or spec.default_model}"

    STORE = Store(args.db) if args.db else Store()
    s = STORE.stats()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.verbose = args.verbose  # type: ignore[attr-defined]
    url = f"http://{args.host}:{args.port}"
    print("Persona Sandbox MVP")
    print(f"  → {url}")
    print(f"  LLM: {mode}")
    print(f"  DB : {s['db_path']}  ({s['db_size_kb']} KB)")
    print(f"       使用者 {s['users']} · session {s['sessions']} · 事件 {s['events']}"
          f" · 回合 {s['turns']} · 人格卡 {s['distillations']}")
    print("  Ctrl-C 結束\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n再見。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
