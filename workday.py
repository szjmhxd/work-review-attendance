#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工作日判断（本地日历纯计算 + 今年节假日数据远程拉取一次后缓存本地）

用法:
    python workday.py [YYYY-MM-DD] [--no-holiday] [--no-weekend]

参数:
    YYYY-MM-DD      可选，要判断的日期；不传则判断今天
    --no-holiday    不跳过法定节假日（法定节假日也按普通日期处理）
    --no-weekend    不跳过周末（周六周日也视为工作日）

返回:
    1 = 工作日（含调休上班的周末）
    2 = 节假日 / 休息日（法定节假日休息 或 周末）
    结果同时打印到标准输出并作为进程退出码，便于 zTasker 按 errorlevel 判断。

判定优先级:
    1. 节假日数据中 isOffDay=false → 调休上班 → 工作日（最高优先级）
    2. 节假日数据中 isOffDay=true 且未指定 --no-holiday → 休息日
    3. 周六/周日 且未指定 --no-weekend → 休息日
    4. 其余 → 工作日

数据策略:
    - 星期几由 datetime 纯本地计算，永远不需要网络。
    - 法定节假日/调休数据来源于开源项目 NateScarlet/holiday-cn（国务院节假日安排镜像），
      仅在本地无今年缓存时联网拉取一次，缓存为脚本同目录下 holiday_<年份>.json。
    - 只计算今年、只获取今年；之后每次调用只读本地缓存。
    - 联网失败只输出警告到 stderr，不阻断运行，自动退化为“只认周末”。
"""

import sys
import json
import argparse
import urllib.request
from datetime import date, datetime
from pathlib import Path

# 节假日数据源（NateScarlet/holiday-cn，国务院节假日安排镜像）
HOLIDAY_URL = "https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json"

# 脚本所在目录（节假日缓存文件也放在这里）
BASE_DIR = Path(__file__).resolve().parent

# 联网超时时间（秒）
HTTP_TIMEOUT = 10


def warn(message):
    """警告信息输出到 stderr，避免污染 stdout 上的 1/2 结果"""
    print(f"[工作日判断][警告] {message}", file=sys.stderr)


def get_holiday_cache_path(year):
    """获取某年份节假日数据的本地缓存文件路径"""
    return BASE_DIR / f"holiday_{year}.json"


def load_local_holiday(year):
    """
    读取本地缓存的今年节假日数据。
    返回 { "YYYY-MM-DD": True(休息)/False(调休上班) }；
    无缓存文件或文件损坏时返回 None。
    """
    cache_path = get_holiday_cache_path(year)
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return {item["date"]: bool(item["isOffDay"]) for item in data["days"]}
    except Exception as exc:  # 缓存损坏时按无缓存处理，后续会重新拉取
        warn(f"本地节假日数据读取失败: {exc}")
        return None


def fetch_holiday(year):
    """
    联网拉取今年的节假日数据并写入本地缓存文件。
    成功返回日期映射字典；任何失败都只警告并返回 None（退化为只认周末）。
    """
    url = HOLIDAY_URL.format(year=year)
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 workday-checker"}
        )
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))

        # 原始 JSON 原样落盘，只缓存今年的数据
        get_holiday_cache_path(year).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {item["date"]: bool(item["isOffDay"]) for item in data["days"]}
    except Exception as exc:
        warn(f"获取 {year} 年节假日数据失败: {exc}（本次退化为只按周末判断）")
        return None


def get_holiday_map(year):
    """
    获取今年的节假日映射：
    每次调用先查本地缓存，有就直接用；没有才联网获取（只获取今年）。
    """
    holiday_map = load_local_holiday(year)
    if holiday_map is not None:
        return holiday_map
    return fetch_holiday(year)


def check_day(target, skip_holiday, skip_weekend, holiday_map):
    """
    判断目标日期是工作日还是休息日。
    :param target: date 对象
    :param skip_holiday: 是否跳过法定节假日（流程图 skipHoliday）
    :param skip_weekend: 是否跳过周末（流程图 skipWeekend）
    :param holiday_map: 今年节假日日期映射，None 表示无数据
    :return: 1=工作日，2=休息日
    """
    date_str = target.strftime("%Y-%m-%d")
    # None=节假日数据里没有这一天；True=节假日休息；False=调休上班
    is_off_day = None if holiday_map is None else holiday_map.get(date_str)

    # 1) 调休上班（即使是周末也要上班）→ 工作日，优先级最高
    if is_off_day is False:
        return 1

    # 2) 法定节假日休息，且设置了跳过节假日 → 休息日
    if is_off_day is True and skip_holiday:
        return 2

    # 3) 普通周末（weekday: 5=周六, 6=周日），且设置了跳过周末 → 休息日
    if skip_weekend and target.weekday() >= 5:
        return 2

    # 4) 其余情况 → 工作日
    return 1


def parse_args(argv):
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="工作日判断：返回 1=工作日 2=节假日/休息日")
    parser.add_argument(
        "date",
        nargs="?",
        type=lambda value: datetime.strptime(value, "%Y-%m-%d").date(),
        help="要判断的日期，格式 YYYY-MM-DD；不传则判断今天",
    )
    parser.add_argument(
        "--no-holiday",
        action="store_true",
        help="不跳过法定节假日",
    )
    parser.add_argument(
        "--no-weekend",
        action="store_true",
        help="不跳过周末",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    # 不传日期则只计算今天（只计算今年）
    target = args.date if args.date is not None else date.today()

    # 每次调用先判断本地是否已有今年数据，有则用缓存，无则联网获取今年数据
    holiday_map = get_holiday_map(target.year)

    result = check_day(
        target=target,
        skip_holiday=not args.no_holiday,
        skip_weekend=not args.no_weekend,
        holiday_map=holiday_map,
    )

    # 输出到 stdout 并作为退出码返回（1=工作日，2=节假日/休息日）
    print(result)
    return result


if __name__ == "__main__":
    sys.exit(main())
