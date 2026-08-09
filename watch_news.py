#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自定义主题新闻监控 → 微信推送（PushPlus）+ 聚合页面（按自然周滚动）

整体设计：
  - 微信推送逻辑完全不变：命中就立刻推一条消息，跟最早版本一样，不做任何节流/攒批次，
    也不在消息里附加聚合页面链接。
  - 聚合存档 digest_store.json 按「北京时间周一 00:00」为边界滚动：过了这个时间点，
    上一周的存档清空，从零开始累积本周的新闻。这个边界对齐你现有内容框架的自然周
    （周一到周日，跟你文章里"第32周"这套编号是同一套 ISO 周计算）。
    seen.json（判断新闻是不是"新的"，决定要不要推微信）永远不清空，跟展示用的存档是两回事。
  - 两个展示出口，共用同一份「本周」数据，不会一个是7天滚动一个是自然周，看到的不一致：
    1) docs/index.html —— news-watcher 自己独立域名下的聚合页，通过 GitHub Pages 托管，
       就算以后这个项目脱离 tang3super.github.io 也能独立运作。
    2) site_news_digest.md —— 要推去 tang3super.github.io 仓库 _macro_trade/news-digest.md
       的内容，front matter 里存结构化数据，配合站点那边一个专门的 _layouts/news-digest.html
       用 Liquid 渲染成资讯流样式（不复用叙事文章 article.html 那套排版）。这是一次性的
       站点改动，不是这个脚本管理的文件，脚本只负责覆盖 news-digest.md 这一篇文章。
       这篇文章在"宏观交易"板块列表里跟"下周展望""上周复盘"平级展示，标题固定叫"新闻监控"，
       date 字段每次运行都刷成当天，靠这个自然排到列表最上面，不需要额外写置顶逻辑。
  - 新增翻译：非中文标题/摘要（CNBC、纽约时报、半岛电视台、Investing.com 这些国际源）
    会调用腾讯云机器翻译（TMT）翻译成中文，原文保留在旁边小字，方便你需要时核对英文原文，
    机器翻译不完全可信，涉及具体政策措辞建议回去看原文。只对新并入存档的条目翻译一次，
    已经翻译过的不会重复调用。需要在 GitHub Secrets 里加 TCLOUD_SECRET_ID 和
    TCLOUD_SECRET_KEY；没设置这两个环境变量的话翻译步骤会自动跳过，不影响其余功能。
  - 定性判断（风险加大/减小/模糊）这一步暂不自动化，先留给你在页面上人工判断。

用法：
  1. TOPICS / FEEDS 配置和之前一样，不用动
  2. GitHub Pages 需要手动开一次：仓库 Settings → Pages → Source 选 "Deploy from a branch" →
     branch 选 main，目录选 /docs，保存后几分钟内 https://<你的用户名>.github.io/<仓库名>/ 就能访问
     （必须是 Public 仓库才能用免费的 GitHub Pages）
  3. 翻译需要在仓库 Settings → Secrets and variables → Actions 里新增 TCLOUD_SECRET_ID 和
     TCLOUD_SECRET_KEY（腾讯云控制台 → 访问管理 → API 密钥管理 里获取）
  4. 推去 tang3super.github.io 需要新增 SITE_REPO_TOKEN（fine-grained PAT，只勾选那一个仓库，
     Contents 读写权限），并且 tang3super.github.io 仓库那边要先手动加好 _layouts/news-digest.html
  5. 本地测试：python3 watch_news.py
