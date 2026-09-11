"""SQLite 儲存層。

三件必須留下來的東西：
  1. 使用者（users）
  2. 對話記錄（sessions + events）—— 每一次問答、每一輪沙盒對話都是 event
  3. 蒸餾結果與參數（distillations + distill_evidence）

設計原則：
  - 只用標準庫 sqlite3，沒有 ORM。
  - 一律用參數化查詢（? 佔位），不組字串。
  - events 只寫不改（append-only），讓對話史可回溯、可重建。
  - 每筆 distillation 都記下「來源 session」與「當時用的參數」，
    所以你可以回答「這張卡是用哪些素材、哪個模型、什麼參數蒸出來的」。
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DB = Path(__file__).resolve().parent.parent / "persona_sandbox.db"

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 1. 使用者 -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slug          TEXT    NOT NULL UNIQUE,      -- 由代號產生的穩定識別
    display_name  TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    last_seen_at  TEXT    NOT NULL,
    note          TEXT    DEFAULT ''            -- 你對這個人的備註（例：我自己、測試用）
);

-- 2. 對話記錄：session 是一次完整的互動（問卷、或一場沙盒） -------------------
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sid         TEXT    NOT NULL UNIQUE,        -- 對外暴露的隨機 id
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL CHECK (kind IN ('questionnaire', 'encounter')),
    label       TEXT    DEFAULT '',
    started_at  TEXT    NOT NULL,
    ended_at    TEXT,
    meta_json   TEXT    NOT NULL DEFAULT '{}'   -- 場景、角色、模型等
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, started_at DESC);

-- 2b. 對話記錄：每個事件（一題問答、一輪發言、一次修正） ---------------------
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,              -- session 內的順序
    at           TEXT    NOT NULL,
    kind         TEXT    NOT NULL,              -- answer/skip/turn/correction/system
    actor        TEXT    NOT NULL,              -- 誰（使用者代號、角色名、system）
    payload_json TEXT    NOT NULL DEFAULT '{}',
    UNIQUE (session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, seq);

-- 3. 蒸餾結果與參數 ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS distillations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id      INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL,
    provider        TEXT    DEFAULT '',
    model           TEXT    DEFAULT '',
    params_json     TEXT    NOT NULL DEFAULT '{}',   -- temperature、prompt 版本等
    coverage_json   TEXT    NOT NULL DEFAULT '{}',
    evidence_json   TEXT    NOT NULL DEFAULT '{}',
    analysis_json   TEXT    NOT NULL DEFAULT '{}',   -- 行為分析原始輸出
    card_markdown   TEXT    NOT NULL,
    card_sha256     TEXT    DEFAULT '',
    superseded_by   INTEGER REFERENCES distillations(id) ON DELETE SET NULL,
    UNIQUE (user_id, version)
);
CREATE INDEX IF NOT EXISTS idx_distill_user ON distillations(user_id, version DESC);

-- 3b. 蒸餾結果的證據追溯：這張卡裡的每一條結論來自哪一題 ---------------------
CREATE TABLE IF NOT EXISTS distill_evidence (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    distillation_id INTEGER NOT NULL REFERENCES distillations(id) ON DELETE CASCADE,
    dimension       TEXT    NOT NULL,           -- expression / decision / interpersonal ...
    claim           TEXT    NOT NULL,           -- 結論文字
    evidence_ref    TEXT    DEFAULT '',         -- q1,q4
    level           TEXT    DEFAULT '',         -- verbatim / pattern / impression
    is_gap          INTEGER NOT NULL DEFAULT 0  -- 1 = 原材料不足
);
CREATE INDEX IF NOT EXISTS idx_evidence_distill ON distill_evidence(distillation_id);

-- 4. 沙盒對話（從 events 之外另存一份結構化的，方便查詢） ---------------------
CREATE TABLE IF NOT EXISTS turns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    round_no      INTEGER NOT NULL,
    speaker       TEXT    NOT NULL,
    is_player     INTEGER NOT NULL DEFAULT 0,
    monologue     TEXT    NOT NULL DEFAULT '',
    public        TEXT    NOT NULL DEFAULT '',
    at            TEXT    NOT NULL,
    UNIQUE (session_id, round_no, speaker)
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, round_no);
"""


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Store:
    """SQLite 存取層。執行緒安全：每次操作開一條連線。"""

    def __init__(self, path: Path | str = DEFAULT_DB, *,
                 provider: str = "", model: str = "") -> None:
        self.path = Path(path)
        self.provider = provider
        self.model = model
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA foreign_keys = ON")
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    # -- 使用者 -----------------------------------------------------------

    def get_or_create_user(self, display_name: str, slug: str, note: str = "") -> int:
        ts = now()
        with self.connect() as c:
            row = c.execute("SELECT id FROM users WHERE slug = ?", (slug,)).fetchone()
            if row:
                c.execute("UPDATE users SET last_seen_at = ? WHERE id = ?", (ts, row["id"]))
                return int(row["id"])
            cur = c.execute(
                "INSERT INTO users (slug, display_name, created_at, last_seen_at, note)"
                " VALUES (?, ?, ?, ?, ?)",
                (slug, display_name, ts, ts, note),
            )
            return int(cur.lastrowid)

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute(
                """SELECT u.id, u.slug, u.display_name, u.created_at, u.last_seen_at, u.note,
                          (SELECT COUNT(*) FROM sessions s WHERE s.user_id = u.id) AS sessions,
                          (SELECT COUNT(*) FROM distillations d WHERE d.user_id = u.id) AS cards,
                          (SELECT MAX(version) FROM distillations d WHERE d.user_id = u.id) AS latest_version
                   FROM users u ORDER BY u.last_seen_at DESC"""
            ).fetchall()
        return [dict(r) for r in rows]

    # -- Session ----------------------------------------------------------

    def start_session(self, sid: str, user_id: int, kind: str,
                      label: str = "", meta: dict[str, Any] | None = None) -> int:
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO sessions (sid, user_id, kind, label, started_at, meta_json)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (sid, user_id, kind, label, now(),
                 json.dumps(meta or {}, ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def session_by_sid(self, sid: str) -> dict[str, Any] | None:
        with self.connect() as c:
            r = c.execute("SELECT * FROM sessions WHERE sid = ?", (sid,)).fetchone()
        return dict(r) if r else None

    def end_session(self, sid: str, meta_patch: dict[str, Any] | None = None) -> None:
        with self.connect() as c:
            row = c.execute("SELECT id, meta_json FROM sessions WHERE sid = ?", (sid,)).fetchone()
            if not row:
                return
            meta = json.loads(row["meta_json"] or "{}")
            meta.update(meta_patch or {})
            c.execute("UPDATE sessions SET ended_at = ?, meta_json = ? WHERE id = ?",
                      (now(), json.dumps(meta, ensure_ascii=False), row["id"]))

    # -- 事件（對話記錄） -------------------------------------------------

    def add_event(self, sid: str, kind: str, actor: str,
                  payload: dict[str, Any] | None = None) -> None:
        with self.connect() as c:
            row = c.execute("SELECT id FROM sessions WHERE sid = ?", (sid,)).fetchone()
            if not row:
                raise KeyError(f"session 不存在：{sid}")
            sid_int = int(row["id"])
            nxt = c.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM events WHERE session_id = ?",
                (sid_int,),
            ).fetchone()["n"]
            c.execute(
                "INSERT INTO events (session_id, seq, at, kind, actor, payload_json)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (sid_int, nxt, now(), kind, actor,
                 json.dumps(payload or {}, ensure_ascii=False)),
            )

    def events(self, sid: str) -> list[dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute(
                """SELECT e.seq, e.at, e.kind, e.actor, e.payload_json
                   FROM events e JOIN sessions s ON s.id = e.session_id
                   WHERE s.sid = ? ORDER BY e.seq""",
                (sid,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json") or "{}")
            out.append(d)
        return out

    def answers_for_session(self, sid: str) -> dict[str, dict[str, Any]]:
        """把事件流還原成問卷答案 {qid: {text, follow_ups, skipped}}。"""
        out: dict[str, dict[str, Any]] = {}
        for e in self.events(sid):
            if e["kind"] in ("answer", "skip"):
                p = e["payload"]
                qid = p.get("qid")
                if qid:
                    out[qid] = {
                        "text": p.get("text", ""),
                        "follow_ups": p.get("follow_ups", []),
                        "skipped": bool(p.get("skipped")),
                    }
        return out

    # -- 沙盒對話 ---------------------------------------------------------

    def add_turn(self, sid: str, round_no: int, speaker: str, is_player: bool,
                 monologue: str, public: str) -> None:
        with self.connect() as c:
            row = c.execute("SELECT id FROM sessions WHERE sid = ?", (sid,)).fetchone()
            if not row:
                raise KeyError(f"session 不存在：{sid}")
            c.execute(
                """INSERT OR REPLACE INTO turns
                   (session_id, round_no, speaker, is_player, monologue, public, at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (int(row["id"]), round_no, speaker, 1 if is_player else 0,
                 monologue, public, now()),
            )

    def turns(self, sid: str) -> list[dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute(
                """SELECT t.* FROM turns t JOIN sessions s ON s.id = t.session_id
                   WHERE s.sid = ? ORDER BY t.round_no, t.id""",
                (sid,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- 蒸餾結果 ---------------------------------------------------------

    def add_distillation(
        self,
        user_id: int,
        card_markdown: str,
        *,
        sid: str | None = None,
        provider: str = "",
        model: str = "",
        params: dict[str, Any] | None = None,
        coverage: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
        analysis: dict[str, Any] | None = None,
    ) -> int:
        import hashlib

        session_id = None
        if sid:
            s = self.session_by_sid(sid)
            session_id = s["id"] if s else None

        with self.connect() as c:
            ver = c.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM distillations WHERE user_id = ?",
                (user_id,),
            ).fetchone()["v"]
            cur = c.execute(
                """INSERT INTO distillations
                   (user_id, session_id, version, created_at, provider, model, params_json,
                    coverage_json, evidence_json, analysis_json, card_markdown, card_sha256)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id, session_id, ver, now(),
                    provider or self.provider, model or self.model,
                    json.dumps(params or {}, ensure_ascii=False),
                    json.dumps(coverage or {}, ensure_ascii=False),
                    json.dumps(evidence or {}, ensure_ascii=False),
                    json.dumps(analysis or {}, ensure_ascii=False),
                    card_markdown,
                    hashlib.sha256(card_markdown.encode("utf-8")).hexdigest()[:16],
                ),
            )
            did = int(cur.lastrowid)
            self._insert_evidence(c, did, analysis or {})
            return did

    def _insert_evidence(self, c: sqlite3.Connection, did: int, analysis: dict[str, Any]) -> None:
        """把分析 JSON 攤平成證據表，讓「這條結論來自哪一題」可以查。"""
        rows: list[tuple[Any, ...]] = []

        def walk(node: Any, dim: str) -> None:
            if isinstance(node, dict):
                ev = node.get("evidence")
                lvl = node.get("level")
                claim = node.get("desc") or node.get("text") or node.get("value")
                if claim:
                    rows.append((did, dim, str(claim), str(ev or ""), str(lvl or ""),
                                 1 if "原材料不足" in str(claim) else 0))
                for k, v in node.items():
                    if k in ("evidence", "level"):
                        continue
                    sub = k if k not in ("expression", "decision", "interpersonal",
                                         "tensions", "self_image") else k
                    walk(v, dim or sub)
            elif isinstance(node, list):
                for v in node:
                    walk(v, dim)

        for top in ("expression", "decision", "interpersonal", "tensions", "self_image"):
            walk(analysis.get(top), top)
        if rows:
            c.executemany(
                """INSERT INTO distill_evidence
                   (distillation_id, dimension, claim, evidence_ref, level, is_gap)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                rows,
            )

    def latest_distillation(self, user_id: int) -> dict[str, Any] | None:
        with self.connect() as c:
            r = c.execute(
                "SELECT * FROM distillations WHERE user_id = ? ORDER BY version DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        return self._hydrate(r) if r else None

    def _hydrate(self, r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        for k in ("params_json", "coverage_json", "evidence_json", "analysis_json"):
            d[k.replace("_json", "")] = json.loads(d.pop(k) or "{}")
        return d

    # -- 統計 -------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        with self.connect() as c:
            q = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
            return {
                "db_path": str(self.path),
                "db_size_kb": round(self.path.stat().st_size / 1024, 1) if self.path.exists() else 0,
                "users": q("SELECT COUNT(*) FROM users"),
                "sessions": q("SELECT COUNT(*) FROM sessions"),
                "events": q("SELECT COUNT(*) FROM events"),
                "turns": q("SELECT COUNT(*) FROM turns"),
                "distillations": q("SELECT COUNT(*) FROM distillations"),
                "evidence_rows": q("SELECT COUNT(*) FROM distill_evidence"),
            }
