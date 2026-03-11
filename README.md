# 每日 AI 资讯摘要（Flask）

一个可本地运行、可部署上线的 AI 资讯网页工具：

1. 每天抓取网络最新 AI 资讯（技术、博客、心得等）
2. 使用 GPT 自动生成中文摘要（每条不超过 300 字）
3. 综合重要性挑选 5-10 条展示
4. 支持网页手动刷新 + 每天上午 8:00 自动刷新
5. 当天摘要持久化保存到 `data/summaries.json`（重启不丢失）
6. RSS 多源抓取带超时与故障隔离，单个源失败不影响整体

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
- **RSS 抓取稳健性**：
  - 不直接 `feedparser.parse(url)`，而是先进行带超时（默认 9 秒）的 HTTP 请求，再交给 `feedparser.parse(content)`。
  - 每个 RSS 源独立 `try/except`，失败源会记录日志但不会中断其他源抓取。
  - 后端日志会输出每个源的成功/失败、耗时、抓取条数与失败原因。
- **排序策略**：综合关键词、时效性、来源权重评分，并加入来源多样性惩罚，减少单一来源长期霸榜。
- **来源权重**（更高代表更优先）：
  - OpenAI / Anthropic / Google AI Blog（高）
  - MIT Technology Review（较高）
- **手动刷新**：页面“手动刷新资讯”按钮可立即重算并覆盖当天结果。
- **来源分布可观测性**：每次刷新都会在后端日志打印最终入选资讯的来源分布，便于排查来源单一问题。
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