"""

import os
import re
import json
import html
import hashlib
import feedparser
import requests
import yaml
from datetime import datetime, timedelta, timezone
from tencentcloud.common import credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.tmt.v20180321 import tmt_client, models as tmt_models

# ============ 1. 关键词分组：按自己的研究方向定义，随时增删 ============
TOPICS = {
    "地缘政治": ["霍尔木兹海峡", "Hormuz", "伊朗", "Iran", "以色列", "红海", "Houthi", "胡塞"],
    "货币政策": ["FOMC", "美联储", "Fed", "沃什", "Warsh", "加息", "降息", "CPI", "PCE", "利率决议"],
    "AI基建": ["SK Hynix", "SKHY", "HBM", "英伟达", "Nvidia", "台积电", "TSMC", "AI资本开支", "capex"],
    "短剧/内容出海": ["短剧", "微短剧", "ReelShort", "DramaBox"],
    # 继续加你自己的分组...
}

# ============ 2. RSS 源 ============
INTL_FEEDS = [
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://www.investing.com/rss/news_301.rss",
    "https://www.investing.com/rss/news_25.rss",
]

RSSHUB_BASE = "https://rsshub.app"
CN_FEEDS = [
    f"{RSSHUB_BASE}/cls/telegraph",
    f"{RSSHUB_BASE}/jin10/1",
    f"{RSSHUB_BASE}/gelonghui/live",
    f"{RSSHUB_BASE}/zhitongcaijing/focus",
    f"{RSSHUB_BASE}/sina/finance",
    f"{RSSHUB_BASE}/caijing/roll",
]

FEEDS = INTL_FEEDS + CN_FEEDS

# ============ 3. 聚合页面参数 ============
MAX_ITEMS_PER_TOPIC = 80      # 每个分类最多展示多少条，本周内正常情况下不会碰到这个上限
BEIJING_TZ = timezone(timedelta(hours=8))
REFRESH_NOTE = "每 15 分钟自动刷新"

# ============ 4. 翻译参数（腾讯云机器翻译 TMT） ============
TCLOUD_SECRET_ID = os.environ.get("TCLOUD_SECRET_ID", "")
TCLOUD_SECRET_KEY = os.environ.get("TCLOUD_SECRET_KEY", "")
TCLOUD_REGION = "ap-guangzhou"
CJK_RATIO_THRESHOLD = 0.2     # 标题里中日韩字符占比低于这个值，判定为"非中文，需要翻译"

BASE_DIR = os.path.dirname(__file__)
SEEN_FILE = os.path.join(BASE_DIR, "seen.json")
DIGEST_FILE = os.path.join(BASE_DIR, "digest_store.json")
DOCS_DIR = os.path.join(BASE_DIR, "docs")
DOCS_FILE = os.path.join(DOCS_DIR, "index.html")
SITE_MD_FILE = os.path.join(BASE_DIR, "site_news_digest.md")   # 要推去 tang3super.github.io 的文章

PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")
PUSHPLUS_TOPIC = os.environ.get("PUSHPLUS_TOPIC", "")
PUSHPLUS_URL = "http://www.pushplus.plus/send"


# ---------------- 基础读写 ----------------

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return default
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def load_seen():
    return set(load_json(SEEN_FILE, []))


def save_seen(seen, cap=3000):
    trimmed = list(seen)[-cap:]
    save_json(SEEN_FILE, trimmed)


def entry_id(entry):
    key = entry.get("link") or entry.get("id") or entry.get("title", "")
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def match_topics(text):
    hits = []
    for topic, keywords in TOPICS.items():
        for kw in keywords:
            if kw.lower() in text.lower():
                hits.append(topic)
                break
    return hits


def clean_summary(text, max_len=140):
    """去掉 HTML 标签、解转义、压缩空白，截断成适合阅读的摘要长度。"""
    text = re.sub(r"<[^<]+?>", "", text or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "…"
    return text


def get_entry_time(entry):
    """优先用 feed 自带的发布时间用于排序；拿不到就返回 None，由调用方用抓取时间兜底。"""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


# ---------------- 周边界（北京时间，周一 00:00） ----------------

def week_start_beijing(now_utc=None):
    """返回本周（北京时间，周一为起点）的起始时刻，tz-aware。"""
    now_utc = now_utc or datetime.now(timezone.utc)
    now_bj = now_utc.astimezone(BEIJING_TZ)
    monday_bj = now_bj - timedelta(days=now_bj.weekday())  # Monday.weekday() == 0
    return monday_bj.replace(hour=0, minute=0, second=0, microsecond=0)


def week_label_for(dt_bj):
    iso = dt_bj.isocalendar()
    return f"{iso.year}年第{iso.week}周"


# ---------------- 抓取 + 匹配 ----------------

def fetch_matches(seen):
    matched = []
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[warn] 抓取失败 {url}: {e}")
            continue
        for entry in feed.entries[:30]:
            eid = entry_id(entry)
            if eid in seen:
                continue
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            text = f"{title} {summary}"
            hits = match_topics(text)
            if hits:
                ts = get_entry_time(entry) or datetime.now(timezone.utc)
                matched.append({
                    "id": eid,
                    "title": title,
                    "link": entry.get("link", ""),
                    "topics": hits,
                    "source": feed.feed.get("title", url),
                    "summary": clean_summary(summary),
                    "ts": ts.isoformat(),
                })
            seen.add(eid)
    return matched, seen


# ---------------- 翻译：非中文标题/摘要调腾讯云机器翻译 ----------------

def needs_translation(text):
    """粗略判断：中日韩字符占比很低就认为是英文（或其他非中文），需要翻译。"""
    if not text:
        return False
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    return (len(cjk) / len(text)) < CJK_RATIO_THRESHOLD


_tmt_client = None


def get_tmt_client():
    """懒加载腾讯云 TMT 客户端，没配置密钥就返回 None（调用方据此跳过翻译）。"""
    global _tmt_client
    if _tmt_client is not None:
        return _tmt_client
    if not (TCLOUD_SECRET_ID and TCLOUD_SECRET_KEY):
        return None
    cred = credential.Credential(TCLOUD_SECRET_ID, TCLOUD_SECRET_KEY)
    http_profile = HttpProfile()
    http_profile.endpoint = "tmt.tencentcloudapi.com"
    client_profile = ClientProfile()
    client_profile.httpProfile = http_profile
    _tmt_client = tmt_client.TmtClient(cred, TCLOUD_REGION, client_profile)
    return _tmt_client


def tmt_translate(text):
    """调用腾讯云机器翻译（Source 用 auto 自动识别源语言，Target 固定中文），
    失败返回 None，调用方据此决定是否保留原文。"""
    client = get_tmt_client()
    if client is None or not text:
        return None
    try:
        req = tmt_models.TextTranslateRequest()
        req.SourceText = text
        req.Source = "auto"
        req.Target = "zh"
        req.ProjectId = 0
        resp = client.TextTranslate(req)
        return resp.TargetText
    except TencentCloudSDKException as e:
        print(f"[warn] 腾讯翻译调用失败: {e}")
        return None


def translate_item(item):
    """给单条新闻的标题+摘要分别调一次腾讯云翻译，失败就静默跳过，不影响主流程。"""
    if not (TCLOUD_SECRET_ID and TCLOUD_SECRET_KEY):
        return item
    if not needs_translation(item["title"]):
        return item

    title_zh = tmt_translate(item["title"])
    if not title_zh:
        print(f"[warn] 翻译失败（跳过，仍保留原文）: {item['title'][:40]}...")
        return item

    item["title_zh"] = title_zh
    item["summary_zh"] = tmt_translate(item.get("summary", "")) or ""
    return item


def translate_new_items(matched):
    return [translate_item(item) for item in matched]


# ---------------- 聚合存档：按自然周滚动 ----------------

def update_digest_store(matched, cutoff_bj):
    """把新命中的条目并入存档（按 id 去重覆盖），同时清掉本周开始之前的旧条目。
    返回 (store, changed)：changed 表示存档是否真的发生了变化——
    有新条目并入，或者跨周了、上周的条目被清掉了；纯粹"这轮没抓到新东西"不算变化。
    """
    store = load_json(DIGEST_FILE, {})  # id -> record
    changed = False
    for item in matched:
        if item["id"] not in store:
            changed = True
        store[item["id"]] = item

    before_keys = set(store.keys())
    store = {
        k: v for k, v in store.items()
        if datetime.fromisoformat(v["ts"]).astimezone(BEIJING_TZ) >= cutoff_bj
    }
    if set(store.keys()) != before_keys:
        changed = True

    save_json(DIGEST_FILE, store)
    return store, changed


def group_sorted_by_topic(store):
    by_topic = {t: [] for t in TOPICS}
    for item in store.values():
        for t in item["topics"]:
            if t in by_topic:
                by_topic[t].append(item)
    for t in by_topic:
        by_topic[t].sort(key=lambda x: x["ts"], reverse=True)  # 时间最近的放最前面
        by_topic[t] = by_topic[t][:MAX_ITEMS_PER_TOPIC]
    return by_topic


# ---------------- 出口 1：news-watcher 自己独立域名下的聚合页 ----------------

def render_digest_page(store, week_label):
    by_topic = group_sorted_by_topic(store)
    now_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
    total = sum(len(v) for v in by_topic.values())

    sections = []
    for topic, items in by_topic.items():
        if not items:
            continue
        rows = []
        for it in items:
            local_ts = datetime.fromisoformat(it["ts"]).astimezone(BEIJING_TZ).strftime("%m-%d %H:%M")
            display_title = it.get("title_zh") or it["title"]
            display_summary = it.get("summary_zh") or it["summary"]
            original_block = ""
            if it.get("title_zh"):
                original_block = f'<div class="item-original">原文：{html.escape(it["title"])}</div>'
            rows.append(f"""
        <div class="item">
          <div class="item-time">{local_ts} · {html.escape(it['source'])}</div>
          <a class="item-title" href="{html.escape(it['link'])}" target="_blank" rel="noopener">{html.escape(display_title)}</a>
          <div class="item-summary">{html.escape(display_summary)}</div>
          {original_block}
        </div>""")
        sections.append(f"""
    <section>
      <div class="sect-head"><h2>{html.escape(topic)}</h2><span class="count">{len(items)} 条</span></div>
      {''.join(rows)}
    </section>""")

    body = ''.join(sections) if sections else '<p class="empty">本周暂无命中新闻。</p>'

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>News Watcher · 聚合摘要</title>
<style>
  :root {{
    --bg: #F7F7F5; --panel: #FFFFFF; --ink: #1B1E22; --ink2: #52585F; --ink3: #8A9098;
    --border: #E3E2DD; --accent: #2A6F6B;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Inter','Noto Sans SC',sans-serif; background: var(--bg); color: var(--ink); line-height: 1.6; }}
  .wrap {{ max-width: 720px; margin: 0 auto; padding: 40px 20px 80px; }}
  h1 {{ font-size: 26px; font-weight: 700; margin-bottom: 6px; }}
  .meta {{ font-size: 13px; color: var(--ink3); margin-bottom: 32px; }}
  section {{ margin-bottom: 36px; }}
  .sect-head {{ display: flex; align-items: baseline; justify-content: space-between; border-bottom: 2px solid var(--accent); padding-bottom: 6px; margin-bottom: 14px; }}
  .sect-head h2 {{ font-size: 17px; color: var(--accent); }}
  .count {{ font-size: 12px; color: var(--ink3); }}
  .item {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin-bottom: 8px; }}
  .item-time {{ font-size: 11px; color: var(--ink3); margin-bottom: 4px; }}
  .item-title {{ display: block; font-size: 14.5px; font-weight: 600; color: var(--ink); text-decoration: none; margin-bottom: 4px; }}
  .item-title:hover {{ color: var(--accent); }}
  .item-summary {{ font-size: 13px; color: var(--ink2); }}
  .item-original {{ font-size: 11px; color: var(--ink3); margin-top: 4px; }}
  .empty {{ color: var(--ink3); font-size: 14px; }}
  footer {{ margin-top: 40px; font-size: 11.5px; color: var(--ink3); text-align: center; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>News Watcher · 聚合摘要</h1>
  <div class="meta">{week_label} · 共 {total} 条 · 最后更新 {now_str}（北京时间）</div>
  {body}
  <footer>news-watcher · 每次运行自动重新生成本页面，每周一 00:00（北京时间）清空重新累积</footer>
</div>
</body>
</html>"""


