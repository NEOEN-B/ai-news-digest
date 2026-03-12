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
from urllib.parse import urlparse
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
    {"name": "NVIDIA Omniverse Blog", "url": "https://developer.nvidia.com/blog/tag/omniverse/feed"},
    {"name": "Stability AI Blog", "url": "https://stability.ai/news/rss.xml"},
    {"name": "DeepMind Blog", "url": "https://deepmind.google/blog/rss.xml"},
]

SEARCH_MODE_SITES = [
    "openai.com",
    "blog.google",
    "deepmind.google",
    "huggingface.co",
    "stability.ai",
    "developer.nvidia.com",
]
SEARCH_ALLOWED_HOSTS = {
    "openai.com",
    "blog.google",
    "deepmind.google",
    "huggingface.co",
    "stability.ai",
    "developer.nvidia.com",
}
SEARCH_BLOCKED_HOSTS = {
    "forums.developer.nvidia.com",
    "reddit.com",
    "www.reddit.com",
    "news.ycombinator.com",
}
SEARCH_BLOCKED_PATH_KEYWORDS = ["/forum", "/forums", "/community", "/discuss", "/discussion"]
SEARCH_SOURCE_NAME_MAP = {
    "openai.com": "OpenAI News",
    "blog.google": "Google AI Blog",
    "deepmind.google": "DeepMind Blog",
    "huggingface.co": "Hugging Face",
    "stability.ai": "Stability AI Blog",
    "developer.nvidia.com": "NVIDIA Developer Blog",
}
SEARCH_MODE_QUERIES = [
    "AI model",
    "generative AI",
    "video generation",
    "image generation",
    "game AI",
    "filmmaking AI",
    "digital human",
    "virtual production",
]
SEARCH_TIMEOUT_SECONDS = 9
SERPER_ENDPOINT_NEWS = "https://google.serper.dev/news"
SERPER_ENDPOINT_SEARCH = "https://google.serper.dev/search"

MAX_ITEMS = 6
MIN_ITEMS = 5
RSS_TIMEOUT_SECONDS = 9
MAX_ENTRIES_PER_SOURCE = 18
DIVERSITY_PENALTY = 3
CN_TZ = timezone(timedelta(hours=8))
DATA_PATH = Path("data/summaries.json")
FAVORITES_PATH = Path("data/favorites.json")
LOCK = RLock()
LAST_ERROR = ""

SOURCE_WEIGHTS = {
    "OpenAI": 4,
    "Google AI Blog": 4,
    "NVIDIA Omniverse": 4,
    "Stability AI": 4,
    "DeepMind": 4,
    "Hugging Face": 3,
}

FOCUS_DOMAIN_KEYWORDS = {
    "ai_game": ["game", "gaming", "unreal", "unity", "npc", "gameplay", "游戏"],
    "ai_video": ["video generation", "text-to-video", "video model", "sora", "veo", "视频生成"],
    "ai_film": ["film", "movie", "cinematic", "filmmaking", "animation", "vfx", "studio", "影视", "电影", "短片", "动画"],
    "ai_virtual": ["avatar", "digital human", "virtual production", "3d generation", "数字人", "虚拟制作", "3d"],
}
FOCUS_DOMAIN_BONUS = 4

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

MIXED_CONTENT_SOURCES = {"NVIDIA Omniverse Blog"}

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
    if not FAVORITES_PATH.exists():
        FAVORITES_PATH.write_text("[]", encoding="utf-8")


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


