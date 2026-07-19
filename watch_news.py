#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自定义主题新闻监控 → 微信推送（PushPlus）

用法：
  1. 在下面 TOPICS 里定义你关心的关键词分组（支持中英文混合）
  2. 在 FEEDS 里配置要监控的 RSS 源
  3. 设置环境变量 PUSHPLUS_TOKEN（去 www.pushplus.plus 微信登录后获取）
  4. 本地测试：python3 watch_news.py
  5. 部署到 GitHub Actions 定时跑（见 .github/workflows/watch.yml）

原理：
  - 拉取各 RSS 源的最新条目
  - 标题+摘要里命中任一关键词组的任一关键词 → 判定为相关
  - 用 seen.json 记录已经推送过的条目链接，避免重复推送
  - 命中的新条目按关键词组分类，一次性推送到微信
"""

import os
import re
import json
import hashlib
import feedparser
import requests
from datetime import datetime, timezone

# ============ 1. 关键词分组：按自己的研究方向定义，随时增删 ============
TOPICS = {
    "地缘政治": ["霍尔木兹海峡", "Hormuz", "伊朗", "Iran", "以色列", "红海", "Houthi", "胡塞"],
    "货币政策": ["FOMC", "美联储", "Fed", "沃什", "Warsh", "加息", "降息", "CPI", "PCE", "利率决议"],
    "AI基建": ["SK Hynix", "SKHY", "HBM", "英伟达", "Nvidia", "台积电", "TSMC", "AI资本开支", "capex"],
    "短剧/内容出海": ["短剧", "微短剧", "ReelShort", "DramaBox"],
    # 继续加你自己的分组...
}

# ============ 2. RSS 源 ============
# 国际源（地缘政治 + 宏观向）
# 注：Reuters 在 2020 年停掉了公开 RSS，网上很多教程里的 reuters 链接现在都是失效的，这里不用
INTL_FEEDS = [
    "https://www.federalreserve.gov/feeds/press_all.xml",       # 美联储官方新闻稿（最权威的货币政策源）
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",    # CNBC 全球市场
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",   # 纽约时报 国际新闻
    "https://www.aljazeera.com/xml/rss/all.xml",                 # 半岛电视台（中东地缘覆盖最全，适合霍尔木兹海峡这类场景）
    "https://www.investing.com/rss/news_301.rss",                # Investing.com - 大宗商品/期货
    "https://www.investing.com/rss/news_25.rss",                 # Investing.com - 外汇
]

# 中文源（通过 RSSHub 公共实例生成，这些站点大多没有官方 RSS）
# 公共实例偶尔会限流/不稳定，用量大或要求稳定的话可以自己 Docker 部署一个 RSSHub 实例替换域名
RSSHUB_BASE = "https://rsshub.app"
CN_FEEDS = [
    f"{RSSHUB_BASE}/cls/telegraph",        # 财联社电报（实时快讯，覆盖面很广）
    f"{RSSHUB_BASE}/jin10/1",              # 金十数据 - 只看重要
    f"{RSSHUB_BASE}/gelonghui/live",       # 格隆汇实时快讯
    f"{RSSHUB_BASE}/zhitongcaijing/focus", # 智通财经 - 要闻
    f"{RSSHUB_BASE}/sina/finance",         # 新浪财经 - 国内
    f"{RSSHUB_BASE}/caijing/roll",         # 财经网 - 滚动新闻
]

FEEDS = INTL_FEEDS + CN_FEEDS

STATE_FILE = os.path.join(os.path.dirname(__file__), "seen.json")
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")
PUSHPLUS_URL = "http://www.pushplus.plus/send"


def load_seen():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen, cap=3000):
    # 防止文件无限增长，只保留最近 cap 条
    trimmed = list(seen)[-cap:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False)


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
                matched.append({
                    "id": eid,
                    "title": title,
                    "link": entry.get("link", ""),
                    "topics": hits,
                    "source": feed.feed.get("title", url),
                })
            seen.add(eid)
    return matched, seen


def group_by_topic(matched):
    grouped = {}
    for item in matched:
        for t in item["topics"]:
            grouped.setdefault(t, []).append(item)
    return grouped


def build_message(grouped):
    lines = [f"**新闻监控 · {datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M')}**\n"]
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
    resp = requests.post(PUSHPLUS_URL, json={
        "token": PUSHPLUS_TOKEN,
        "title": title,
        "content": content_md,
        "template": "markdown",
    }, timeout=15)
    print("[push] 状态:", resp.status_code, resp.text[:200])


def main():
    # 强制测试模式：在 GitHub Actions 里手动跑的时候，把环境变量 FORCE_TEST_PUSH 设成 "1"
    # 就会不管有没有抓到新新闻，都先发一条测试消息，方便确认 GitHub → PushPlus → 微信 这条链路是否打通
    if os.environ.get("FORCE_TEST_PUSH") == "1":
        push_to_wechat(
            "GitHub Actions 测试推送",
            f"这是一条测试消息，发送时间：{datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S')}\n\n如果你在微信收到了这条消息，说明 GitHub Actions → PushPlus → 微信 这条链路完全打通了。"
        )
        return

    seen = load_seen()
    matched, seen = fetch_matches(seen)
    save_seen(seen)

    if not matched:
        print("本轮没有命中关键词的新条目。")
        return

    grouped = group_by_topic(matched)
    content_md = build_message(grouped)
    title = f"新闻监控命中 {len(matched)} 条 · {', '.join(grouped.keys())}"
    print(content_md)
    push_to_wechat(title, content_md)


if __name__ == "__main__":
    main()
