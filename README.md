# 每日 AI 资讯摘要（Flask）

一个可本地运行、可部署上线的 AI 资讯分析与解读工具：

1. 每天抓取网络最新 AI 资讯（技术、博客、心得等）
2. 使用 GPT 生成高密度中文分析摘要（每条不超过 300 字）
3. 综合重要性挑选 5-6 条展示
4. 支持网页手动刷新 + 每天上午 8:00 自动刷新
5. 当天摘要持久化保存到 `data/summaries.json`（重启不丢失）
6. RSS 多源抓取带超时与故障隔离，单个源失败不影响整体

默认订阅源：OpenAI News、Google AI Blog、Hugging Face Blog、NVIDIA Omniverse Blog、Adobe AI Blog、Stability AI Blog。

说明：Runway Blog 的 RSS 链接当前无效（404），已从默认源中移除。

本项目当前特别关注 AI 在游戏、视频生成、影视/短片制作、动画与数字人工作流中的应用资讯。

## 一、本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 可选：编辑 .env，填入 OPENAI_API_KEY
python app.py
```

浏览器打开：`http://127.0.0.1:5000`

## 二、生产部署（Gunicorn）

> 已在 `requirements.txt` 中包含 `gunicorn`。

### 1) 准备环境

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 生产建议：FLASK_DEBUG=false，并按需配置 OPENAI_API_KEY
```

### 2) 启动服务

```bash
gunicorn -w 2 -k gthread --threads 4 -b 0.0.0.0:5000 app:app
```

说明：
- `-w 2` 表示 2 个 worker（可按机器配置调整）。
- `--threads 4` 表示每个 worker 4 线程。
- 如需反向代理，可在 Nginx/Caddy 前挂载该端口。

## 三、环境变量说明（.env）

项目启动时会自动读取 `.env`（通过 `python-dotenv`）。

### 使用第三方 OpenAI 兼容接口
如果使用第三方兼容 OpenAI 的服务，需要在 `.env` 中配置：

OPENAI_API_KEY=你的密钥
OPENAI_BASE_URL=https://api.gptsapi.net/v1
OPENAI_MODEL=gpt-4o-mini

注意：部分兼容接口需要使用 `/v1` 路径，否则可能返回 404。
- `FLASK_RUN_HOST`：可选，默认 `0.0.0.0`。
- `FLASK_RUN_PORT`：可选，默认 `5000`。
- `FLASK_DEBUG`：可选，默认 `false`（更适合部署环境）。

可直接参考 `.env.example`。

## 四、核心能力说明

- **自动任务**：使用 `APScheduler` 每天北京时间 **08:00** 自动抓取并生成当天摘要。
- **持久化**：摘要写入 `data/summaries.json`，服务重启后仍可读取。
- **去重策略**：
  - 先按 URL 去重；
  - 再按标题相似度去重（避免多站转载重复展示）。
- **来源策略**：优先选择“稳定可抓取 + 内容相关”的 RSS 源，重点覆盖 AI 游戏/NPC/虚拟世界、文生视频/影视生成、动画/数字人/VFX/虚拟制作。
- **RSS 抓取稳健性**：
  - 不直接 `feedparser.parse(url)`，而是先进行带超时（默认 9 秒）的 HTTP 请求，再交给 `feedparser.parse(content)`。
  - 每个 RSS 源独立 `try/except`，失败源会记录日志但不会中断其他源抓取。
  - 多个 RSS 源并发抓取（非串行），进一步降低手动刷新总耗时。
  - 后端日志会输出每个源的成功/失败、耗时、抓取总数、AI 过滤后数量与失败原因。
  - 为避免单次刷新过慢，每个 RSS 源仅处理最近 18 条 entries。
- **刷新性能优化**：
  - `MAX_ITEMS` 下调为 6（`MIN_ITEMS` 仍为 5），减少单次需要生成的新摘要数量。
  - 若同 URL 文章已在内存缓存或 `data/summaries.json` 中存在摘要，将直接复用，避免重复调用模型。
- **AI 相关性硬过滤（严格版）**：系统会先执行 `is_ai_related(article)`，仅保留明确 AI 相关资讯；未命中（如普通电影推荐、泛影视教程）会在排序前直接排除。
  - 英文强关键词使用正则“单词边界”匹配（避免 `ai` 误命中普通单词片段）。
  - 弱关键词（如 `ai`）不能单独成立，必须与创意/生成上下文词（如 `game/video/film/movie/animation` 或 `游戏/影视/电影/短片/动画/视频`）共同命中。
  - 对混合来源（NVIDIA Omniverse Blog、Adobe AI Blog）执行更严格规则：必须命中至少一个强 AI 关键词，才能进入候选池。
- **排序策略**：综合关键词、时效性、来源权重评分，并加入来源多样性惩罚，减少单一来源长期霸榜。
- **重点领域关注（加权优先）**：在“综合 AI 资讯”前提下，对以下方向追加关键词加分：
  - AI + 游戏（如 `game/gaming/unreal/unity/npc/gameplay/游戏`）
  - AI + 视频生成（如 `video generation/text-to-video/video model/sora/veo/视频生成`）
  - AI + 影视/电影/短片生成（如 `film/movie/cinematic/filmmaking/animation/vfx/studio/影视/电影/短片/动画`）
  - AI + 动画/3D/数字人/虚拟制作（如 `avatar/digital human/virtual production/3d generation/数字人/虚拟制作/3d`）
- **来源权重**（更高代表更优先）：
  - OpenAI / Google AI Blog（高）
  - NVIDIA Omniverse（高），Adobe AI / Stability AI / Hugging Face（较高）
- **手动刷新**：页面“手动刷新资讯”按钮可立即重算并覆盖当天结果。
- **来源分布可观测性**：每次刷新都会在后端日志打印最终入选资讯的来源分布，便于排查来源单一问题。
- **重点领域命中可观测性**：后端日志会额外打印最终入选资讯中命中重点领域关键词的数量。
- **资讯分类字段**：每条入选资讯会包含 `topic` 字段，取值为 `游戏 / 视频生成 / 影视生成 / 通用 AI`，便于后续前端做分类筛选。
- **分析型摘要（非简单翻译）**：摘要会强调“核心内容 + 技术背景 + 原理机制 + 行业影响”；当原文很短时可做合理背景扩展，但不编造具体事实。
- **前端筛选**：支持按来源与 `topic`（全部 / 游戏 / 视频生成 / 影视生成 / 通用 AI）组合筛选资讯。
- **时间字段兼容**：当 RSS 条目缺少 `published` 时，会回退到 `updated/pubDate/created`，再尝试 `*_parsed` 字段；仍缺失时使用当前时间，避免直接丢弃。

## 五、目录结构

```text
.
├── app.py
├── requirements.txt
├── .env.example
├── data/
│   └── summaries.json
├── templates/
│   └── index.html
└── static/
    └── style.css
```