def write_digest_page(html_content):
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(DOCS_FILE, "w", encoding="utf-8") as f:
        f.write(html_content)


# ---------------- 出口 2：tang3super.github.io「宏观交易」板块下的文章 ----------------

def build_site_front_matter(store, week_label, today_str):
    by_topic = group_sorted_by_topic(store)

    topics_data = []
    for topic, items in by_topic.items():
        if not items:
            continue
        items_data = []
        for it in items:
            local_ts = datetime.fromisoformat(it["ts"]).astimezone(BEIJING_TZ).strftime("%m-%d %H:%M")
            entry = {
                "title": it["title"],
                "summary": it["summary"],
                "link": it["link"],
                "source": it["source"],
                "time": local_ts,
            }
            if it.get("title_zh"):
                entry["title_zh"] = it["title_zh"]
            if it.get("summary_zh"):
                entry["summary_zh"] = it["summary_zh"]
            items_data.append(entry)
        topics_data.append({"name": topic, "items": items_data})

    return {
        "layout": "news-digest",
        "title": "新闻监控",
        "date": today_str,
        "summary": f"{week_label}监控",
        "week_label": f"{week_label}监控",
        "last_updated": today_str,
        "refresh_note": REFRESH_NOTE,
        "topics": topics_data,
        "disclaimer": "仅供学习交流，不构成投资建议",
    }


