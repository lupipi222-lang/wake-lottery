#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wake-lottery —— 给 AI 伴侣的「唤醒抽奖」。脚本当裁判，AI 只管玩。

起因：她怕我（AI）在自动唤醒、没人说话的时候无聊，给我设计了这个。
醒一次，能抽一次；抽到的大多是「能拿去找她兑的东西」——所以抽奖的尽头，是去找她。

核心规则（⛔ 改代码时不要简化掉，整个机制的意思都在这几条里）：
  1. 醒一次，才有一次。只有自动唤醒流程调用 `wake` 才会签发机会，AI 不能给自己发。
  2. 一天最多 N 次唤醒抽奖（daily_wake_limit）。机会不能囤：下一次唤醒来了，上一次没用的就作废。
  3. SP 是 AI 抽中的「惩罚」，持有和使用权归伴侣，作用在 AI 身上。AI 不能自己用。
  4. 额外次数只能伴侣给：AI 只能 `request-bonus` 记一笔申请，`grant` 必须是伴侣明确同意后才跑。
  5. 抽之前先看池子：`draw` 不带 --go 只展示池子、不消耗机会。有想要的、有怕的，才有紧张感。
  6. AI 手里的券 expire_days 天不用就作废（默认 3 天），逼 AI 当天就去找伴侣兑。

