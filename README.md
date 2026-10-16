# Reddit 热帖雷达（Reddit → 二创 → 手动发 X）

每天盯住少量关注社区的**最新**帖子，剔除垃圾/广告，按**评论数**判断哪些会成为爆款，自动生成二创文案和配图，复制后自己去 X 发布。

## 快速开始

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv -r requirements.txt
cp .env.example .env          # 不填 Reddit 凭证即为演示模式
.venv/bin/uvicorn app.main:app --port 8799
```

打开 http://localhost:8799 。运行测试：`.venv/bin/python -m unittest discover -s tests -v`

## 流程

1. **拉新帖**：启用社区每 20 个合并成一次 `/r/a+b+c/new?limit=100`，与已知帖子重叠即停止翻页。
2. **过滤垃圾/广告**（0 次额外调用，只用列表里自带的字段）：广告投放、置顶/版务帖、广告关键词、精确推广域名匹配、
   flair 黑名单、固定讨论串、正文外链过多、标题全大写/过短、同作者 24h 刷屏；刷新后好评率过低也会被剔除。
   被过滤的帖子立即停止跟踪，不再消耗额度。规则都可在「设置」里改。
3. **跟踪评论数**：到期帖子用 `/api/info?id=...` 每 100 条一次批量刷新。**不抓评论区。**
   帖龄 < 2h 每 15 分钟、2–6h 每 30 分钟；超过 2h 仍冷门的只留一次最终快照；默认只跟踪 12h；过期快照与缓存自动定期清理。
4. **爆款打分（0–100）**：评论数 ÷ 该社区同帖龄评论 P90 + 最近评论增速 + 同帖龄高赞加成，自动标注「🔥 高赞共识」与「⚔️ 高争议话题」。
5. **候选与去重**：热度 ≥ 阈值、帖龄 ≤ 3h、评论 ≥ 最低值、非 NSFW、24h 内不与已有候选标题或规范化外链 URL 重复（含同批次跨社区去重）。
6. **二创文案**：外链帖自动抓取网页摘要补充背景（0 Reddit 额度），LLM 重写（支持自定义账号人设与 280/600/1500 字数上限）；无 LLM 时用模板。
7. **配图工作台**：原帖自带图片 + 支持上传/直接粘贴本地图片 + 3 种卡片模板 × 3 套配色主题（深邃黑/黑金琥珀/极简白）× 3 种尺寸，
   一键复制图片或下载 PNG；可选配置 `IMAGE_MODEL` 用 AI 生成配图。
8. **一键去 X 发布 & 主动通知**：支持「复制配图 + 去 X 发帖 ↗」直接唤起预填好文案的 X 发帖框，按 `Cmd/Ctrl+V` 粘贴图片即可发布；支持浏览器桌面通知与飞书/企微/Discord/Bark/Slack Webhook 推送。

## 调用预算

`budget.py` 按「剩余额度 ÷ 剩余天数」算日预算，所有 Reddit 数据请求记入 `api_usage`；低优先级请求在日预算用到 80% 后跳过。
5 个社区、10 分钟轮询的默认配置约 480 次/天 ≈ 1.4 万次/月。

## 目录

```
app/
  config.py         环境变量 / 凭证
  db.py             SQLite 表结构 + 迁移 + 运行时设置
  budget.py         额度统计与限流
  reddit_client.py  Reddit OAuth 客户端 + 图片提取
  spam.py           垃圾/广告过滤
  demo.py           确定性模拟器（演示模式）
  collector.py      增量采集 + 批量刷新 + 刷新节奏
  scoring.py        社区基线 + 评论热度评分 + 去重
  generator.py      二创文案（LLM / 模板）+ X 字符权重
  images.py         图片代理（域名白名单）+ AI 生图
  pipeline.py       候选 → 二创文案
  scheduler.py      APScheduler 定时任务
  main.py           FastAPI 接口 + 静态前端
  static/           前端（原生 JS，无构建步骤；studio.js 为配图工作台）
tests/test_core.py
```

## 注意

- 原帖图片可能有版权，商用前请确认；AI 生成图片或纯文字卡片更安全。
- 遵守 Reddit Data API 条款（商业用途、删帖同步）。