def load_favorites() -> List[Dict[str, str]]:
    ensure_data_file()
    try:
        data = json.loads(FAVORITES_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    except (json.JSONDecodeError, OSError):
        logger.warning("收藏读取失败，将返回空列表")
    return []


def save_favorites(favorites: List[Dict[str, str]]) -> None:
    ensure_data_file()
    try:
        FAVORITES_PATH.write_text(
            json.dumps(favorites, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        logger.warning("收藏保存失败")


def list_archive_dates(mode: str) -> List[str]:
    date_keys = {
        key.split(":", 1)[0]
        for key in CACHE.keys()
        if isinstance(key, str) and key.endswith(f":{mode}")
    }
    return sorted(date_keys, reverse=True)


def resolve_archive_date(selected_date: str, mode: str) -> str:
    available_dates = list_archive_dates(mode)
    if selected_date and selected_date in available_dates:
        return selected_date
    today = datetime.now(CN_TZ).strftime("%Y-%m-%d")
    if today in available_dates:
        return today
    return available_dates[0] if available_dates else today


def get_archive_items(date_key: str, mode: str) -> List[Dict[str, str]]:
    return CACHE.get(f"{date_key}:{mode}", [])



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
        parsed_articles: List[Dict[str, str]] = []
        for entry in feed.entries[:MAX_ENTRIES_PER_SOURCE]:
            summary = (entry.get("summary") or entry.get("description") or "").strip()
            parsed_articles.append(
                {
                    "title": entry.get("title", "无标题"),
                    "url": entry.get("link", "#"),
                    "source": source_name,
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


def extract_source_name_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower().replace("www.", "")
    return SEARCH_SOURCE_NAME_MAP.get(host, host or "未知来源")


def is_allowed_search_result(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower().replace("www.", "")
    path = parsed.path.lower()

    if host in SEARCH_BLOCKED_HOSTS:
        return False
    if any(keyword in path for keyword in SEARCH_BLOCKED_PATH_KEYWORDS):
        return False
    return host in SEARCH_ALLOWED_HOSTS


def parse_serper_results(payload: Dict[str, object], mode: str) -> List[Dict[str, str]]:
    key = "news" if mode == "news" else "organic"
    items = payload.get(key, [])
    if not isinstance(items, list):
        return []

    articles: List[Dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        url = str(item.get("link", "")).strip()
        snippet = str(item.get("snippet", "")).strip()
        if not title or not url:
            continue
        if not is_allowed_search_result(url):
            continue
        published_at = item.get("date") or item.get("datePublished") or item.get("publishedDate")
        parsed_date = parse_entry_time({"published": str(published_at)}) if published_at else datetime.now(timezone.utc)
        articles.append(
            {
                "title": title,
                "url": url,
                "source": extract_source_name_from_url(url),
                "published": parsed_date,
                "raw_summary": snippet,
                "mode": "search",
            }
        )
    return articles


def search_recent_ai_news(limit: int = 50) -> List[Dict[str, str]]:
    serper_key = os.getenv("SERPER_API_KEY", "").strip()
    serper_mode = os.getenv("SERPER_MODE", "news").strip().lower()
    serper_mode = "search" if serper_mode == "search" else "news"
    endpoint = SERPER_ENDPOINT_SEARCH if serper_mode == "search" else SERPER_ENDPOINT_NEWS

    if not serper_key:
        logger.warning("Search模式未配置 SERPER_API_KEY，跳过搜索模式抓取")
        return []

    articles: List[Dict[str, str]] = []
    for query in SEARCH_MODE_QUERIES:
        search_query = f"({query}) (" + " OR ".join(f"site:{site}" for site in SEARCH_MODE_SITES) + ")"
        request_body = {
            "q": search_query,
            "num": 10,
            "gl": "us",
            "hl": "en",
            "tbs": "qdr:d3",
        }
        start = time.monotonic()
        try:
            req = Request(
                endpoint,
                data=json.dumps(request_body).encode("utf-8"),
                headers={
                    "X-API-KEY": serper_key,
                    "Content-Type": "application/json",
                    "User-Agent": "ai-news-digest/1.0",
                },
                method="POST",
            )
            with urlopen(req, timeout=SEARCH_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8", errors="ignore") or "{}")
            hits = parse_serper_results(payload, serper_mode)
            elapsed = time.monotonic() - start
            logger.info("Serper搜索成功 mode=%s query=%s cost=%.2fs hit_count=%d", serper_mode, query, elapsed, len(hits))
            articles.extend(hits)
        except Exception as e:
            elapsed = time.monotonic() - start
            logger.error("Serper搜索失败 mode=%s query=%s cost=%.2fs reason=%s", serper_mode, query, elapsed, repr(e))

    dedup_by_url: Dict[str, Dict[str, str]] = {}
    for item in articles:
        dedup_by_url[item["url"]] = item

    total_hits = len(dedup_by_url)
    filtered = [item for item in dedup_by_url.values() if is_ai_related(item)]
    logger.info("Search模式汇总 total_hits=%d ai_related_count=%d", total_hits, len(filtered))

    ranked = sorted(filtered, key=score_article, reverse=True)
    return ranked[:limit]


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
    search_guard_notice = (
        "当前为搜索模式且缺少正文摘要：仅可做保守背景解读，不要推断具体技术细节、发布事件或量化结果。"
        if article.get("mode") == "search" and not raw_summary.strip()
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
{short_notice} {search_guard_notice}

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
        logger.warning("摘要生成失败：%r", e)
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


def rotate_equal_score_groups(ranked: List[Dict[str, str]]) -> List[Dict[str, str]]:
    if not ranked:
        return ranked
    rotated: List[Dict[str, str]] = []
    idx = 0
    while idx < len(ranked):
        current_score = score_article(ranked[idx])
        end = idx + 1
        while end < len(ranked) and score_article(ranked[end]) == current_score:
            end += 1
        group = ranked[idx:end]
        if len(group) > 1:
            group = group[1:] + group[:1]
        rotated.extend(group)
        idx = end
    return rotated


def select_with_novelty(
    ranked: List[Dict[str, str]],
    target_count: int,
    previous_urls: List[str],
    mode: str,
) -> tuple[List[Dict[str, str]], int, int]:
    selected = select_diverse_articles(ranked, target_count)
    if not selected:
        return selected, 0, 0

    previous_set = {url for url in previous_urls if url}
    if not previous_set:
        return selected, 0, 0

    selected_urls = {item.get("url") for item in selected}
    candidate_pool = [
        item for item in ranked
        if item.get("url") not in previous_set and item.get("url") not in selected_urls
    ]

    replacement_cap = 2 if mode == "search" else 1
    replaced_count = 0

    replaceable = [
        (idx, item) for idx, item in enumerate(selected)
        if item.get("url") in previous_set
    ]
    replaceable.sort(key=lambda pair: score_article(pair[1]))

    for idx, old_item in replaceable:
        if replaced_count >= replacement_cap or not candidate_pool:
            break

        old_score = score_article(old_item)
        min_required = old_score - (1 if mode == "search" else 0)
        replacement_idx = next(
            (
                i for i, candidate in enumerate(candidate_pool)
                if score_article(candidate) >= min_required
            ),
            None,
        )
        if replacement_idx is None:
            continue

        new_item = candidate_pool.pop(replacement_idx)
        selected[idx] = new_item
        replaced_count += 1

    overlap_count = sum(1 for item in selected if item.get("url") in previous_set)
    return selected, overlap_count, replaced_count

def build_daily_digest(force_refresh: bool = False, mode: str = "rss", refresh_reason: str = "auto") -> List[Dict[str, str]]:
    day_key = f"{datetime.now(CN_TZ).strftime('%Y-%m-%d')}:{mode}"
    if not force_refresh and day_key in CACHE:
        return CACHE[day_key]

    try:
        api_key = os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("OPENAI_BASE_URL")

        if mode == "search":
            candidates = search_recent_ai_news(limit=80)
        else:
            candidates = fetch_latest_ai_articles(limit=50)

        previous_items = CACHE.get(day_key, [])
        previous_urls = [
            item.get("url", "")
            for item in previous_items
            if isinstance(item, dict)
        ]

        ranked = sorted(candidates, key=score_article, reverse=True)
        if mode == "search" and refresh_reason == "manual":
            ranked = rotate_equal_score_groups(ranked)

        target_count = min(MAX_ITEMS, max(MIN_ITEMS, len(ranked)))
        selected, overlap_count, replaced_count = select_with_novelty(
            ranked,
            target_count,
            previous_urls,
            mode,
        )
        logger.info(
            "候选与轮换统计 mode=%s candidate_count=%d overlap_with_previous=%d replaced_count=%d",
            mode,
            len(candidates),
            overlap_count,
            replaced_count,
        )

        source_distribution: Dict[str, int] = {}
        for item in selected:
            source = item.get("source", "未知来源")
            source_distribution[source] = source_distribution.get(source, 0) + 1
        logger.info("最终入选来源分布：%s", source_distribution)
        selected_focus_hits = sum(1 for item in selected if count_focus_domain_hits(item) > 0)
        logger.info("最终入选重点领域命中数量：%d/%d", selected_focus_hits, len(selected))

        summary_by_url = build_summary_cache_by_url()
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

            topic = classify_article_topic(item)

            result.append(
                {
                    "title": item["title"],
                    "url": item["url"],
                    "source": item["source"],
                    "topic": topic,
                    "published": item["published"].astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M"),
                    "summary": summary,
                    "mode": mode,
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
    build_daily_digest(force_refresh=True, mode="rss", refresh_reason="scheduled")


@app.route("/")
def index():
    selected_source = request.args.get("source", "all")
    selected_topic = request.args.get("topic", "all")
    selected_mode = request.args.get("mode", "rss")
    selected_view = request.args.get("view", "all")
    selected_archive_date = request.args.get("archive_date", "")

    all_items = build_daily_digest(mode=selected_mode)
    available_sources = sorted({item.get("source", "未知来源") for item in all_items})
    available_topics = ["游戏", "视频生成", "影视生成", "通用 AI"]

    archive_dates = list_archive_dates(selected_mode)
    resolved_archive_date = resolve_archive_date(selected_archive_date, selected_mode)

    favorites = load_favorites()
    favorites_by_url = {item.get("url"): item for item in favorites if item.get("url")}

    if selected_view == "favorites":
        favorites_sorted = sorted(
            favorites,
            key=lambda x: x.get("favorited_at", ""),
            reverse=True,
        )
        items = [
            {
                "title": item.get("title", "无标题"),
                "url": item.get("url", "#"),
                "source": item.get("source", "未知来源"),
                "topic": item.get("topic", DEFAULT_TOPIC),
                "published": item.get("published", ""),
                "summary": item.get("summary", ""),
                "mode": item.get("mode", selected_mode),
            }
            for item in favorites_sorted
        ]
    elif selected_view == "archive":
        items = get_archive_items(resolved_archive_date, selected_mode)
    else:
        items = all_items

    if selected_source != "all":
        items = [item for item in items if item.get("source") == selected_source]
    if selected_topic != "all":
        items = [item for item in items if item.get("topic", DEFAULT_TOPIC) == selected_topic]

    for item in items:
        item["is_favorited"] = item.get("url") in favorites_by_url

    mode_hint = ""
    if selected_mode == "search" and len(items) < MIN_ITEMS and selected_view == "all":
        mode_hint = "Search 模式近 3 天可用高质量结果较少，建议切换到 RSS 稳定模式查看更多资讯。"

    return render_template(
        "index.html",
        items=items,
        updated_at=datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M"),
        error_message=get_last_error(),
        sources=available_sources,
        topics=available_topics,
        selected_source=selected_source,
        selected_topic=selected_topic,
        selected_mode=selected_mode,
        selected_view=selected_view,
        selected_archive_date=resolved_archive_date,
        archive_dates=archive_dates,
        mode_hint=mode_hint,
    )


@app.route("/refresh", methods=["POST"])
def refresh_news():
    selected_source = request.form.get("source", "all")
    selected_topic = request.form.get("topic", "all")
    selected_mode = request.form.get("mode", "rss")
    selected_view = request.form.get("view", "all")
    selected_archive_date = request.form.get("archive_date", "")
    build_daily_digest(force_refresh=True, mode=selected_mode, refresh_reason="manual")
    return redirect(
        url_for(
            "index",
            source=selected_source,
            topic=selected_topic,
            mode=selected_mode,
            view=selected_view,
            archive_date=selected_archive_date,
        )
    )


@app.route("/favorite-toggle", methods=["POST"])
def favorite_toggle():
    selected_source = request.form.get("source", "all")
    selected_topic = request.form.get("topic", "all")
    selected_mode = request.form.get("mode", "rss")
    selected_view = request.form.get("view", "all")
    selected_archive_date = request.form.get("archive_date", "")

    article_url = request.form.get("url", "").strip()
    if not article_url:
        return redirect(
            url_for(
                "index",
                source=selected_source,
                topic=selected_topic,
                mode=selected_mode,
                view=selected_view,
                archive_date=selected_archive_date,
            )
        )

    favorites = load_favorites()
    favorite_index = next((idx for idx, item in enumerate(favorites) if item.get("url") == article_url), None)

    if favorite_index is not None:
        favorites.pop(favorite_index)
    else:
        favorites.append(
            {
                "title": request.form.get("title", "无标题"),
                "url": article_url,
                "source": request.form.get("article_source", "未知来源"),
                "topic": request.form.get("article_topic", DEFAULT_TOPIC),
                "published": request.form.get("published", ""),
                "summary": request.form.get("summary", ""),
                "mode": request.form.get("article_mode", selected_mode),
                "favorited_at": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    save_favorites(favorites)

    return redirect(
        url_for(
            "index",
            source=selected_source,
            topic=selected_topic,
            mode=selected_mode,
            view=selected_view,
            archive_date=selected_archive_date,
        )
    )


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
