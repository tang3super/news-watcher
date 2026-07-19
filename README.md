# 自定义主题新闻监控 → 微信推送

关键词命中就推微信，完全免费（GitHub Actions 免费额度 + PushPlus 免费额度），个人盯盘用。

## 部署步骤（10分钟内能跑起来）

1. **拿 PushPlus token**
   打开 www.pushplus.plus，微信扫码登录，首页能直接看到"你的token"，复制下来。

2. **建一个新的私有 GitHub 仓库**（比如叫 `news-watcher`），把这几个文件传上去：
   ```
   watch_news.py
   .github/workflows/watch.yml
   ```

3. **配置 Token**
   仓库页面 → Settings → Secrets and variables → Actions → New repository secret
   名字填 `PUSHPLUS_TOKEN`，值填第1步拿到的 token。

4. **改关键词分组**
   打开 `watch_news.py`，改最上面的 `TOPICS` 字典，加你自己关心的词（中英文都行）。

5. **手动测试一次**
   仓库页面 → Actions → News Watcher → Run workflow，点一下，看能不能收到微信推送。

6. 之后就是自动的了，每15分钟跑一次，命中关键词才会推送，不会一直骚扰你。

## 后续可以升级的地方

- **中文源用的是 RSSHub**：公共实例 `rsshub.app` 免费但偶尔限流，用量大/要稳定的话可以自己 Docker 部署一个 RSSHub 实例（官方有现成镜像），把代码里的 `RSSHUB_BASE` 换成自己的域名就行，对你的技术背景来说半小时能搞定。RSSHub 支持的中文财经站点很多（雪球、东方财富、格隆汇、证券时报等），想加别的源直接查 https://rsshub-doc.pages.dev/finance.html
- **换更精准的付费新闻API**：现在是纯关键词匹配，没有语义理解。想要情感分析、实体识别，可以换成 finlight.me 或 apitube.io，只需要改 `fetch_matches()` 这一段。
- **接入 GDELT**：如果想覆盖更冷门的地缘政治事件（小语种媒体报道），GDELT 是免费的全球事件数据库，但噪音比较大，需要自己再加一层过滤逻辑。
- **调整推送频率**：改 `watch.yml` 里的 cron 表达式，比如 `*/5 * * * *` 是每5分钟一次（注意 GitHub Actions 免费额度对私有仓库每月 2000 分钟，跑太频繁可能超额）。