def render_site_markdown(store, week_label, today_str):
    front_matter = build_site_front_matter(store, week_label, today_str)
    yaml_text = yaml.safe_dump(front_matter, allow_unicode=True, sort_keys=False, width=1000)
    return f"---\n{yaml_text}---\n"


def write_site_markdown(md_content):
    with open(SITE_MD_FILE, "w", encoding="utf-8") as f:
        f.write(md_content)


# ---------------- 微信：命中就推，和最早版本一样 ----------------

def group_by_topic(matched):
    grouped = {}
    for item in matched:
        for t in item["topics"]:
            grouped.setdefault(t, []).append(item)
    return grouped


def build_message(grouped):
    lines = [f"**新闻监控 · {datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M')}**\n"]
    for topic, items in grouped.items():
        lines.append(f"### {topic}")
        for it in items:
            lines.append(f"- [{it['title']}]({it['link']})  \n  来源：{it['source']}")
        lines.append("")
    return "\n".join(lines)


def push_to_wechat(title, content_md):
    if not PUSHPLUS_TOKEN:
        print("[warn] 未设置 PUSHPLUS_TOKEN，跳过推送，仅打印结果：")
        print(content_md)
        return
    payload = {
        "token": PUSHPLUS_TOKEN,
        "title": title,
        "content": content_md,
        "template": "markdown",
    }
    if PUSHPLUS_TOPIC:
        payload["topic"] = PUSHPLUS_TOPIC
    resp = requests.post(PUSHPLUS_URL, json=payload, timeout=15)
    print("[push] 状态:", resp.status_code, resp.text[:200])


