import os
from datetime import datetime, timezone, timedelta

from flask import Flask, render_template, request
from pymongo import MongoClient

app = Flask(__name__)

# MONGO_URL set via environment or default
_MONGO_PORT = os.environ.get("MONGO_PORT", "27017")
_MONGO_HOST = os.environ.get("MONGO_HOST", "localhost")
MONGO_URL = os.environ.get("MONGO_URL", f"mongodb://{_MONGO_HOST}:{_MONGO_PORT}")
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
    for coll_name in sorted(db.list_collection_names()):
        if coll_name == "stats":
            continue

        coll = db[coll_name]
        count = coll.count_documents({"crawltime": {"$gte": today_ts}})

        last = coll.find_one(
            sort=[("crawltime", -1)], projection={"crawltime": 1}
        )
        last_ts = last.get("crawltime") if last else None

        crawlers.append({
            "name": coll_name,
            "count": count,
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

    return render_template(
        "dashboard.html",
        crawlers=crawlers,
        active=active,
        idle=idle,
        total=total,
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

    # 分页
    PER_PAGE = 20
    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1

    total_items = coll.count_documents({})
    total_pages = max(1, (total_items + PER_PAGE - 1) // PER_PAGE)
    if page > total_pages:
        page = total_pages

    skip = (page - 1) * PER_PAGE
    items = list(coll.find(sort=[("crawltime", -1)]).skip(skip).limit(PER_PAGE))
    today_count = coll.count_documents({"crawltime": {"$gte": today_ts}})

    first = coll.find_one(sort=[("crawltime", 1)], projection={"crawltime": 1})
    last = coll.find_one(sort=[("crawltime", -1)], projection={"crawltime": 1})

    for item in items:
        item["_time"] = ts_to_beijing(item.get("crawltime"))

    return render_template(
        "crawler.html",
        name=name,
        items=items,
        total=total_items,
        total_pages=total_pages,
        page=page,
        today_count=today_count,
        first_time=ts_to_beijing(first.get("crawltime") if first else None),
        last_time=ts_to_beijing(last.get("crawltime") if last else None),
        last_ago=ts_ago(last.get("crawltime") if last else None),
        error=None,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
