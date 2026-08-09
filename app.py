import os
import re
from datetime import datetime, timezone, timedelta

import yaml
from flask import Flask, render_template, request
from pymongo import MongoClient

app = Flask(__name__)

# 详情页额外字段 → 显示标签映射
EXTRA_LABELS = {
    "reply_count": "💬",
    "author": "👤",
    "desc": "📝",
    "summary": "📝",
}

# 爬虫标签映射：collection 名 → display 名 + 分类
LABELS_PATH = os.environ.get("LABELS_PATH", "/getweb-labels/labels.yaml")
_labels_cache = None


def _load_labels():
    """加载 labels.yaml，缓存避免重复读盘。"""
    global _labels_cache
    if _labels_cache is not None:
        return _labels_cache
    if os.path.isfile(LABELS_PATH):
        with open(LABELS_PATH) as f:
            _labels_cache = yaml.safe_load(f) or {}
    else:
        _labels_cache = {}
    return _labels_cache


def display_name(coll_name):
    """collection 名 → 人类可读显示名。"""
    labels = _load_labels()
    entry = labels.get(coll_name, {})
    return entry.get("display", coll_name)


def category_of(coll_name):
    """collection 名 → 分类名。"""
    labels = _load_labels()
    entry = labels.get(coll_name, {})
    return entry.get("category", "未分类")


@app.context_processor
def inject_sidebar():
    """向所有模板注入侧边栏爬虫列表（含显示名）"""
    try:
        db = get_db()
        raw = sorted(
            c for c in db.list_collection_names() if c != "stats"
        )
        crawlers = [{"name": c, "display": display_name(c)} for c in raw]
    except Exception:
        crawlers = []
    return {"sidebar_crawlers": crawlers, "display_name": display_name}

# MONGO_URL set via environment or default
_MONGO_PORT = os.environ.get("MONGO_PORT", "27017")
_MONGO_HOST = os.environ.get("MONGO_HOST", "localhost")
MONGO_URL = os.environ.get("MONGO_URL", f"mongodb://{_MONGO_HOST}:{_MONGO_PORT}")
LOGS_PATH = os.environ.get("LOGS_PATH", "/getweb-logs")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/getweb-config.yaml")
RELOAD_PATH = os.environ.get("RELOAD_PATH", "/getweb-reload")


DB_NAME = "webdata"


def get_db():
    client = MongoClient(MONGO_URL)
    return client[DB_NAME]


def beijing_today_start_ts():
    """北京时间今天 00:00 → UTC 时间戳"""
    now_utc = datetime.now(timezone.utc)
    now_bj = now_utc + timedelta(hours=8)
    today_bj = now_bj.replace(hour=0, minute=0, second=0, microsecond=0)
    today_utc = today_bj - timedelta(hours=8)
    return today_utc.timestamp()


def ts_to_beijing(ts):
    """时间戳 → 北京时间字符串"""
    if ts is None:
        return "—"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=8)
    return dt.strftime("%m-%d %H:%M")


def ts_ago(ts):
    """时间戳 → 相对时间（如 '18 分钟前'）"""
    if ts is None:
        return "从未"
    now = datetime.now(timezone.utc).timestamp()
    diff = int(now - ts)
    if diff < 60:
        return f"{diff} 秒前"
    if diff < 3600:
        return f"{diff // 60} 分钟前"
    if diff < 86400:
        return f"{diff // 3600} 小时前"
    return f"{diff // 86400} 天前"


@app.route("/")
def dashboard():
    db = get_db()
    today_ts = beijing_today_start_ts()

    crawlers = []
    grand_total = 0  # 爬取总数（不限时间）
    for coll_name in sorted(db.list_collection_names()):
        if coll_name == "stats":
            continue

        coll = db[coll_name]
        count = coll.count_documents({"crawltime": {"$gte": today_ts}})
        total_docs = coll.count_documents({})
        grand_total += total_docs

        last = coll.find_one(
            sort=[("crawltime", -1)], projection={"crawltime": 1}
        )
        last_ts = last.get("crawltime") if last else None

        crawlers.append({
            "name": coll_name,
            "display": display_name(coll_name),
            "category": category_of(coll_name),
            "count": count,
            "total_docs": total_docs,
            "last_ts": last_ts,
            "ago": ts_ago(last_ts),
        })

    total = sum(c["count"] for c in crawlers)
    active = [c for c in crawlers if c["count"] > 0]
    idle = [c for c in crawlers if c["count"] == 0]

    # 排序
    sort_by = request.args.get("sort", "count")
    if sort_by == "time":
        active.sort(key=lambda c: c["last_ts"] or 0, reverse=True)
    else:
        active.sort(key=lambda c: c["count"], reverse=True)
    idle.sort(key=lambda c: c["last_ts"] or 0, reverse=True)

    # 按分类分组（有产出的）
    categories = {}
    for c in active:
        cat = c["category"]
        categories.setdefault(cat, []).append(c)

    # 分类排序：按组内总数降序
    cat_order = sorted(categories, key=lambda cat: sum(x["count"] for x in categories[cat]), reverse=True)

    return render_template(
        "dashboard.html",
        crawlers=crawlers,
        active=active,
        idle=idle,
        categories=categories,
        cat_order=cat_order,
        total=total,
        grand_total=grand_total,
        total_crawlers=len(crawlers),
        sort=sort_by,
    )