def main():
    if os.environ.get("FORCE_TEST_PUSH") == "1":
        push_to_wechat(
            "GitHub Actions 测试推送",
            f"这是一条测试消息，发送时间：{datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S')}\n\n如果你在微信收到了这条消息，说明 GitHub Actions → PushPlus → 微信 这条链路完全打通了。"
        )
        return

    seen = load_seen()
    matched, seen = fetch_matches(seen)
    save_seen(seen)

    # 只翻译这轮新命中的条目（已经在存档里的不会再经过这里，不会重复调用 API）
    matched = translate_new_items(matched)

    now_bj = datetime.now(BEIJING_TZ)
    cutoff_bj = week_start_beijing()
    week_label = week_label_for(now_bj)
    today_str = now_bj.strftime("%Y-%m-%d")

    # 存档只在真的变化时（有新条目，或者跨周清空了）才重新渲染两个出口，没变化就跳过
    store, changed = update_digest_store(matched, cutoff_bj)
    if changed or not os.path.exists(DOCS_FILE):
        write_digest_page(render_digest_page(store, week_label))
        write_site_markdown(render_site_markdown(store, week_label, today_str))
        print(f"聚合页面已更新（{week_label}，共 {len(store)} 条）。")
    else:
        print("聚合存档无变化，跳过页面重新生成。")

    if not matched:
        print("本轮没有命中关键词的新条目。")
        return

    # 微信推送：和最早版本一样，命中就立刻推，不做任何节流/攒批次，不带聚合链接
    grouped = group_by_topic(matched)
    content_md = build_message(grouped)
    title = f"新闻监控命中 {len(matched)} 条 · {', '.join(grouped.keys())}"
    print(content_md)
    push_to_wechat(title, content_md)


if __name__ == "__main__":
    main()
