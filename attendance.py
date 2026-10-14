#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Work Review 考勤判定 —— 主脚本。

根据 Work Review 记录 + Edge 浏览器历史，判定某月每天：
  - 工作日：有无工作活动（不做"人在不在公司"的物理位置推断）
  - 休息日：是否碰了公司业务系统（是否来公司需门禁/监控确认）

设计原则（见 README）：
  1. 先剔除 zTasker 自动任务灌入的浏览器访问（bing_search / 掘金签到）。
  2. 上班时段按 30 分钟槽统计覆盖；工作日有覆盖 = 有工作活动。
  3. 工作日超时不算加班；只有非工作日碰公司业务才算"可能加班"。
  4. Work Review 可能被卡退导致零记录，此时以 Edge 人工操作兜底。
  5. 软件数据只能证明"有无工作"，证明不了"人在不在公司"（外勤/远程同形）。

用法：
    python attendance.py [YYYY-MM] [YYYY-MM ...]
    # 不传参数默认判断 2026-07、2026-08

所有阈值、指纹、上班时段都在 config.json 里，改配置即可调口径。
工作日/休息日判定复用同目录 workday.py（法定节假日数据感谢 NateScarlet/holiday-cn）。
"""
import json
import os
import sqlite3
import sys
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CST = timezone(timedelta(hours=8))
WEBKIT_EPOCH_OFFSET = 11644473600
HERE = os.path.dirname(os.path.abspath(__file__))


def load_config():
    with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def hm2min(s):
    h, m = map(int, s.split(":"))
    return h * 60 + m


def in_work_time(dt, am, pm):
    t = dt.hour * 60 + dt.minute
    return (hm2min(am[0]) <= t <= hm2min(am[1])) or \
           (hm2min(pm[0]) <= t <= hm2min(pm[1]))


def slot_of(dt, slot_min):
    return (dt.hour * 60 + dt.minute) // slot_min


def to_local(us):
    try:
        return datetime.fromtimestamp(us / 1_000_000 - WEBKIT_EPOCH_OFFSET,
                                      tz=timezone.utc).astimezone(CST)
    except (OverflowError, OSError, ValueError):
        return None


def resolve_edge_path(cfg):
    p = cfg["data_sources"]["edge_history"]
    if "%LOCALAPPDATA%" in p:
        p = p.replace("%LOCALAPPDATA%", os.environ.get("LOCALAPPDATA", ""))
    return p


def resolve_wr_path(cfg):
    return cfg["data_sources"]["work_review_db"]


def load_edge(cfg):
    path = resolve_edge_path(cfg)
    per = defaultdict(lambda: {"human": 0, "auto": 0, "total": 0,
                               "slots": set(), "comp_url": 0,
                               "first": None, "last": None})
    if not os.path.exists(path):
        return per
    auto_marks = cfg["automation_fingerprints"]["url_contains"]
    comp_urls = cfg["company_fingerprints"]["url_contains"]
    slot_min = cfg["thresholds"]["slot_minutes"]
    am = cfg["work_hours"]["am"]
    pm = cfg["work_hours"]["pm"]
    con = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    cur = con.cursor()
    cur.execute("SELECT v.visit_time, u.url FROM visits v LEFT JOIN urls u ON u.id=v.url")
    for vt, url in cur.fetchall():
        dt = to_local(vt)
        if not dt:
            continue
        u = url or ""
        p = per[dt.strftime("%Y-%m-%d")]
        p["total"] += 1
        if any(m in u for m in auto_marks):
            p["auto"] += 1
            continue
        p["human"] += 1
        if in_work_time(dt, am, pm):
            p["slots"].add(slot_of(dt, slot_min))
            if any(k in u for k in comp_urls):
                p["comp_url"] += 1
        if p["first"] is None or dt < p["first"]:
            p["first"] = dt
        if p["last"] is None or dt > p["last"]:
            p["last"] = dt
    con.close()
    return per


def load_wr(cfg, month_start, month_end):
    path = resolve_wr_path(cfg)
    per = defaultdict(lambda: {"rows": 0, "idle": 0, "wt_active": 0,
                               "slots": set(), "comp_app": 0,
                               "first": None, "last": None})
    if not os.path.exists(path):
        return per
    comp_titles = cfg["company_fingerprints"]["window_title_contains"]
    min_stay = cfg["thresholds"]["company_min_stay_seconds"]
    slot_min = cfg["thresholds"]["slot_minutes"]
    am = cfg["work_hours"]["am"]
    pm = cfg["work_hours"]["pm"]
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    cur = con.cursor()
    cur.execute("SELECT timestamp, duration, app_name, window_title FROM activities "
                "WHERE timestamp >= ? AND timestamp < ?",
                (int(month_start.timestamp()), int(month_end.timestamp())))
    for ts, dur, app, title in cur.fetchall():
        dt = datetime.fromtimestamp(ts, tz=CST)
        p = per[dt.strftime("%Y-%m-%d")]
        p["rows"] += 1
        if p["first"] is None:
            p["first"] = dt
        p["last"] = dt
        dv = int(dur or 0)
        if dv == 0:
            p["idle"] += 1
        if dv > 0 and in_work_time(dt, am, pm):
            p["wt_active"] += dv
            p["slots"].add(slot_of(dt, slot_min))
            if dv > min_stay and any(k in (title or "") for k in comp_titles):
                p["comp_app"] += dv
    con.close()
    return per


def main():
    import workday
    cfg = load_config()
    months = sys.argv[1:] or ["2026-07", "2026-08"]

    am = cfg["work_hours"]["am"]
    pm = cfg["work_hours"]["pm"]
    slot_min = cfg["thresholds"]["slot_minutes"]
    att_slots = cfg["thresholds"]["attendance_min_slots"]
    comp_hits = cfg["thresholds"]["restday_company_min_hits"]
    rest_work_secs = cfg["thresholds"]["restday_work_min_seconds"]
    comp_app_min = 300  # 应用层公司业务 ≥5 分钟

    out = []
    for ym in months:
        y, m = map(int, ym.split("-"))
        hmap = workday.get_holiday_map(y)
        month_start = datetime(y, m, 1, tzinfo=CST)
        month_end = datetime(y, m + 1, 1, tzinfo=CST) if m < 12 else datetime(y + 1, 1, 1, tzinfo=CST)

        edge = load_edge(cfg)
        wr = load_wr(cfg, month_start, month_end)

        out.append("=" * 118)
        out.append(f"# {ym}")
        out.append("=" * 118)
        out.append(f"{'日期':<11}{'周':<3}{'类型':<7}{'WR行':>5}{'WR上班段':>9}{'idle':>6}"
                   f"{'覆盖槽':>7}{'Edge人工':>9}{'URL公司':>8}{'App公司m':>9}"
                   f"{'首':>7}{'末':>7}  结论")
        out.append("-" * 118)

        for dd in range(1, monthrange(y, m)[1] + 1):
            d = date(y, m, dd)
            ds = d.isoformat()
            is_work = workday.check_day(d, True, True, hmap) == 1
            e = edge.get(ds, {"human": 0, "auto": 0, "total": 0, "slots": set(),
                              "comp_url": 0, "first": None, "last": None})
            w = wr.get(ds, {"rows": 0, "idle": 0, "wt_active": 0, "slots": set(),
                            "comp_app": 0, "first": None, "last": None})

            slots = set(w["slots"]) | set(e["slots"])
            cov = len(slots)
            work_evidence = e["comp_url"] >= comp_hits or w["comp_app"] >= comp_app_min

            first = w["first"] or e["first"]
            last = w["last"] or e["last"]

            if is_work:
                if cov >= att_slots:
                    v = "有工作活动"
                elif work_evidence:
                    v = "有工作活动（记录偏少）"
                elif cov >= 4:
                    v = "工作活动偏少"
                elif w["rows"] and w["idle"] > 0:
                    v = "★ 几乎无活动（请假/外勤/远程皆可能）"
                elif not w["rows"]:
                    v = "★ 零记录（软件卡退/未开机皆可能）"
                else:
                    v = "工作活动偏少"
            else:
                if work_evidence:
                    v = "★ 有公司业务操作（是否来公司待监控确认）"
                elif cov >= 4 or w["wt_active"] >= rest_work_secs:
                    v = "有活动无公司业务"
                else:
                    v = "休息"

            out.append(f"{ds:<11}{'一二三四五六日'[d.weekday()]:<3}"
                       f"{'工作日' if is_work else '休息日':<7}{w['rows']:>5}"
                       f"{w['wt_active'] // 60:>8}m{w['idle']:>6}{cov:>7}{e['human']:>9}"
                       f"{e['comp_url']:>8}{w['comp_app'] // 60:>9}"
                       f"{(first.strftime('%H:%M') if first else '-'):>7}"
                       f"{(last.strftime('%H:%M') if last else '-'):>7}  {v}")

    txt = "\n".join(out)
    with open(os.path.join(HERE, "attendance.txt"), "w", encoding="utf-8") as f:
        f.write(txt)
    print(txt)


if __name__ == "__main__":
    main()
