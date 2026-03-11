import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import RLock
from typing import Dict, List, Optional
from urllib.request import Request, urlopen

import feedparser
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request, url_for
from openai import OpenAI

load_dotenv(override=True)

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

RSS_SOURCES = [
    {"name": "OpenAI News", "url": "https://openai.com/blog/rss.xml"},
    {"name": "Google AI Blog", "url": "https://blog.google/technology/ai/rss/"},
    {"name": "Hugging Face Blog", "url": "https://huggingface.co/blog/feed.xml"},
    {"name": "NVIDIA Omniverse Blog", "url": "https://blogs.nvidia.com/blog/category/omniverse/feed/"},
    {"name": "Adobe AI Blog", "url": "https://blog.adobe.com/en/topics/ai/feed"},
    {"name": "Stability AI Blog", "url": "https://stability.ai/news/rss.xml"},
]

MAX_ITEMS = 6
MIN_ITEMS = 5
RSS_TIMEOUT_SECONDS = 9
MAX_ENTRIES_PER_SOURCE = 18
DIVERSITY_PENALTY = 3
CN_TZ = timezone(timedelta(hours=8))
DATA_PATH = Path("data/summaries.json")
LOCK = RLock()
LAST_ERROR = ""

SOURCE_WEIGHTS = {
    "OpenAI": 4,
    "Google AI Blog": 4,
    "NVIDIA Omniverse": 4,
    "Adobe AI": 3,
    "Stability AI": 3,
    "Hugging Face": 3,
}

FOCUS_DOMAIN_KEYWORDS = {
    "ai_game": ["game", "gaming", "unreal", "unity", "npc", "gameplay", "游戏"],
    "ai_video": ["video generation", "text-to-video", "video model", "sora", "veo", "视频生成"],
    "ai_film": ["film", "movie", "cinematic", "filmmaking", "animation", "vfx", "studio", "影视", "电影", "短片", "动画"],
    "ai_virtual": ["avatar", "digital human", "virtual production", "3d generation", "数字人", "虚拟制作", "3d"],
}
FOCUS_DOMAIN_BONUS = 3

TOPIC_KEYWORDS = {
    "游戏": ["game", "gaming", "unreal", "unity", "npc", "gameplay", "游戏"],
    "视频生成": ["video generation", "text-to-video", "video model", "sora", "veo", "视频生成"],
    "影视生成": ["film", "movie", "cinematic", "filmmaking", "animation", "vfx", "studio", "影视", "电影", "短片", "动画"],
}
DEFAULT_TOPIC = "通用 AI"

AI_STRONG_EN_KEYWORDS = [
    "artificial intelligence",
    "generative ai",
    "genai",
    "llm",
    "large language model",
    "machine learning",
    "neural network",
    "text-to-video",
    "video generation",
    "video model",
    "image generation",
    "stable diffusion",
    "diffusion",
    "sora",
    "veo",
    "runway",
]
AI_STRONG_ZH_KEYWORDS = [
    "生成式ai",
    "生成式人工智能",
    "人工智能",
    "大模型",
    "智能体",
    "视频生成",
    "图像生成",
    "文生视频",
    "数字人",
    "虚拟制作",
]
AI_WEAK_EN_KEYWORDS = ["ai"]
AI_CONTEXT_EN_KEYWORDS = [
    "game", "gaming", "unreal", "unity", "npc",
    "video", "film", "movie", "cinematic", "animation", "vfx", "studio",
]
AI_CONTEXT_ZH_KEYWORDS = ["游戏", "影视", "电影", "短片", "动画", "视频"]

MIXED_CONTENT_SOURCES = {"NVIDIA Omniverse Blog", "Adobe AI Blog"}

AI_STRONG_EN_PATTERNS = [re.compile(rf"\b{re.escape(keyword)}\b", re.IGNORECASE) for keyword in AI_STRONG_EN_KEYWORDS]
AI_WEAK_EN_PATTERNS = [re.compile(rf"\b{re.escape(keyword)}\b", re.IGNORECASE) for keyword in AI_WEAK_EN_KEYWORDS]
AI_CONTEXT_EN_PATTERNS = [re.compile(rf"\b{re.escape(keyword)}\b", re.IGNORECASE) for keyword in AI_CONTEXT_EN_KEYWORDS]

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