@app.route("/crawler/<name>")
def crawler_detail(name):
    db = get_db()

    if name not in db.list_collection_names():
        return render_template("crawler.html", name=name, error="找不到此爬虫"), 404

    coll = db[name]
    today_ts = beijing_today_start_ts()
    filter_mode = request.args.get("filter", "today")
    q = request.args.get("q", "").strip()

    # 查询条件
    query = {}
    if filter_mode == "today":
        query["crawltime"] = {"$gte": today_ts}
    if q:
        query["title"] = {"$regex": re.escape(q), "$options": "i"}

    # Telegram 频道筛选
    channel_filter = request.args.get("channel", "").strip()
    channels = []
    if name == "telegram_channel":
        pipeline = [
            {"$match": {"crawltime": {"$gte": today_ts}} if filter_mode == "today" else {}},
            {"$group": {"_id": "$channel", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
        ]
        raw_channels = list(coll.aggregate(pipeline))
        labels = _load_labels()
        chan_labels = labels.get("channels", {})
        for ch in raw_channels:
            ch["display"] = chan_labels.get(ch["_id"], ch["_id"])
        channels = raw_channels
        if channel_filter:
            query["channel"] = channel_filter

    # 扫描此爬虫可用的额外字段（取一份样本即可，同一爬虫字段一致）
    available_extras = []
    field_types = {}
    sample = coll.find_one(sort=[("crawltime", -1)])
    if sample:
        for k in sample:
            if k not in ("_id", "title", "link", "crawltime"):
                label = EXTRA_LABELS.get(k, k)
                ft = "num" if isinstance(sample[k], (int, float)) else "str"
                available_extras.append((k, label, ft))
                field_types[k] = ft

    # 额外字段筛选：ef_<field>_op=gte&ef_<field>_val=50
    extra_filters = {}
    for k in field_types:
        op = request.args.get(f"ef_{k}_op", "")
        val = request.args.get(f"ef_{k}_val", "").strip()
        if not op or not val:
            continue
        if field_types[k] == "num":
            try:
                num_val = float(val) if "." in val else int(val)
            except ValueError:
                continue
            mongo_op = {"gte": "$gte", "gt": "$gt", "lte": "$lte", "lt": "$lt", "eq": "$eq"}.get(op, "$gte")
            query[k] = {mongo_op: num_val}
        else:
            query[k] = {"$regex": re.escape(val), "$options": "i"}
        extra_filters[k] = (op, val)

    # 总条数和今日数：有筛选时反映筛选结果
    if extra_filters:
        extra_only = {k: v for k, v in query.items() if k not in ("crawltime", "title")}
        total_all = coll.count_documents(extra_only) if extra_only else coll.count_documents({})
        today_query = {"crawltime": {"$gte": today_ts}}
        today_query.update(extra_only)
        today_count = coll.count_documents(today_query)
    else:
        total_all = coll.count_documents({})
        today_count = coll.count_documents({"crawltime": {"$gte": today_ts}})

    # 分页
    PER_PAGE = 20
    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1

    total_items = coll.count_documents(query)
    total_pages = max(1, (total_items + PER_PAGE - 1) // PER_PAGE)
    if page > total_pages:
        page = total_pages

    skip = (page - 1) * PER_PAGE
    items = list(coll.find(query, sort=[("crawltime", -1), ("_id", -1)]).skip(skip).limit(PER_PAGE))

    # 当前激活的额外字段
    active_extras_raw = request.args.get("extras", "")
    active_extras = [e for e in active_extras_raw.split(",") if e]

    # 生成每个切换按钮的 URL
    def _extras_url(toggle_key):
        new = [e for e in active_extras if e != toggle_key]
        if toggle_key not in active_extras:
            new.append(toggle_key)
        url = f"?filter={filter_mode}"
        if new:
            url += f"&extras={','.join(new)}"
        if q:
            url += f"&q={q}"
        return url

    extra_buttons = [
        (key, label, ft, _extras_url(key), key in active_extras)
        for key, label, ft in available_extras
    ]

    first = coll.find_one(sort=[("crawltime", 1)], projection={"crawltime": 1})
    last = coll.find_one(sort=[("crawltime", -1)], projection={"crawltime": 1})

    latest_ts = last["crawltime"] if last else None
    for item in items:
        item["_time"] = ts_to_beijing(item.get("crawltime"))
        # 提取激活的额外字段为徽章
        extras = []
        for k in active_extras:
            v = item.get(k)
            if v is not None:
                label = EXTRA_LABELS.get(k, k)
                if isinstance(v, str) and len(v) > 50:
                    v = v[:47] + "..."
                extras.append((label, v))
        item["_extras"] = extras

    return render_template(
        "crawler.html",
        name=name,
        display=display_name(name),
        items=items,
        latest_ts=latest_ts,
        channels=channels,
        channel_filter=channel_filter,
        total=total_items,
        total_all=total_all,
        total_pages=total_pages,
        page=page,
        q=q,
        filter_mode=filter_mode,
        today_count=today_count,
        extra_buttons=extra_buttons,
        active_extras=active_extras,
        extra_filters=extra_filters,
        field_types=field_types,
        first_time=ts_to_beijing(first.get("crawltime") if first else None),
        last_time=ts_to_beijing(last.get("crawltime") if last else None),
        last_ago=ts_ago(last.get("crawltime") if last else None),
        error=None,
    )


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return render_template("search.html", q="", results=[], total=0, sources={}, truncated=False)

    db = get_db()
    results = []
    source_counts = {}
    regex = {"$regex": re.escape(q), "$options": "i"}
    MAX_RESULTS = 500

    for coll_name in sorted(db.list_collection_names()):
        if coll_name == "stats":
            continue

        coll = db[coll_name]
        matched = coll.count_documents({"title": regex})
        if matched == 0:
            continue

        source_counts[coll_name] = matched
        items = list(coll.find(
            {"title": regex},
            sort=[("crawltime", -1), ("_id", -1)],
            limit=MAX_RESULTS - len(results),
        ))

        for item in items:
            item["_source"] = coll_name
            item["_time"] = ts_to_beijing(item.get("crawltime"))
            results.append(item)

        if len(results) >= MAX_RESULTS:
            break

    results.sort(key=lambda x: x.get("crawltime") or 0, reverse=True)
    truncated = len(results) >= MAX_RESULTS

    return render_template(
        "search.html",
        q=q,
        results=results,
        total=len(results),
        sources=source_counts,
        truncated=truncated,
    )


@app.route("/logs")
def view_logs():
    """查看 getWeb 日志"""
    file = request.args.get("file", "getweb.log")
    lines = request.args.get("lines", 200, type=int)
    level = request.args.get("level", "").upper()
    crawler = request.args.get("crawler", "").strip()

    # 列出可用的日志文件
    log_files = []
    if os.path.isdir(LOGS_PATH):
        log_files = sorted(
            f for f in os.listdir(LOGS_PATH)
            if f.endswith(".log")
        )

    # 安全检查：防止路径穿越
    file_path = os.path.join(LOGS_PATH, os.path.basename(file))
    log_lines = []
    if os.path.isfile(file_path):
        with open(file_path, errors="ignore") as f:
            raw = f.readlines()

        # 过滤
        for line in raw:
            if level and f"| {level:<8}" not in line:
                continue
            if crawler and f"[{crawler}]" not in line:
                continue
            log_lines.append(line.strip())

        # 取最后 N 行，倒序（最新在前）
        log_lines = log_lines[-lines:]
        log_lines.reverse()

    return render_template(
        "logs.html",
        log_files=log_files,
        current_file=file,
        log_lines=log_lines,
        level=level,
        crawler=crawler,
        lines=lines,
        crawler_names=[c for c in get_db().list_collection_names() if c != "stats"],
    )


@app.route("/stats/trend")
def stats_trend():
    """每日新增趋势折线图 — 仅聚合 Top 8 爬虫"""
    db = get_db()
    days = request.args.get("days", 7, type=int)
    if days not in (7, 14, 30):
        days = 7

    tz = timezone(timedelta(hours=8))
    since = datetime.now(tz) - timedelta(days=days)
    since = since.replace(hour=0, minute=0, second=0, microsecond=0)
    since_ts = since.timestamp()

    # 生成全部日期标签
    all_dates = []
    for i in range(days):
        d = since + timedelta(days=i)
        all_dates.append(d.strftime("%m-%d"))

    # 阶段 1：快速统计各爬虫周期总量（只用 count，轻量）
    crawler_totals = {}
    for coll_name in sorted(db.list_collection_names()):
        if coll_name == "stats":
            continue
        total = db[coll_name].count_documents(
            {"crawltime": {"$exists": True, "$gte": since_ts}}
        )
        if total > 0:
            crawler_totals[coll_name] = total

    # 阶段 2：聚合所有有数据的爬虫按天统计，同时算出每日总量
    top_names = sorted(crawler_totals, key=lambda k: crawler_totals[k], reverse=True)[:8]

    crawler_trends = {}
    daily_totals = {date: 0 for date in all_dates}

    for name in crawler_totals:
        coll = db[name]
        pipeline = [
            {"$match": {"crawltime": {"$gte": since_ts}}},
            {"$group": {
                "_id": {"$dateToString": {
                    "format": "%m-%d",
                    "date": {"$toDate": {"$multiply": ["$crawltime", 1000]}},
                }},
                "count": {"$sum": 1},
            }},
            {"$sort": {"_id": 1}},
        ]
        daily = list(coll.aggregate(pipeline))
        date_map = {d["_id"]: d["count"] for d in daily}

        # 累加到每日总量
        for date in all_dates:
            daily_totals[date] += date_map.get(date, 0)

        # Top 8 存入折线图数据
        if name in top_names:
            crawler_trends[name] = {
                date: date_map.get(date, 0) for date in all_dates
            }

    # 转为有序列表给 Chart.js
    daily_total_values = [daily_totals[date] for date in all_dates]

    return render_template(
        "stats_trend.html",
        days=days,
        all_dates=all_dates,
        top_names=top_names,
        crawler_trends=crawler_trends,
        daily_total_values=daily_total_values,
    )


@app.route("/stats/today")
def stats_today():
    """今日各站新增对比柱状图 — 仅 count，秒级加载"""
    db = get_db()
    today_ts = beijing_today_start_ts()

    counts = []
    for coll_name in sorted(db.list_collection_names()):
        if coll_name == "stats":
            continue
        n = db[coll_name].count_documents({"crawltime": {"$gte": today_ts}})
        counts.append((coll_name, n))

    counts.sort(key=lambda x: x[1], reverse=True)

    return render_template("stats_today.html", counts=counts)


def _dedup_label(ccfg: dict, is_configured: bool) -> str:
    """去重策略的人类可读标签。未配置返回空串，coins 显式 null → 不去重"""
    if not is_configured:
        return ""
    dk = ccfg.get("dedup_key")
    if dk is None:
        return "无去重"
    if dk:
        return dk
    return "link"


def _cron_to_human(expr):
    """将 cron 表达式转人类可读，仅处理 getWeb config.yaml 中出现的模式"""
    parts = expr.strip().split()
    if len(parts) != 5:
        return expr
    minute, hour, dom, month, dow = parts

    # */N * * * * → 每 N 分钟
    if minute.startswith("*/") and hour == "*" and dom == "*":
        return f"每 {minute[2:]} 分钟"

    # 0 */N * * * → 每 N 小时
    if minute == "0" and hour.startswith("*/"):
        return f"每 {hour[2:]} 小时"

    # 0 * * * * → 每小时
    if minute == "0" and hour == "*" and dom == "*":
        return "每小时"

    # M H * * * → 每天 HH:MM
    if minute.isdigit() and hour.isdigit() and dom == "*" and month == "*" and dow == "*":
        return f"每天 {int(hour):02d}:{int(minute):02d}"

    return expr


def _cron_frequency(expr):
    """计算 cron 表达式的每日执行次数，用于排序"""
    parts = expr.strip().split()
    if len(parts) != 5:
        return 0
    minute, hour = parts[0], parts[1]

    if minute.startswith("*/"):   # */N * * * *
        return 1440 // int(minute[2:])
    if minute == "0":
        if hour.startswith("*/"):  # 0 */N * * *
            return 24 // int(hour[2:])
        if hour == "*":            # 0 * * * *
            return 24
    if minute.isdigit() and hour.isdigit():  # M H * * *
        return 1
    return 0


@app.route("/config", methods=["GET", "POST"])
def crawler_config():
    """展示并编辑 getWeb config.yaml 中的爬虫调度配置"""
    import yaml

    if request.method == "POST":
        action = request.form.get("action", "")
        
        if not os.path.isfile(CONFIG_PATH):
            return {"error": "配置文件未找到"}, 500
        
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        
        if action == "toggle":
            name = request.form.get("name", "")
            cron = cfg.get("schedules", {}).get(name, "")
            if cron.startswith("#"):
                cfg["schedules"][name] = cron.lstrip("#")
            elif cron:
                cfg["schedules"][name] = "#" + cron
        
        elif action == "update_cron":
            name = request.form.get("name", "")
            new_cron = request.form.get("cron", "").strip()
            if name and new_cron:
                old = cfg.get("schedules", {}).get(name, "")
                prefix = "#" if old.startswith("#") else ""
                cfg["schedules"][name] = prefix + new_cron
        
        # 写回 config.yaml
        yaml_str = yaml.dump(cfg, allow_unicode=True, default_flow_style=False, sort_keys=False)
        # 只在 schedules: 段内给未加引号的 cron 值统一加单引号
        yaml_str = yaml.dump(cfg, allow_unicode=True, default_flow_style=False, sort_keys=False)
        lines = yaml_str.split("\n")
        in_schedules = False
        for i, line in enumerate(lines):
            if line.startswith("schedules:"):
                in_schedules = True
                continue
            if in_schedules:
                # 遇到下一个顶级 key（不缩进），退出 schedules 段
                if line and not line[0].isspace():
                    in_schedules = False
                    continue
                m = re.match(r'^(\s+)(\w+):\s+([\d\s\*/]+)$', line)
                if m:
                    indent, name, cron = m.groups()
                    lines[i] = f"{indent}{name}: '{cron}'"
        yaml_str = "\n".join(lines)
        with open(CONFIG_PATH, "w") as f:
            f.write(yaml_str)
        
        # 触发热加载
        try:
            with open(RELOAD_PATH, "w") as f:
                f.write("reload")
        except Exception:
            pass
        
        return {"ok": True}
    
    # GET — 展示配置
    schedules_list = []
    error = None
    stats = {"total": 0, "enabled": 0, "disabled": 0}

    if os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        
        crawlers_cfg = cfg.get("crawlers", {})
        
        for name, cron_expr in cfg.get("schedules", {}).items():
            disabled = cron_expr.startswith("#")
            clean_cron = cron_expr.lstrip("#")
            ccfg = crawlers_cfg.get(name, {})
            is_configured = name in crawlers_cfg
            schedules_list.append({
                "name": name, "display": display_name(name),
                "cron": clean_cron,
                "cron_raw": cron_expr,
                "human": _cron_to_human(clean_cron),
                "enabled": not disabled,
                "freq": _cron_frequency(clean_cron),
                "dedup_key": _dedup_label(ccfg, is_configured),
                "update_fields": ccfg.get("update_fields"),
            })
            stats["total"] += 1
            if disabled:
                stats["disabled"] += 1
            else:
                stats["enabled"] += 1
    else:
        error = f"配置文件未找到：{CONFIG_PATH}"

    schedules_list.sort(key=lambda s: s["freq"], reverse=True)
    return render_template("config.html", schedules=schedules_list, stats=stats, error=error)


@app.route("/api/today")
def api_today():
    """导出今日所有抓取数据为 JSON，供 AI 日报等消费。

    Query params:
        compact=1   默认，仅返回 title/link/source（~50KB/百条）
        full=1      返回完整字段
    """
    db = get_db()
    today_ts = beijing_today_start_ts()
    compact = request.args.get("full") != "1"

    result = {}
    total = 0
    for coll_name in sorted(db.list_collection_names()):
        if coll_name == "stats":
            continue
        items = list(db[coll_name].find(
            {"crawltime": {"$gte": today_ts}},
            sort=[("crawltime", -1)],
        ))
        if not items:
            continue
        for item in items:
            item.pop("_id", None)
            if compact:
                # 只保留核心字段，大幅减小体积
                item = {
                    "title": item.get("title", ""),
                    "link": item.get("link", ""),
                }
            item["source"] = coll_name
            result.setdefault(coll_name, []).append(item)
            total += 1

    # 按数量排序
    sorted_result = dict(sorted(result.items(), key=lambda x: len(x[1]), reverse=True))
    sorted_result["_total"] = total
    sorted_result["_date"] = datetime.fromtimestamp(today_ts, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    return sorted_result


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
