import json
import os
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import RLock
from typing import Dict, List, Optional

import feedparser
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request, url_for
from openai import OpenAI

load_dotenv(override=True)

app = Flask(__name__)

RSS_SOURCES = [
    "https://openai.com/blog/rss.xml",
    "https://www.anthropic.com/news/rss.xml",
    "https://huggingface.co/blog/feed.xml",
]

MAX_ITEMS = 10
MIN_ITEMS = 5
CN_TZ = timezone(timedelta(hours=8))
DATA_PATH = Path("data/summaries.json")
LOCK = RLock()
LAST_ERROR = ""

SOURCE_WEIGHTS = {
    "OpenAI": 4,
    "Anthropic": 4,
    "Google AI Blog": 4,
    "MIT Technology Review": 3,
    "Hugging Face": 2,
}

CACHE: Dict[str, List[Dict[str, str]]] = {}


def set_last_error(message: str = "") -> None:
    global LAST_ERROR
    with LOCK:
        LAST_ERROR = message


def get_last_error() -> str:
    with LOCK:
        return LAST_ERROR


def ensure_data_file() -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DATA_PATH.exists():
        DATA_PATH.write_text("{}", encoding="utf-8")


def load_persisted_cache() -> None:
    ensure_data_file()
    try:
        persisted = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        if isinstance(persisted, dict):
            for day_key, items in persisted.items():
                if isinstance(day_key, str) and isinstance(items, list):
                    CACHE[day_key] = items
    except (json.JSONDecodeError, OSError):
        set_last_error("历史摘要读取失败，系统将重新抓取资讯。")