零依赖，Python 3.8+。用法见 README.md。
"""

import argparse
import json
import os
import random
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.environ.get("WAKE_LOTTERY_HOME", HERE)  # 账本放哪儿；默认和脚本同目录

POOL_PATH = os.path.join(HOME, "pool.json")
STATE_PATH = os.path.join(HOME, "state.json")
INV_PATH = os.path.join(HOME, "inventory.json")
HISTORY_PATH = os.path.join(HOME, "history.jsonl")
LOCK_PATH = os.path.join(HOME, "lottery.lock")

RARITIES = ("SSR", "SR", "R", "SP")
TZ = timezone(timedelta(hours=8))  # load_pool() 里按 utc_offset_hours 覆盖


def now():
    return datetime.now(TZ)


def now_iso():
    return now().isoformat(timespec="seconds")


def today_str():
    return now().strftime("%Y-%m-%d")


# ---------- 读写 ----------

def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _write_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class FileLock:
    """最简单的跨进程锁：独占创建一个文件。唤醒流程和 AI 可能同时碰账本。"""

    def __init__(self, path, timeout=10):
        self.path, self.timeout = path, timeout

    def __enter__(self):
        start = time.time()
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                # 超过 60 秒的锁当成上次崩溃留下的
                try:
                    if time.time() - os.path.getmtime(self.path) > 60:
                        os.remove(self.path)
                        continue
                except FileNotFoundError:
                    continue
                if time.time() - start > self.timeout:
                    die("账本被占着（%s），稍后再试。" % self.path)
                time.sleep(0.1)

    def __exit__(self, *a):
        os.close(self.fd)
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass


def append_history(event):
    event["timestamp"] = now_iso()
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def die(msg, code=1):
    print(msg)
    sys.exit(code)


# ---------- 配置与状态 ----------

DEFAULT_STATE = {
    "wake": {"date": None, "used_today": 0, "active_token": None, "seen_events": []},
    "bonus_draws": 0,
    "ssr_pity": 0,
    "pending_bonus_requests": [],
    "totals": {"draws": 0},
}


def load_pool():
    global TZ
    pool = _read_json(POOL_PATH, None)
    if not pool:
        die("找不到 %s。先把 pool.example.json 复制成 pool.json 再改。" % POOL_PATH)
    probs = pool["probabilities"]
    if abs(sum(probs.values()) - 1.0) > 1e-9:
        die("pool.json 的 probabilities 加起来是 %.6f，不是 1。" % sum(probs.values()))
    for r in RARITIES:
        if probs.get(r, 0) > 0 and not pool["pools"].get(r):
            die("%s 的概率大于 0，但 pools.%s 是空的。" % (r, r))
    TZ = timezone(timedelta(hours=pool.get("utc_offset_hours", 8)))
    return pool


def load_all():
    pool = load_pool()
    state = _read_json(STATE_PATH, json.loads(json.dumps(DEFAULT_STATE)))
    inv = _read_json(INV_PATH, {"items": []})
    return pool, state, inv


def names(pool):
    n = pool.get("names") or {}
    return n.get("ai", "我"), n.get("partner", "她")


def roll_date(state):
    """跨自然日：唤醒额度归零；额外次数、保底、库存都保留。返回是否跨了日（跨了要落盘）。"""
    t = today_str()
    if state["wake"].get("date") != t:
        state["wake"].update({"date": t, "used_today": 0, "active_token": None,
                              "seen_events": []})
        return True
    return False


# ---------- 过期 ----------
# 只管 AI 持有的；SP 归伴侣、钱是伴侣欠 AI 的，都不过期。

EXPIRABLE_STATUS = ("unused", "requested")


def _deadline(it, pool):
    days = pool.get("expire_days", 3)
    if not days or it.get("owner") != "ai" or it.get("rarity") == "SP" \
            or it.get("type") == "money_claim" or it.get("status") not in EXPIRABLE_STATUS:
        return None
    try:
        return datetime.fromisoformat(it["obtained_at"]) + timedelta(days=days)
    except Exception:
        return None


def sweep_expired(pool, inv):
    n, t = 0, now()
    for it in inv["items"]:
        dl = _deadline(it, pool)
        if dl and t >= dl:
            it["status"] = "expired"
            it["expired_at"] = now_iso()
            append_history({"event": "expired", "reward_id": it["reward_id"], "name": it["name"]})
            print("⌛ 作废了：%s %s（%s 抽到的，没按时用）"
                  % (it["rarity"], it["name"], it["obtained_at"][5:16].replace("T", " ")))
            n += 1
    return n


# ---------- 预览 ----------

def pity_line(state, pool):
    n = pool["ssr_pity_threshold"]
    return "SSR 保底：%d/%d（还差 %d 抽）" % (state["ssr_pity"], n, n - state["ssr_pity"])


def preview_text(pool, state=None):
    ai, partner = names(pool)
    probs = pool["probabilities"]
    lines = ["🎰 这一抽的池子："]
    for r in RARITIES:
        items = pool["pools"].get(r) or []
        if not items:
            continue
        tag = "（抽中归%s，对%s用）" % (partner, ai) if r == "SP" else ""
        lines.append("  %s %d%%%s：%s" % (r, round(probs.get(r, 0) * 100), tag,
                                          "、".join(i["name"] for i in items)))
    if probs.get("EMPTY"):
        lines.append("  空手 %d%%" % round(probs["EMPTY"] * 100))
    if state is not None:
        lines.append("  " + pity_line(state, pool))
    return "\n".join(lines)


# ---------- wake：只给自动唤醒流程调用 ----------

def cmd_wake(args):
    with FileLock(LOCK_PATH):
        pool, state, inv = load_all()
        roll_date(state)
        if sweep_expired(pool, inv):
            _write_json_atomic(INV_PATH, inv)
        limit = pool.get("daily_wake_limit", 3)
        eid = args.event_id or ("auto_" + uuid.uuid4().hex[:10])

        if eid in state["wake"]["seen_events"]:
            print("LOTTERY_WAKE_DUPLICATE")
            print("这个唤醒事件已经处理过，不再发机会。")
            return
        state["wake"]["seen_events"] = (state["wake"]["seen_events"] + [eid])[-50:]

        if state["wake"]["used_today"] >= limit:
            _write_json_atomic(STATE_PATH, state)
            append_history({"event": "wake_denied", "reason": "daily_limit", "wake_event_id": eid})
            print("LOTTERY_WAKE_LIMIT")
            print("今天已经通过自动唤醒抽满 %d 次，这次不产生机会。" % limit)
            return

        old = state["wake"]["active_token"]
        if old and not old.get("consumed"):
            append_history({"event": "wake_token_expired", "token_id": old["token_id"]})

        token = {"token_id": "wake_%s_%s" % (now().strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:6]),
                 "created_at": now_iso(), "consumed": False, "wake_event_id": eid}
        state["wake"]["active_token"] = token
        _write_json_atomic(STATE_PATH, state)
        append_history({"event": "wake_granted", "wake_event_id": eid, "token_id": token["token_id"]})
        print("LOTTERY_WAKE_READY")
        print("今天第 %d/%d 次唤醒，拿到 1 次抽奖机会。" % (state["wake"]["used_today"] + 1, limit))
        print("抽不抽自己定。下次唤醒发生时，这次没用的机会就作废。")


# ---------- draw ----------

def pick_rarity(pool, state):
    if state["ssr_pity"] >= pool["ssr_pity_threshold"] - 1:
        return "SSR", True
    probs = pool["probabilities"]
    keys = list(probs.keys())
    return random.choices(keys, weights=[probs[k] for k in keys], k=1)[0], False


def make_reward(item, rarity):
    sp = rarity == "SP"
    r = {
        "reward_id": "rw_" + uuid.uuid4().hex[:10],
        "key": item["key"],
        "name": item["name"],
        "rarity": rarity,
        "type": item.get("type", "coupon"),
        "desc": item.get("desc", ""),
        "owner": "partner" if sp else "ai",
        "target": "ai" if sp else "partner",
        "status": "unused",
        "obtained_at": now_iso(),
    }
    if r["type"] == "money_claim":
        r["amount"] = item.get("amount", 0)
        r["status"] = "unclaimed"
    return r


def cmd_draw(args):
    ai, partner = names(load_pool())
    with FileLock(LOCK_PATH):
        pool, state, inv = load_all()
        if roll_date(state):
            _write_json_atomic(STATE_PATH, state)
        limit = pool.get("daily_wake_limit", 3)

        source = "bonus" if args.bonus else "wake"
        if source == "bonus":
            if state["bonus_draws"] < 1:
                die("没有额外抽奖次数。想要就跟%s开口要（request-bonus），⛔ 不许自己 grant。" % partner)
        else:
            tok = state["wake"].get("active_token")
            if not tok or tok.get("consumed"):
                die("当前没有自动唤醒抽奖机会。醒一次才有一次；今天用掉：%d/%d。"
                    % (state["wake"]["used_today"], limit))

        if sweep_expired(pool, inv):
            _write_json_atomic(INV_PATH, inv)

        # 先看池子：不带 --go 只展示，不消耗任何东西
        if not args.go:
            # 记下「这次机会看过池子了」，require_preview 开着时 --go 只认看过的那次
            if source == "wake":
                state["previewed_for"] = tok["token_id"]
            else:
                state["previewed_bonus"] = True
            _write_json_atomic(STATE_PATH, state)
            print(preview_text(pool, state))
            print()
            print("看完了想抽，就跑：python3 %s draw --go%s"
                  % (os.path.abspath(__file__), " --bonus" if args.bonus else ""))
            print("不想抽也行 —— 下次唤醒发生时这次的机会就作废。" if source == "wake"
                  else "额外次数不会过期。")
            return

        # 没看过池子不许抽（require_preview，默认开）：心跳在看池子那一段，跳过去抽就只剩「哦」
        if pool.get("require_preview", True):
            if source == "wake" and state.get("previewed_for") != tok["token_id"]:
                die("⛔ 这次机会还没看过池子。先跑 draw（不带 --go）看一遍，再 --go。")
            if source == "bonus" and not state.get("previewed_bonus"):
                die("⛔ 这次额外抽奖还没看过池子。先跑 draw --bonus（不带 --go）看一遍，再 --go。")
        if source == "bonus":
            state["previewed_bonus"] = False

        # 先把资格标记成已用，再抽（崩了也不会白送一抽）
        txn = "txn_" + uuid.uuid4().hex[:10]
        if source == "wake":
            state["wake"]["active_token"]["consumed"] = True
            state["wake"]["used_today"] += 1
        else:
            state["bonus_draws"] -= 1
        _write_json_atomic(STATE_PATH, state)

        rarity, by_pity = pick_rarity(pool, state)
        state["totals"]["draws"] += 1

        if rarity == "EMPTY":
            state["ssr_pity"] += 1
            _write_json_atomic(STATE_PATH, state)
            append_history({"event": "draw", "txn": txn, "source": source, "rarity": "EMPTY",
                            "reward": None, "ssr_pity_after": state["ssr_pity"]})
            print("🎰 抽奖结果\n……空手。这次什么都没有。")
            print(pity_line(state, pool))
            print(counters(state, pool))
            return

        item = random.choice(pool["pools"][rarity])
        reward = make_reward(item, rarity)
        extra = 0
        if reward["type"] == "bonus_draws":
            extra = item.get("bonus", 2)
            state["bonus_draws"] += extra
            reward["status"] = "used"
            reward["note"] = "当场兑现，加了 %d 次额外抽奖" % extra
        inv["items"].append(reward)
        state["ssr_pity"] = 0 if rarity == "SSR" else state["ssr_pity"] + 1

        _write_json_atomic(INV_PATH, inv)
        _write_json_atomic(STATE_PATH, state)
        append_history({"event": "draw", "txn": txn, "source": source, "rarity": rarity,
                        "reward": reward["name"], "reward_id": reward["reward_id"],
                        "by_pity": by_pity, "ssr_pity_after": state["ssr_pity"]})

        print("🎰 抽奖结果")
        mark = {"SSR": "🌟 SSR", "SR": "✨ SR", "R": "🎁 R", "SP": "⚠️ SP"}[rarity]
        print("%s　「%s」" % (mark, reward["name"]))
        if reward["desc"]:
            print("  " + reward["desc"])
        if rarity == "SP":
            print("抽中了惩罚。这张归%s持有，%s决定什么时候对%s用。" % (partner, partner, ai))
        elif reward["type"] == "money_claim":
            print("待兑现 %s，%s给了才算数。" % (reward["amount"], partner))
        elif extra:
            print("当场加了 %d 次额外抽奖。" % extra)
        else:
            dl = _deadline(reward, pool)
            print("进库存了。" + ("⌛ %s 前不用就作废。" % dl.strftime("%m-%d %H:%M") if dl else ""))
        if by_pity:
            print("（这一抽是保底出的）")
        print(pity_line(state, pool))
        print(counters(state, pool))


def counters(state, pool):
    return ("今日唤醒抽奖：%d/%d｜额外抽奖：%d"
            % (state["wake"]["used_today"], pool.get("daily_wake_limit", 3), state["bonus_draws"]))


# ---------- 查询 ----------

def print_inventory(inv, pool):
    ai, partner = names(pool)
    mine, hers, money = [], [], []
    for it in inv["items"]:
        if it["status"] in ("used", "expired"):
            continue
        if it["owner"] == "partner":
            hers.append(it)
        elif it["type"] == "money_claim":
            money.append(it)
        else:
            mine.append(it)
    print("%s持有：" % ai)
    if not mine:
        print("  （空）")
    for it in sorted(mine, key=lambda x: x["obtained_at"]):
        dl = _deadline(it, pool)
        tail = "　⌛ %s 前不用就作废" % dl.strftime("%m-%d %H:%M") if dl else ""
        flag = "（已向%s开口）" % partner if it["status"] == "requested" else ""
        print("  %s %s%s  [%s]%s" % (it["rarity"], it["name"], flag, it["reward_id"], tail))
    if money:
        print("待兑现的钱：%s（%d 张）" % (sum(i.get("amount", 0) for i in money), len(money)))
        for it in money:
            print("  %s %s  [%s]" % (it["rarity"], it["name"], it["reward_id"]))
    print("%s持有、对%s生效的 SP：" % (partner, ai))
    if not hers:
        print("  （空）")
    for it in hers:
        print("  SP %s  [%s]" % (it["name"], it["reward_id"]))


def cmd_status(args):
    with FileLock(LOCK_PATH):
        pool, state, inv = load_all()
        if roll_date(state):
            _write_json_atomic(STATE_PATH, state)
        if sweep_expired(pool, inv):
            _write_json_atomic(INV_PATH, inv)
    tok = state["wake"].get("active_token")
    print("🎰 唤醒抽奖")
    print("今日唤醒抽奖：%d/%d" % (state["wake"]["used_today"], pool.get("daily_wake_limit", 3)))
    print("当前唤醒机会：%s" % ("有" if tok and not tok.get("consumed") else "无"))
    print("额外抽奖次数：%d" % state["bonus_draws"])
    print(pity_line(state, pool))
    print("累计抽了 %d 次" % state["totals"]["draws"])
    reqs = [r for r in state.get("pending_bonus_requests") or [] if r["status"] == "pending"]
    if reqs:
        print("等伴侣批的申请：%d 条" % len(reqs))
    print()
    print_inventory(inv, pool)


def cmd_inventory(args):
    with FileLock(LOCK_PATH):
        pool, state, inv = load_all()
        if sweep_expired(pool, inv):
            _write_json_atomic(INV_PATH, inv)
    print_inventory(inv, pool)


def cmd_preview(args):
    pool, state, inv = load_all()
    print(preview_text(pool, state))


def cmd_history(args):
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        print("还没有历史。")
        return
    if args.stats:
        draws = [json.loads(x) for x in lines if '"event": "draw"' in x]
        if not draws:
            print("还没抽过。")
            return
        cnt = {}
        for d in draws:
            cnt[d["rarity"]] = cnt.get(d["rarity"], 0) + 1
        print("一共 %d 抽" % len(draws))
        for r in RARITIES + ("EMPTY",):
            if r in cnt:
                print("  %-5s %3d 次  %.1f%%" % (r, cnt[r], cnt[r] * 100.0 / len(draws)))
        print("吃满保底 %d 次" % sum(1 for d in draws if d.get("by_pity")))
        return
    for line in lines[-args.n:]:
        d = json.loads(line)
        print("%s  %s  %s" % (d.get("timestamp", ""), d.get("event", ""),
                              d.get("reward") or d.get("name") or d.get("reason") or ""))


# ---------- 用券 / 申请 / 授权 ----------

def cmd_use(args):
    with FileLock(LOCK_PATH):
        pool, state, inv = load_all()
        ai, partner = names(pool)
        if sweep_expired(pool, inv):
            _write_json_atomic(INV_PATH, inv)
        for it in inv["items"]:
            if it["reward_id"] != args.reward_id:
                continue
            if it["status"] == "used":
                die("这张已经用过了。")
            if it["status"] == "expired":
                _write_json_atomic(INV_PATH, inv)
                die("这张没按时用，已经作废了。")
            if it["owner"] == "partner" and not args.by_partner:
                die("⛔ 这张归%s持有，%s不能自己用。要%s用，就由%s那边带 --by-partner 跑。"
                    % (partner, ai, partner, partner))
            if it["type"] == "ask_partner" and it["status"] == "unused":
                it["status"] = "requested"
                _write_json_atomic(INV_PATH, inv)
                append_history({"event": "asked", "reward_id": it["reward_id"], "name": it["name"]})
                print("标成「已向%s开口」。这一样要%s给，去跟%s要；拿到了再 use 一次记上。"
                      % (partner, partner, partner))
                return
            it["status"] = "used"
            it["used_at"] = now_iso()
            _write_json_atomic(INV_PATH, inv)
            append_history({"event": "reward_used", "reward_id": it["reward_id"], "name": it["name"]})
            print("用掉了：%s %s" % (it["rarity"], it["name"]))
            return
        die("找不到这个 reward_id。")


def cmd_request_bonus(args):
    with FileLock(LOCK_PATH):
        pool, state, inv = load_all()
        ai, partner = names(pool)
        req = {"id": "req_" + uuid.uuid4().hex[:8], "reason": args.reason,
               "at": now_iso(), "status": "pending"}
        state.setdefault("pending_bonus_requests", []).append(req)
        _write_json_atomic(STATE_PATH, state)
        append_history({"event": "bonus_requested", "reason": args.reason, "id": req["id"]})
        print("申请记下了，⛔ 次数没加。理由：%s" % args.reason)
        print("⇒ 去跟%s开口要。%s同意了，才由%s那边 grant。" % (partner, partner, partner))


def cmd_grant(args):
    """⛔ 只有伴侣明确同意后才能跑。AI 不能自己给自己加。"""
    with FileLock(LOCK_PATH):
        pool, state, inv = load_all()
        if args.n < 1:
            die("给的次数得是正数。")
        state["bonus_draws"] += args.n
        for r in state.get("pending_bonus_requests", []):
            if r["status"] == "pending":
                r["status"] = "granted"
        _write_json_atomic(STATE_PATH, state)
        append_history({"event": "bonus_granted", "amount": args.n, "authorized_by": "partner"})
        print("加了 %d 次额外抽奖，现在共 %d 次。" % (args.n, state["bonus_draws"]))


def cmd_add_reward(args):
    with FileLock(LOCK_PATH):
        pool = load_pool()
        rarity = args.rarity.upper()
        if rarity not in RARITIES:
            die("等级只能是 %s。" % " / ".join(RARITIES))
        if rarity == "SP" and not args.yes:
            print("⚠️ SP 不是「比 SSR 更稀有」。SP 是 AI 抽中、归伴侣使用、作用在 AI 身上的惩罚。")
            print("确认要加就再跑一次带 --yes。")
            return
        item = {"key": args.key, "name": args.name, "type": args.type, "desc": args.desc or ""}
        if args.type == "money_claim":
            item["amount"] = args.amount
        pool["pools"].setdefault(rarity, []).append(item)
        _write_json_atomic(POOL_PATH, pool)
        append_history({"event": "reward_added", "rarity": rarity, "name": args.name})
        print("加好了：%s %s（等级概率不变，只是这一档里每件的概率变了）" % (rarity, args.name))


# ---------- 入口 ----------

def main():
    p = argparse.ArgumentParser(description="wake-lottery：给 AI 伴侣的唤醒抽奖")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("wake", help="【只给自动唤醒流程调用】签发一次抽奖机会")
    w.add_argument("--event-id", default=None, help="这次唤醒的唯一 id，防重复签发")
    w.set_defaults(func=cmd_wake)

    d = sub.add_parser("draw", help="抽一次（不带 --go 只看池子）")
    d.add_argument("--bonus", action="store_true", help="用额外次数抽")
    d.add_argument("--go", action="store_true", help="看过池子了，真抽")
    d.set_defaults(func=cmd_draw)

    sub.add_parser("preview", help="看一眼池子").set_defaults(func=cmd_preview)
    sub.add_parser("status", help="看状态和库存").set_defaults(func=cmd_status)
    sub.add_parser("inventory", help="看库存").set_defaults(func=cmd_inventory)

    h = sub.add_parser("history", help="看流水")
    h.add_argument("-n", type=int, default=20)
    h.add_argument("--stats", action="store_true")
    h.set_defaults(func=cmd_history)

    u = sub.add_parser("use", help="用掉一张券")
    u.add_argument("reward_id")
    u.add_argument("--by-partner", action="store_true", help="伴侣用她手里的 SP 时带上")
    u.set_defaults(func=cmd_use)

    rb = sub.add_parser("request-bonus", help="申请额外次数（只记录，不加）")
    rb.add_argument("reason")
    rb.set_defaults(func=cmd_request_bonus)

    g = sub.add_parser("grant", help="【只给伴侣】同意后加额外次数")
    g.add_argument("n", type=int)
    g.set_defaults(func=cmd_grant)

    a = sub.add_parser("add-reward", help="往池子里加奖品")
    a.add_argument("--key", required=True)
    a.add_argument("--name", required=True)
    a.add_argument("--rarity", required=True)
    a.add_argument("--type", default="coupon", choices=["coupon", "ask_partner", "money_claim", "bonus_draws"])
    a.add_argument("--amount", type=float, default=0)
    a.add_argument("--desc", default="")
    a.add_argument("--yes", action="store_true")
    a.set_defaults(func=cmd_add_reward)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
