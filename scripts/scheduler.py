#!/usr/bin/env python3
"""FSRS-4.5 调度内核 + 概念节点库 + 留存率指标。

三条硬规则（为什么需要这个文件）:

1. **日期和间隔由确定性代码算，绝不交给模型心算。**
   模型算日期会错，且不可复现。这里所有间隔都是公式输出。

2. **每个概念一条独立遗忘曲线。**
   旧版把 D+1/D+3/D+7/D+14 写死给所有概念；难概念和一眼会的概念不该同节奏。
   现在每个节点独立维护 stability(记忆稳定度, 天) 与 difficulty(难度, 1-10)。

3. **指标只报测得的数。**
   样本不足就报 unmeasured，不用"应该没问题"填补。

参数来源: open-spaced-repetition/fsrs4anki 的 FSRS-4.5 默认参数（17 个 w）。
算法本身是公开的，这里是纯标准库重实现，无外部依赖。
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from pathlib import Path

# ---------- FSRS-4.5 参数与公式 ----------

# w[0..3]  首次评分 G=1..4 对应的初始稳定度 S0
# w[4]     初始难度 D0 的基准
# w[5]     D0 对评分的敏感度
# w[6]     难度的更新步长
# w[7]     难度均值回归强度
# w[8..10] 成功回忆时的稳定度增长
# w[11..14] 遗忘时的稳定度衰减
# w[15..16] Hard / Easy 的修正系数
W = (
    0.4872, 1.4003, 3.7145, 13.8206,
    5.1618, 1.2298, 0.8975, 0.031,
    1.6474, 0.1367, 1.0461,
    2.1072, 0.0793, 0.3246, 1.587,
    0.2272, 2.8755,
)

DECAY = -0.5
FACTOR = 19.0 / 81.0
DEFAULT_RETENTION = 0.90
MIN_INTERVAL = 1          # 天
MAX_INTERVAL = 365        # 天
MIN_STABILITY = 0.1
MAX_STABILITY = 36500.0

GRADE_LABEL = {1: "错", 2: "勉强", 3: "对", 4: "轻松"}
LABEL_GRADE = {
    "错": 1, "不会": 1, "again": 1, "fail": 1, "x": 1, "✗": 1, "×": 1,
    "勉强": 2, "卡": 2, "hard": 2, "半对": 2,
    "对": 3, "正确": 3, "good": 3, "✓": 3, "√": 3, "是": 3, "通过": 3,
    "轻松": 4, "秒答": 4, "easy": 4,
}


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def forgetting_curve(elapsed_days: float, stability: float) -> float:
    """t 天后的可回忆概率 R(t,S) = (1 + FACTOR*t/S)^DECAY。"""
    if stability <= 0:
        return 0.0
    if elapsed_days <= 0:
        return 1.0
    return (1.0 + FACTOR * elapsed_days / stability) ** DECAY


def init_stability(g: int) -> float:
    return max(W[min(max(g, 1), 4) - 1], MIN_STABILITY)


def init_difficulty(g: int) -> float:
    return clamp(W[4] - (g - 3) * W[5], 1.0, 10.0)


def next_difficulty(d: float, g: int) -> float:
    """难度向均值回归，再按评分调整。"""
    moved = d - W[6] * (g - 3)
    return clamp(W[7] * W[4] + (1 - W[7]) * moved, 1.0, 10.0)


def next_recall_stability(d: float, s: float, r: float, g: int) -> float:
    hard = W[15] if g == 2 else 1.0
    easy = W[16] if g == 4 else 1.0
    return s * (
        1.0
        + math.exp(W[8])
        * (11.0 - d)
        * (s ** (-W[9]))
        * (math.exp((1.0 - r) * W[10]) - 1.0)
        * hard
        * easy
    )


def next_forget_stability(d: float, s: float, r: float) -> float:
    return (
        W[11]
        * (d ** (-W[12]))
        * ((s + 1.0) ** W[13] - 1.0)
        * math.exp((1.0 - r) * W[14])
    )


def interval_for(stability: float, retention: float = DEFAULT_RETENTION) -> int:
    """给定稳定度，算出让留存概率落在 retention 上的间隔天数。"""
    retention = clamp(retention, 0.70, 0.99)
    raw = stability / FACTOR * (retention ** (1.0 / DECAY) - 1.0)
    return int(clamp(round(raw), MIN_INTERVAL, MAX_INTERVAL))


def parse_grade(value) -> int | None:
    """接受 1-4 或中文/英文标签。"""
    if isinstance(value, int):
        return value if 1 <= value <= 4 else None
    s = str(value).strip().lower()
    if s.isdigit() and 1 <= int(s) <= 4:
        return int(s)
    return LABEL_GRADE.get(s)


# ---------- 节点库 ----------

def today_iso() -> str:
    return dt.date.today().isoformat()


def _parse_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def norm_key(s: str) -> str:
    """概念名归一：全半角、大小写、空格与连接符不该把同一个概念拆成两个。

    「JS 闭包」「js闭包」「ＪＳ闭包」在判定连错时要算同一个 —— 否则同一概念换种写法
    就绕过了「连错 2 轮」的拦截，而那正是最需要拦住的情况。
    """
    import unicodedata
    s = unicodedata.normalize("NFKC", (s or "")).strip().lower()
    return re.sub(r"[\s·・\-_—–/]+", "", s)


def make_id(name: str, taken: set) -> str:
    """概念名 → 稳定 id（n1/n2...），与名称解耦，改名不丢历史。"""
    i = 1
    while f"n{i}" in taken:
        i += 1
    return f"n{i}"


class NodeStore:
    """概念节点库，落在 <会话>/.learning.json。

    nodes[id] = {id, name, prereq[], S, D, reps, lapses, last, due, retired, events[]}
    （按 id 存，按 name 或 id 取；name 可改，id 不变）
    """

    def __init__(self, session: Path):
        self.session = Path(session)
        self.path = self.session / ".learning.json"
        self.data = {
            "version": 2,
            "request_retention": DEFAULT_RETENTION,
            "nodes": {},
            "reviews": 0,
        }
        self._load()

    # -- 持久化 --

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(raw, dict) and isinstance(raw.get("nodes"), dict):
            self.data.update(raw)
            self.data.setdefault("request_retention", DEFAULT_RETENTION)
            self.data.setdefault("reviews", 0)

    def save(self) -> None:
        self.session.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # -- 查询 --

    @property
    def nodes(self) -> dict:
        return self.data["nodes"]

    def get(self, key: str) -> dict | None:
        """按 id 或名称取节点；先精确，再唯一子串，避免「闭包」误命中「闭包编译器」。"""
        key = (key or "").strip()
        if not key:
            return None
        if key in self.nodes:
            return self.nodes[key]
        for n in self.nodes.values():
            if n["name"] == key:
                return n
        k = norm_key(key)
        hits = [n for n in self.nodes.values()
                if k and (norm_key(n["name"]) == k
                          or k in [norm_key(a) for a in n.get("aliases") or []])]
        if len(hits) == 1:
            return hits[0]
        hits = [n for n in self.nodes.values() if key in n["name"]]
        return hits[0] if len(hits) == 1 else None

    def find_many(self, key: str) -> list:
        key = (key or "").strip()
        return [n for n in self.nodes.values() if key and key in n["name"]]

    def live(self) -> list:
        return [n for n in self.nodes.values() if not n.get("retired")]

    # -- 写入 --

    def sync(self, concepts: list) -> tuple:
        """按 [(名称, 前置名称)] 同步节点。返回 (新增, 保留, 失联)。

        已存在节点只更新 prereq，不动 S/D/历史。
        失联 = 节点还在库里但概念表里没了 —— 只报告，不删除（历史不能凭空消失）。
        """
        want = {}
        for item in concepts:
            name = (item[0] if isinstance(item, (list, tuple)) else item) or ""
            name = str(name).strip()
            if name:
                prereq = item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else []
                want[name] = [str(p).strip() for p in prereq if str(p).strip()]

        added = []
        for name, prereq in want.items():
            node = self.get(name)
            if node and self.same_identity(node, name):
                # 归一或别名指向同一个概念（「JS 闭包」vs「js闭包」），别再建一个
                node["prereq"] = prereq
                continue
            if node and node["name"] == name:
                node["prereq"] = prereq
                continue
            nid = make_id(name, set(self.nodes))
            self.nodes[nid] = {
                "id": nid,
                "name": name,
                "prereq": prereq,
                "S": 0.0,
                "D": 0.0,
                "reps": 0,
                "lapses": 0,
                "last": None,
                "due": None,
                "retired": False,
                "events": [],
            }
            added.append(self.nodes[nid])

        # 前置名称 → id
        for node in self.nodes.values():
            node["prereq_ids"] = [
                n["id"] for n in self.nodes.values()
                if n["name"] in node.get("prereq", [])
            ]

        lost = [n["name"] for n in self.nodes.values() if n["name"] not in want]
        return added, len(want) - len(added), lost

    def grade(self, key: str, g: int, confidence: int | None = None,
              on: dt.date | None = None, note: str = "",
              evidence: str = "") -> dict:
        """记录一次评分，更新 S/D/due。返回更新后的节点（含本次计算结果）。

        evidence 是用户的原话片段。没有它，这条记录只是模型的自我印象；
        有它，才叫收据 —— stats 会统计无凭证收据的比例。
        """
        node = self.get(key)
        if node is None:
            raise KeyError(key)
        on = on or dt.date.today()
        last = _parse_date(node.get("last"))
        r_now = None

        if node["reps"] == 0 or last is None:
            s = init_stability(g)
            d = init_difficulty(g)
        else:
            elapsed = max((on - last).days, 0)
            r_now = forgetting_curve(elapsed, node["S"])
            d = next_difficulty(node["D"], g)
            if g == 1:
                s = next_forget_stability(node["D"], node["S"], r_now)
                node["lapses"] = node.get("lapses", 0) + 1
            else:
                s = next_recall_stability(node["D"], node["S"], r_now, g)

        s = clamp(s, MIN_STABILITY, MAX_STABILITY)
        interval = interval_for(s, self.data.get("request_retention", DEFAULT_RETENTION))

        node["S"] = round(s, 4)
        node["D"] = round(d, 4)
        node["reps"] = node.get("reps", 0) + 1
        node["last"] = on.isoformat()
        node["due"] = (on + dt.timedelta(days=interval)).isoformat()
        node["events"].append({
            "at": on.isoformat(),
            "grade": g,
            "conf": confidence,
            "R": None if r_now is None else round(r_now, 4),
            "S": node["S"],
            "interval": interval,
            "note": note,
            "evidence": evidence[:500],
        })
        self.data["reviews"] = self.data.get("reviews", 0) + 1
        return node

    @staticmethod
    def same_identity(node: dict, name: str) -> bool:
        """这个名字是不是就是那个节点本身（含归一与别名）。"""
        k = norm_key(name)
        return bool(k) and (k == norm_key(node["name"])
                            or k in [norm_key(a) for a in node.get("aliases") or []])

    def add_alias(self, key: str, aliases: list) -> dict | None:
        """给概念登记别名。语义相同、字面不同的写法靠这个统一。"""
        node = self.get(key)
        if node is None:
            return None
        cur = node.setdefault("aliases", [])
        for a in aliases:
            a = (a or "").strip()
            if a and a not in cur and norm_key(a) != norm_key(node["name"]):
                cur.append(a)
        return node

    def retire(self, key: str, on: bool = True) -> dict | None:
        node = self.get(key)
        if node:
            node["retired"] = on
        return node

    # -- 队列 --

    def due_list(self, on: dt.date | None = None, include_new: bool = False) -> list:
        """到期队列，按「当前遗忘了多少」降序 —— 忘得最多的先救。"""
        on = on or dt.date.today()
        out = []
        for node in self.live():
            if node["reps"] == 0:
                if include_new:
                    out.append({**node, "R": None, "overdue": 0})
                continue
            due = _parse_date(node.get("due"))
            if due is None or due > on:
                continue
            elapsed = max((on - _parse_date(node["last"])).days, 0) if node.get("last") else 0
            r = forgetting_curve(elapsed, node["S"])
            out.append({**node, "R": round(r, 4), "overdue": (on - due).days})
        # 前置也到期时，先把地基补上：后置概念现在复习，等于在半塌的地基上砌墙
        names = {n["name"] for n in out}
        for n in out:
            n["blocked_by"] = [p for p in n.get("prereq", []) if p in names]
        out.sort(key=lambda n: (bool(n["blocked_by"]), n["R"], -n["overdue"]))
        return out

    def frontier(self) -> list:
        """可学的下一个概念：还没编码，且前置都已编码。"""
        ready = []
        for node in self.live():
            if node["reps"] > 0:
                continue
            pre = node.get("prereq_ids", [])
            if all(self.nodes.get(p, {}).get("reps", 0) > 0 for p in pre):
                ready.append(node)
        return ready

    # -- 指标 --

    def measured_retention(self, on: dt.date | None = None) -> dict:
        """实测留存率：每个节点「首次编码」到「下一次复习」的间隔，按 7/30 天分桶。

        只算真实发生过的复习事件，不预测。分母含「已到期但没复习」的节点（记为 unmeasured）。
        """
        on = on or dt.date.today()
        buckets = {"d7": [0, 0], "d30": [0, 0], "d90": [0, 0]}  # [成功, 总数]
        for node in self.live():
            ev = node.get("events", [])
            if len(ev) < 2:
                continue
            first = _parse_date(ev[0]["at"])
            if first is None:
                continue
            gaps = []
            prev = first
            for e in ev[1:]:
                cur = _parse_date(e["at"])
                if cur is None:
                    continue
                gaps.append(((cur - prev).days, e.get("grade", 1)))
                prev = cur
            for days, g in gaps:
                if days < 1:
                    continue  # 同一天内重复评分不是留存测试，只会污染分母
                key = "d7" if days <= 7 else "d30" if days <= 30 else "d90"
                buckets[key][1] += 1
                if g >= 3:
                    buckets[key][0] += 1

        unmeasured = sum(
            1 for n in self.live()
            if n["reps"] > 0 and _parse_date(n.get("due")) and _parse_date(n["due"]) <= on
        )
        return {
            "buckets": buckets,
            "unmeasured": unmeasured,
            "measured": sum(v[1] for v in buckets.values()),
        }

    def calibration(self) -> dict:
        """信心 1-5 vs 实际通过率。差值 > 0 说明你高估自己。"""
        rows = []
        for node in self.live():
            for e in node.get("events", []):
                if e.get("conf") and e.get("R") is not None:
                    rows.append((int(e["conf"]), e.get("grade", 1) >= 3))
        if not rows:
            return {"n": 0, "buckets": [], "bias": None, "brier": None}

        buckets = []
        for c in range(1, 6):
            sub = [ok for conf, ok in rows if conf == c]
            if sub:
                buckets.append({
                    "conf": c,
                    "n": len(sub),
                    "hit": round(sum(sub) / len(sub), 3),
                    "expect": round(0.2 * c, 2),
                })
        bias = sum((0.2 * c) - (1.0 if ok else 0.0) for c, ok in rows) / len(rows)
        brier = sum((0.2 * c - (1.0 if ok else 0.0)) ** 2 for c, ok in rows) / len(rows)
        return {
            "n": len(rows),
            "buckets": buckets,
            "bias": round(bias, 3),
            "brier": round(brier, 3),
        }

    def lapse_board(self, limit: int = 5) -> list:
        rows = [(n["name"], n.get("lapses", 0), n["reps"], n["S"])
                for n in self.live() if n.get("lapses", 0) > 0]
        rows.sort(key=lambda r: (-r[1], r[3]))
        return rows[:limit]


# ---------- 从 markdown 概念表抽取 ----------

SEP_RE = re.compile(r"\|[\s:\-|]+\|")
EMPTY = {"", "______", "---", "…", "...", "-", "*", "待填", "TBD", "　", "/"}


def _cells(line: str) -> list:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_concepts(block: str) -> list:
    """概念表 → [(名称, [前置名称])]。表头含「概念」列即认；「前置」列可空。"""
    lines = [ln.strip() for ln in block.splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2:
        return []

    header = _cells(lines[0])
    i_name = i_pre = None
    for i, h in enumerate(header):
        if i_name is None and "概念" in h:
            i_name = i
        if i_pre is None and ("前置" in h or "依赖" in h):
            i_pre = i
    if i_name is None:
        return []

    out = []
    for ln in lines[2:]:
        if SEP_RE.fullmatch(ln):
            continue
        cs = _cells(ln)
        if i_name >= len(cs):
            continue
        name = cs[i_name].strip()
        if name in EMPTY or name.startswith("{"):
            continue
        pre = []
        if i_pre is not None and i_pre < len(cs):
            pre = [p.strip() for p in re.split(r"[,，、;；/]", cs[i_pre])
                   if p.strip() and p.strip() not in EMPTY]
        out.append((name, pre))
    return out


def workload_minutes(n: int) -> int:
    """粗略估算：一道应用题含回忆 + 判分约 1.5 分钟。"""
    return int(round(n * 1.5))


def fmt_due(node: dict) -> str:
    if node["reps"] == 0:
        return "未编码"
    s = f"S={node['S']:.1f}d"
    if node.get("due"):
        s += f" 到期 {node['due']}"
    return s
