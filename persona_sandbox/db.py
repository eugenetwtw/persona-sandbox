"""資料庫工具：檢視、匯出、以及把既有 out/*.json 遷移進 SQLite。

遷移的理由：在接上 SQLite 之前，資料散在 out/answers、out/cards、out/logs 的 JSON 檔裡。
那些是真實跑過的紀錄，不該因為換了儲存方式就消失。

    python3 -m persona_sandbox.db stats
    python3 -m persona_sandbox.db users
    python3 -m persona_sandbox.db migrate      # 把 out/ 的 JSON 匯進 DB
    python3 -m persona_sandbox.db export <user_slug>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .persona.engine import slugify
from .store import DEFAULT_DB, Store

OUT = Path(__file__).resolve().parent / "out"


# --------------------------------------------------------------------------
# 遷移
# --------------------------------------------------------------------------


def migrate_from_out(store: Store, out: Path = OUT, *, verbose: bool = True) -> dict[str, int]:
    """把 out/ 底下既有的 JSON 產出讀進資料庫。可重複執行（以 slug+版本去重）。"""
    counts = {"answers": 0, "cards": 0, "logs": 0, "skipped": 0}

    # --- 問卷答案 -> user + questionnaire session + events -----------------
    for f in sorted((out / "answers").glob("*.json")) if (out / "answers").exists() else []:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            counts["skipped"] += 1
            continue
        name = data.get("name") or f.stem
        slug = slugify(name)
        uid = store.get_or_create_user(name, slug, note="由 out/answers 遷移")
        sid = _existing_questionnaire_sid(store, uid)
        if sid is None:
            sid = f"migrated-q-{slug}"
            store.start_session(sid, uid, "questionnaire", label=name,
                                meta={"migrated_from": str(f)})
            for a in data.get("answers", []):
                store.add_event(
                    sid, "skip" if a.get("skipped") else "answer", name,
                    {"qid": a.get("qid"), "text": a.get("text", ""),
                     "follow_ups": a.get("follow_ups") or [],
                     "skipped": bool(a.get("skipped"))},
                )
        counts["answers"] += 1

    # --- 人格卡 JSON -> distillations --------------------------------------
    for f in sorted((out / "cards").glob("*.json")) if (out / "cards").exists() else []:
        if f.name.startswith("_"):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            counts["skipped"] += 1
            continue
        md = data.get("markdown") or ""
        if not md.strip():
            counts["skipped"] += 1
            continue
        name = data.get("name") or f.stem
        uid = store.get_or_create_user(name, slugify(name), note="由 out/cards 遷移")
        if _already_has_card(store, uid, md):
            counts["skipped"] += 1
            continue
        model = data.get("model", "")
        prov, _, mdl = model.partition(":")
        store.add_distillation(
            uid, md,
            provider=prov, model=mdl or model,
            params={"migrated_from": str(f), "created_at": data.get("created_at", "")},
            coverage=data.get("coverage") or {},
            evidence={}, analysis=data.get("analysis") or {},
        )
        counts["cards"] += 1

    # --- 冒險日誌 -> encounter session + turns -----------------------------
    for f in sorted((out / "logs").glob("*.json")) if (out / "logs").exists() else []:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            counts["skipped"] += 1
            continue
        name = data.get("player_name") or f.stem
        uid = store.get_or_create_user(name, slugify(name), note="由 out/logs 遷移")
        sid = f"migrated-e-{f.stem}"
        if store.session_by_sid(sid):
            counts["skipped"] += 1
            continue
        store.start_session(
            sid, uid, "encounter",
            label=data.get("scenario_name", ""),
            meta={"migrated_from": str(f), "witnesses": data.get("witnesses", [])},
        )
        for t in data.get("turns", []):
            store.add_turn(sid, t.get("round", 0), t.get("speaker", ""),
                           bool(t.get("is_player")), t.get("monologue", ""), t.get("public", ""))
            store.add_event(sid, "turn", t.get("speaker", ""), t)
        store.end_session(sid, {"ended_reason": data.get("ended_reason", "")})
        counts["logs"] += 1

    if verbose:
        print("遷移完成：")
        print(f"  問卷  {counts['answers']} 份")
        print(f"  人格卡 {counts['cards']} 張")
        print(f"  日誌  {counts['logs']} 場")
        print(f"  略過  {counts['skipped']} 個（已存在或無內容）")
    return counts


def _existing_questionnaire_sid(store: Store, uid: int) -> str | None:
    with store.connect() as c:
        r = c.execute(
            "SELECT sid FROM sessions WHERE user_id = ? AND kind = 'questionnaire'"
            " ORDER BY id LIMIT 1",
            (uid,),
        ).fetchone()
    return r["sid"] if r else None


def _already_has_card(store: Store, uid: int, markdown: str) -> bool:
    import hashlib

    h = hashlib.sha256(markdown.encode("utf-8")).hexdigest()[:16]
    with store.connect() as c:
        r = c.execute(
            "SELECT 1 FROM distillations WHERE user_id = ? AND card_sha256 = ? LIMIT 1",
            (uid, h),
        ).fetchone()
    return r is not None


# --------------------------------------------------------------------------
# 檢視
# --------------------------------------------------------------------------


def show_stats(store: Store) -> None:
    s = store.stats()
    print(f"\n資料庫：{s['db_path']}  ({s['db_size_kb']} KB)\n")
    print(f"  使用者        {s['users']}")
    print(f"  sessions      {s['sessions']}")
    print(f"  事件          {s['events']}")
    print(f"  對話回合      {s['turns']}")
    print(f"  蒸餾結果      {s['distillations']}")
    print(f"  證據紀錄      {s['evidence_rows']}\n")


def show_users(store: Store) -> None:
    users = store.list_users()
    if not users:
        print("\n（還沒有任何使用者）\n")
        return
    print(f"\n{'id':<4} {'代號':<14} {'sessions':<9} {'卡片':<5} {'最新版':<6} 最後出現")
    print("-" * 68)
    for u in users:
        print(f"{u['id']:<4} {u['display_name'][:12]:<14} {u['sessions']:<9} "
              f"{u['cards']:<5} {u['latest_version'] or '-':<6} {u['last_seen_at']}")
    print()


def show_sessions(store: Store, user_slug: str | None = None) -> None:
    with store.connect() as c:
        sql = """SELECT s.sid, s.kind, s.label, s.started_at, s.ended_at,
                        u.display_name,
                        (SELECT COUNT(*) FROM events e WHERE e.session_id = s.id) AS ev,
                        (SELECT COUNT(*) FROM turns t WHERE t.session_id = s.id) AS tn
                 FROM sessions s JOIN users u ON u.id = s.user_id"""
        args: tuple = ()
        if user_slug:
            sql += " WHERE u.slug = ?"
            args = (user_slug,)
        sql += " ORDER BY s.id DESC"
        rows = c.execute(sql, args).fetchall()
    if not rows:
        print("\n（沒有 session）\n")
        return
    print(f"\n{'sid':<22} {'類型':<15} {'使用者':<12} {'事件':<5} {'回合':<5} 開始")
    print("-" * 84)
    for r in rows:
        print(f"{r['sid'][:20]:<22} {r['kind']:<15} {r['display_name'][:10]:<12} "
              f"{r['ev']:<5} {r['tn']:<5} {r['started_at']}")
    print()


def show_cards(store: Store, user_slug: str | None = None) -> None:
    with store.connect() as c:
        sql = """SELECT d.id, d.version, d.created_at, d.provider, d.model,
                        d.card_sha256, u.display_name, u.slug,
                        (SELECT COUNT(*) FROM distill_evidence e
                         WHERE e.distillation_id = d.id) AS ev
                 FROM distillations d JOIN users u ON u.id = d.user_id"""
        args: tuple = ()
        if user_slug:
            sql += " WHERE u.slug = ?"
            args = (user_slug,)
        sql += " ORDER BY d.id DESC LIMIT 50"
        rows = c.execute(sql, args).fetchall()
    if not rows:
        print("\n（沒有蒸餾結果）\n")
        return
    print(f"\n{'id':<4} {'使用者':<12} {'v':<3} {'模型':<24} {'證據':<5} 時間")
    print("-" * 74)
    for r in rows:
        print(f"{r['id']:<4} {r['display_name'][:10]:<12} {r['version']:<3} "
              f"{(r['provider'] + ':' + r['model'])[:22]:<24} {r['ev']:<5} {r['created_at']}")
    print()


def export_user(store: Store, user_slug: str, dest: Path) -> None:
    """把某個使用者的完整資料（含對話史與所有版本的人格卡）匯出成一個 JSON。"""
    with store.connect() as c:
        u = c.execute("SELECT * FROM users WHERE slug = ?", (user_slug,)).fetchone()
        if not u:
            print(f"❌ 找不到使用者：{user_slug}", file=sys.stderr)
            raise SystemExit(1)
        sessions = c.execute(
            "SELECT * FROM sessions WHERE user_id = ? ORDER BY id", (u["id"],)
        ).fetchall()
        cards = c.execute(
            "SELECT * FROM distillations WHERE user_id = ? ORDER BY version", (u["id"],)
        ).fetchall()

    payload = {
        "user": dict(u),
        "sessions": [],
        "distillations": [],
    }
    for s in sessions:
        ev = store.events(s["sid"])
        payload["sessions"].append({
            "sid": s["sid"], "kind": s["kind"], "label": s["label"],
            "started_at": s["started_at"], "ended_at": s["ended_at"],
            "meta": json.loads(s["meta_json"] or "{}"),
            "events": ev,
            "turns": store.turns(s["sid"]),
        })
    for d in cards:
        dd = store._hydrate(d)
        with store.connect() as c:
            ev = c.execute(
                "SELECT dimension, claim, evidence_ref, level, is_gap"
                " FROM distill_evidence WHERE distillation_id = ?", (d["id"],)
            ).fetchall()
        dd["evidence_rows"] = [dict(r) for r in ev]
        payload["distillations"].append(dd)

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 已匯出到 {dest}")
    print(f"   session {len(payload['sessions'])} 個、人格卡 {len(payload['distillations'])} 版")


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="persona_sandbox.db", description="資料庫工具")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats", help="資料庫統計")
    sub.add_parser("users", help="列出使用者")
    sub.add_parser("sessions", help="列出對話 session").add_argument("--user", help="使用者 slug")
    sub.add_parser("cards", help="列出蒸餾結果").add_argument("--user", help="使用者 slug")
    sub.add_parser("migrate", help="把 out/ 的 JSON 匯進資料庫")
    p_ex = sub.add_parser("export", help="匯出某使用者的完整資料")
    p_ex.add_argument("user")
    p_ex.add_argument("--out", default=None)

    args = ap.parse_args(argv)
    store = Store(args.db)

    if args.cmd == "stats":
        show_stats(store)
    elif args.cmd == "users":
        show_users(store)
    elif args.cmd == "sessions":
        show_sessions(store, getattr(args, "user", None))
    elif args.cmd == "cards":
        show_cards(store, getattr(args, "user", None))
    elif args.cmd == "migrate":
        migrate_from_out(store)
        show_stats(store)
    elif args.cmd == "export":
        dest = Path(args.out) if args.out else OUT / "export" / f"{args.user}.json"
        export_user(store, args.user, dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