def persist_cache() -> None:
    ensure_data_file()
    try:
        DATA_PATH.write_text(
            json.dumps(CACHE, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        set_last_error("摘要保存失败，请检查 data 目录写入权限。")


def parse_entry_time(entry) -> datetime:
    candidate = (
        entry.get("published")
        or entry.get("updated")
        or entry.get("pubDate")
        or entry.get("created")
    )
    if not candidate:
        return datetime.now(timezone.utc)

    try:
        dt = parsedate_to_datetime(candidate)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def normalize_title(title: str) -> str:
    return "".join(ch.lower() for ch in title if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def is_similar_title(title: str, seen_titles: List[str], threshold: float = 0.82) -> bool:
    current = normalize_title(title)
    if not current:
        return False
    return any(SequenceMatcher(None, current, existing).ratio() >= threshold for existing in seen_titles)


def fetch_latest_ai_articles(limit: int = 50) -> List[Dict[str, str]]:
    articles: List[Dict[str, str]] = []

    for source in RSS_SOURCES:
        feed = feedparser.parse(source)
        source_title = feed.feed.get("title", source)

        for entry in feed.entries:
            summary = (entry.get("summary") or entry.get("description") or "").strip()
            articles.append(
                {
                    "title": entry.get("title", "无标题"),
                    "url": entry.get("link", "#"),
                    "source": source_title,
                    "published": parse_entry_time(entry),
                    "raw_summary": summary,
                }
            )

    if not articles:
        raise RuntimeError("资讯抓取失败：暂时无法获取 RSS 内容，请稍后重试。")

    dedup_by_url: Dict[str, Dict[str, str]] = {}
    for item in articles:
        dedup_by_url[item["url"]] = item

    by_time = sorted(dedup_by_url.values(), key=lambda x: x["published"], reverse=True)
    final_items: List[Dict[str, str]] = []
    seen_titles: List[str] = []
    for item in by_time:
        title = item.get("title", "")
        if is_similar_title(title, seen_titles):
            continue
        seen_titles.append(normalize_title(title))
        final_items.append(item)

    return final_items[:limit]


def summarize_in_chinese(article: Dict[str, str], client: Optional[OpenAI]) -> str:
    fallback = article["raw_summary"][:280] or "该资讯暂无可用摘要，请点击查看原文。"
    if client is None:
        return fallback

    prompt = (
        "Summarize the following AI news article in Simplified Chinese. "
        "Focus on the technical highlights or practical insights. "
        "Requirements: keep it under 300 Chinese characters, concise and readable.\n\n"
        f"Title: {article['title']}\n"
        f"Source: {article['source']}\n"
        f"Content: {article['raw_summary'][:2000]}"
    )

    try:
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=380,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text[:300] if text else fallback
    except Exception as e:
        print("摘要生成失败：", repr(e))
        return fallback


def get_source_weight(source: str) -> int:
    lower = source.lower()
    for key, weight in SOURCE_WEIGHTS.items():
        if key.lower() in lower:
            return weight
    return 0


def score_article(article: Dict[str, str]) -> int:
    text = f"{article['title']} {article['raw_summary']}".lower()
    score = 0
    for kw in [
        "release", "launched", "model", "benchmark", "paper", "open-source",
        "api", "agent", "multimodal", "reasoning", "sota", "breakthrough",
    ]:
        if kw in text:
            score += 2

    age_hours = (datetime.now(timezone.utc) - article["published"]).total_seconds() / 3600
    if age_hours <= 24:
        score += 3
    elif age_hours <= 72:
        score += 1

    return score + get_source_weight(article.get("source", ""))


def build_daily_digest(force_refresh: bool = False) -> List[Dict[str, str]]:
    day_key = datetime.now(CN_TZ).strftime("%Y-%m-%d")
    if not force_refresh and day_key in CACHE:
        return CACHE[day_key]

    try:
        api_key = os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("OPENAI_BASE_URL")
        print("BASE_URL =", os.getenv("OPENAI_BASE_URL"))
        print("API_KEY 前10位 =", repr((api_key or "")[:10]))
        print("API_KEY 长度 =", len(api_key or ""))
        print("API_KEY 是否含非ASCII字符 =", any(ord(c) > 127 for c in (api_key or "")))
        
        client = OpenAI(
            api_key=api_key,
            base_url=base_url if base_url else None
        ) if api_key else None

        candidates = fetch_latest_ai_articles(limit=50)
        ranked = sorted(candidates, key=score_article, reverse=True)

        target_count = min(MAX_ITEMS, max(MIN_ITEMS, len(ranked)))
        selected = ranked[:target_count]

        result = [
            {
                "title": item["title"],
                "url": item["url"],
                "source": item["source"],
                "published": item["published"].astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M"),
                "summary": summarize_in_chinese(item, client),
            }
            for item in selected
        ]

        with LOCK:
            CACHE[day_key] = result
            recent_keys = sorted(CACHE.keys(), reverse=True)[:7]
            for key in list(CACHE.keys()):
                if key not in recent_keys:
                    CACHE.pop(key, None)
            persist_cache()

        set_last_error("")
        return result
    except Exception:
        set_last_error("抓取失败：网络或订阅源可能暂时不可用，请稍后点击“手动刷新资讯”重试。")
        return CACHE.get(day_key, [])


def scheduled_daily_refresh() -> None:
    build_daily_digest(force_refresh=True)


@app.route("/")
def index():
    selected_source = request.args.get("source", "all")
    all_items = build_daily_digest()
    available_sources = sorted({item.get("source", "未知来源") for item in all_items})

    if selected_source != "all":
        items = [item for item in all_items if item.get("source") == selected_source]
    else:
        items = all_items

    return render_template(
        "index.html",
        items=items,
        updated_at=datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M"),
        error_message=get_last_error(),
        sources=available_sources,
        selected_source=selected_source,
    )


@app.route("/refresh", methods=["POST"])
def refresh_news():
    selected_source = request.form.get("source", "all")
    build_daily_digest(force_refresh=True)
    return redirect(url_for("index", source=selected_source))


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=CN_TZ)
    scheduler.add_job(
        scheduled_daily_refresh,
        trigger="cron",
        hour=8,
        minute=0,
        id="daily_8am_refresh",
        replace_existing=True,
    )
    scheduler.start()
    return scheduler


def should_start_scheduler() -> bool:
    debug_mode = os.getenv("FLASK_DEBUG", "true").lower() in {"1", "true", "yes"}
    if not debug_mode:
        return True
    return os.getenv("WERKZEUG_RUN_MAIN") == "true"


load_persisted_cache()
SCHEDULER = start_scheduler() if should_start_scheduler() else None


if __name__ == "__main__":
    host = os.getenv("FLASK_RUN_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_RUN_PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "true").lower() in {"1", "true", "yes"}
    app.run(host=host, port=port, debug=debug)