def build_summary_cache_by_url() -> Dict[str, str]:
    summary_by_url: Dict[str, str] = {}
    for items in CACHE.values():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            summary = item.get("summary")
            if isinstance(url, str) and url and isinstance(summary, str) and summary:
                summary_by_url[url] = summary
    return summary_by_url


def build_topic_cache_by_url() -> Dict[str, str]:
    topic_by_url: Dict[str, str] = {}
    for items in CACHE.values():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            topic = item.get("topic")
            if isinstance(url, str) and url and isinstance(topic, str) and topic:
                topic_by_url[url] = topic
    return topic_by_url


def parse_entry_time(entry) -> datetime:
    candidate = (
        entry.get("published")
        or entry.get("updated")
        or entry.get("pubDate")
        or entry.get("created")
    )
    if candidate:
        try:
            dt = parsedate_to_datetime(candidate)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass

    parsed_candidate = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed_candidate:
        return datetime(*parsed_candidate[:6], tzinfo=timezone.utc)

    return datetime.now(timezone.utc)


def normalize_title(title: str) -> str:
    return "".join(ch.lower() for ch in title if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def is_similar_title(title: str, seen_titles: List[str], threshold: float = 0.82) -> bool:
    current = normalize_title(title)
    if not current:
        return False
    return any(SequenceMatcher(None, current, existing).ratio() >= threshold for existing in seen_titles)


def is_ai_related(article: Dict[str, str]) -> bool:
    text = f"{article.get('title', '')} {article.get('raw_summary', '')}"
    text_lower = text.lower()

    if any(pattern.search(text) for pattern in AI_STRONG_EN_PATTERNS):
        return True
    if any(keyword in text_lower for keyword in AI_STRONG_ZH_KEYWORDS):
        return True

    weak_hit = any(pattern.search(text) for pattern in AI_WEAK_EN_PATTERNS)
    if not weak_hit:
        return False

    context_en_hit = any(pattern.search(text) for pattern in AI_CONTEXT_EN_PATTERNS)
    context_zh_hit = any(keyword in text_lower for keyword in AI_CONTEXT_ZH_KEYWORDS)
    return context_en_hit or context_zh_hit


def has_strong_ai_signal(article: Dict[str, str]) -> bool:
    text = f"{article.get('title', '')} {article.get('raw_summary', '')}"
    text_lower = text.lower()
    return any(pattern.search(text) for pattern in AI_STRONG_EN_PATTERNS) or any(
        keyword in text_lower for keyword in AI_STRONG_ZH_KEYWORDS
    )


def fetch_source_articles(source: Dict[str, str]) -> Dict[str, object]:
    source_name = source["name"]
    source_url = source["url"]
    start = time.monotonic()
    source_articles: List[Dict[str, str]] = []

    try:
        req = Request(source_url, headers={"User-Agent": "ai-news-digest/1.0"})
        with urlopen(req, timeout=RSS_TIMEOUT_SECONDS) as response:
            content = response.read()

        feed = feedparser.parse(content)
        source_title = feed.feed.get("title") or source_name

        parsed_articles: List[Dict[str, str]] = []
        for entry in feed.entries[:MAX_ENTRIES_PER_SOURCE]:
            summary = (entry.get("summary") or entry.get("description") or "").strip()
            parsed_articles.append(
                {
                    "title": entry.get("title", "无标题"),
                    "url": entry.get("link", "#"),
                    "source": source_title,
                    "published": parse_entry_time(entry),
                    "raw_summary": summary,
                }
            )

        # AI相关性硬过滤：所有来源执行过滤；混合来源必须命中强AI关键词
        if source_name in MIXED_CONTENT_SOURCES:
            logger.info("混合来源启用更严格AI过滤 source=%s", source_name)
            source_articles = [
                article for article in parsed_articles
                if is_ai_related(article) and has_strong_ai_signal(article)
            ]
        else:
            source_articles = [article for article in parsed_articles if is_ai_related(article)]

        elapsed = time.monotonic() - start
        logger.info(
            "RSS抓取成功 source=%s url=%s cost=%.2fs total_count=%d ai_related_count=%d",
            source_name,
            source_url,
            elapsed,
            len(parsed_articles),
            len(source_articles),
        )
        return {"articles": source_articles, "error": ""}
    except Exception as e:
        elapsed = time.monotonic() - start
        error_detail = repr(e)
        logger.error(
            "RSS抓取失败 source=%s url=%s cost=%.2fs reason=%s",
            source_name,
            source_url,
            elapsed,
            error_detail,
        )
        return {"articles": [], "error": f"{source_name}: {error_detail}"}


def fetch_latest_ai_articles(limit: int = 50) -> List[Dict[str, str]]:
    articles: List[Dict[str, str]] = []
    failed_sources: List[str] = []

    with ThreadPoolExecutor(max_workers=len(RSS_SOURCES)) as executor:
        futures = [executor.submit(fetch_source_articles, source) for source in RSS_SOURCES]
        for future in as_completed(futures):
            result = future.result()
            articles.extend(result["articles"])
            if result["error"]:
                failed_sources.append(result["error"])

    if not articles:
        fail_text = "；".join(failed_sources) if failed_sources else "无可用 RSS 源"
        raise RuntimeError(f"资讯抓取失败：{fail_text}")

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

    raw_summary = article.get("raw_summary", "")
    short_notice = (
        "原文摘要较短，请基于标题与关键词做适度背景补充，但不要编造具体事实。"
        if len(raw_summary) < 180
        else ""
    )
    prompt = f"""你是AI产业分析师。请基于以下新闻生成一段简体中文“分析型摘要”（<=300字）。
要求：
1) 先说清新闻核心内容；
2) 补充相关技术背景；
3) 简要解释关键原理/机制；
4) 点出对行业、创作流程或产品竞争的潜在影响；
5) 信息密度高，减少机械翻译感；
6) 允许做合理背景扩展，但严禁编造新闻中未确认的具体事实（数字、发布时间、合作方、已落地结果等）。
{short_notice}

标题: {article['title']}
来源: {article['source']}
正文摘要: {raw_summary[:2000]}"""

    try:
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
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


def classify_article_topic(article: Dict[str, str]) -> str:
    text = f"{article.get('title', '')} {article.get('raw_summary', '')}".lower()
    for topic, keywords in TOPIC_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return topic
    return DEFAULT_TOPIC


def count_focus_domain_hits(article: Dict[str, str]) -> int:
    text = f"{article['title']} {article['raw_summary']}".lower()
    hits = 0
    for keywords in FOCUS_DOMAIN_KEYWORDS.values():
        if any(keyword in text for keyword in keywords):
            hits += 1
    return hits


def score_article(article: Dict[str, str]) -> int:
    text = f"{article['title']} {article['raw_summary']}".lower()
    score = 0
    for kw in [
        "release", "launched", "model", "benchmark", "paper", "open-source",
        "api", "agent", "multimodal", "reasoning", "sota", "breakthrough",
    ]:
        if kw in text:
            score += 2

    focus_hits = count_focus_domain_hits(article)
    score += focus_hits * FOCUS_DOMAIN_BONUS

    age_hours = (datetime.now(timezone.utc) - article["published"]).total_seconds() / 3600
    if age_hours <= 24:
        score += 3
    elif age_hours <= 72:
        score += 1

    return score + get_source_weight(article.get("source", ""))


def select_diverse_articles(ranked: List[Dict[str, str]], target_count: int) -> List[Dict[str, str]]:
    selected: List[Dict[str, str]] = []
    source_counts: Dict[str, int] = {}
    remaining = ranked.copy()

    while remaining and len(selected) < target_count:
        best_idx = 0
        best_score = float("-inf")
        for idx, item in enumerate(remaining):
            source = item.get("source", "未知来源")
            already_selected = source_counts.get(source, 0)
            adjusted = score_article(item) - already_selected * DIVERSITY_PENALTY
            if already_selected == 0:
                adjusted += 1
            if adjusted > best_score:
                best_score = adjusted
                best_idx = idx

        pick = remaining.pop(best_idx)
        selected.append(pick)
        source = pick.get("source", "未知来源")
        source_counts[source] = source_counts.get(source, 0) + 1

    return selected


def build_daily_digest(force_refresh: bool = False) -> List[Dict[str, str]]:
    day_key = datetime.now(CN_TZ).strftime("%Y-%m-%d")
    if not force_refresh and day_key in CACHE:
        return CACHE[day_key]

    try:
        api_key = os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("OPENAI_BASE_URL")

        candidates = fetch_latest_ai_articles(limit=50)
        ranked = sorted(candidates, key=score_article, reverse=True)

        target_count = min(MAX_ITEMS, max(MIN_ITEMS, len(ranked)))
        selected = select_diverse_articles(ranked, target_count)

        source_distribution: Dict[str, int] = {}
        for item in selected:
            source = item.get("source", "未知来源")
            source_distribution[source] = source_distribution.get(source, 0) + 1
        logger.info("最终入选来源分布：%s", source_distribution)
        selected_focus_hits = sum(1 for item in selected if count_focus_domain_hits(item) > 0)
        logger.info("最终入选重点领域命中数量：%d/%d", selected_focus_hits, len(selected))

        summary_by_url = build_summary_cache_by_url()
        topic_by_url = build_topic_cache_by_url()
        reused_count = 0
        generated_count = 0
        client: Optional[OpenAI] = None

        result = []
        for item in selected:
            cached_summary = summary_by_url.get(item["url"])
            if cached_summary:
                summary = cached_summary
                reused_count += 1
            else:
                if client is None and api_key:
                    client = OpenAI(
                        api_key=api_key,
                        base_url=base_url if base_url else None,
                    )
                summary = summarize_in_chinese(item, client)
                summary_by_url[item["url"]] = summary
                generated_count += 1

            topic = topic_by_url.get(item["url"]) or classify_article_topic(item)
            topic_by_url[item["url"]] = topic

            result.append(
                {
                    "title": item["title"],
                    "url": item["url"],
                    "source": item["source"],
                    "topic": topic,
                    "published": item["published"].astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M"),
                    "summary": summary,
                }
            )

        logger.info("摘要复用统计：reused=%d generated=%d", reused_count, generated_count)

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
        logger.exception("构建每日摘要失败")
        set_last_error("抓取失败：网络或订阅源可能暂时不可用，请稍后点击“手动刷新资讯”重试。")
        return CACHE.get(day_key, [])


def scheduled_daily_refresh() -> None:
    build_daily_digest(force_refresh=True)


@app.route("/")
def index():
    selected_source = request.args.get("source", "all")
    selected_topic = request.args.get("topic", "all")
    all_items = build_daily_digest()
    available_sources = sorted({item.get("source", "未知来源") for item in all_items})
    available_topics = ["游戏", "视频生成", "影视生成", "通用 AI"]

    items = all_items
    if selected_source != "all":
        items = [item for item in items if item.get("source") == selected_source]
    if selected_topic != "all":
        items = [item for item in items if item.get("topic", "通用 AI") == selected_topic]

    return render_template(
        "index.html",
        items=items,
        updated_at=datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M"),
        error_message=get_last_error(),
        sources=available_sources,
        topics=available_topics,
        selected_source=selected_source,
        selected_topic=selected_topic,
    )


@app.route("/refresh", methods=["POST"])
def refresh_news():
    selected_source = request.form.get("source", "all")
    selected_topic = request.form.get("topic", "all")
    build_daily_digest(force_refresh=True)
    return redirect(url_for("index", source=selected_source, topic=selected_topic))


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
