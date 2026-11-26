# 快乐8开奖研究站 · GitHub Pages 版

不需要购买服务器或域名。网页由 GitHub Pages 托管；Python 只在 GitHub Actions 或本机运行，浏览器只读静态数据。本项目仅用于统计研究，不售票、不收款、不保证预测准确。免费方案下，源码仓库和网站均公开。

## 首次发布（推荐用电脑）

1. 登录 GitHub，在 <https://github.com/new> 新建 **Public（公开）**仓库，建议命名 `kl8-observatory`。用下述 Git 命令时不要勾选自动创建 README、许可证或 .gitignore，以免产生不同的提交历史。
2. 解压 `kl8-github-pages-v1.0.zip`，进入内层 `kl8-pages` 目录。要上传的是展开后的**所有项目文件**，不是上传 ZIP 本身。确认仓库根目录有 `pages.py`、`data/`、`static/` 和 `.github/workflows/pages.yml`；最后一个是隐藏目录里的必需文件。
3. 将源码推送到默认分支。电脑有 GitHub CLI 时，可先在本机运行 `gh auth login --hostname github.com --git-protocol https --web`，随后运行 `gh auth setup-git`；也可用已登录的 GitHub Desktop 发布本地仓库。不要把密码或访问令牌贴到聊天、命令行或源码。

   ```bash
   cd kl8-pages
   git init -b main
   git add -A
   git commit -m "Add KL8 GitHub Pages site"
   git remote add origin https://github.com/YOUR_USERNAME/kl8-observatory.git
   git push -u origin main
   ```

   替换 `YOUR_USERNAME`。如果 Git 首次要求设置提交人姓名和邮箱，可在该目录用 `git config user.name` 和 `git config user.email` 配置自己的名字和邮箱。若通过 GitHub Desktop 的“Publish repository”创建仓库，取消“Keep this code private”，不要重复建同名仓库。
4. 打开仓库 **Settings → Pages → Build and deployment → Source**，选 **GitHub Actions**。再进入 **Actions → 快乐8 · 同步并发布 → Run workflow**，选默认分支，首次建议勾选「全量核对官方历史开奖」。若 Actions 被禁用，先在该仓库设置允许 GitHub Actions 及所用的官方 Actions。工作流使用 GitHub 自动签发的 `GITHUB_TOKEN` 写仓库，不用自己创建任何 Secrets。
5. 等 `build` 和 `publish` 都成功，在运行记录或 **Settings → Pages** 打开网址。例如 `https://YOUR_USERNAME.github.io/kl8-observatory/`。站点状态应显示已同步官网，核对最近期号、历史 CSV、4 胆和验奖档案。如果页面显示“最近同步失败”，说明部署本身可能成功，但 GitHub 运行器没能读到官网；打开 **Actions → build → Fetch, settle and render** 查看原因，再手动全量重跑。

## 何时更新、如何锁号

- 北京时间约每小时检查一次官网，21:00–22:59 每 15 分钟安排一次检查，20:17 安排全量核对。GitHub 的排队、网络或 Pages 发布可能延迟；这里是定时运行，不是实时推送。公开仓库长期无活动，定时工作流可能被 GitHub 停用，届时在 Actions 重新启用。
- 初始包含官网历史快照 2,136 期（2020001–2026315）。新一期预测截止时间默认是预计开奖日的北京时间 21:00。晚于截止不补造当期预测；预计日期与休市以官方公告为准。遇将来的官方休市，在 **Settings → Secrets and variables → Actions → Variables** 增加或修改 `KL8_CLOSED`，如 `YYYY-MM-DD:YYYY-MM-DD`，首日含、末日不含，多段逗号分隔。
- 已锁预测与官网更新保存在 `ledger/`。写仓库成功才发布，普通推送冲突会中止，不做强制推送。不要删 `ledger/`、重写默认分支，或在多个独立仓库里各自锁号。若启用了分支保护且工作流无法写默认分支，工作流会失败并保持上次发布；先检查仓库规则。锁号摘要用于核对内容，不是第三方时间戳。
- 随包继承了开奖前保存的第 2026316 期四胆（35、37、61、69）；之后补充的复式和胆拖按锁号时一并保存的完整排名推导，在验奖处会单独标记，不算原始方案。未能按时锁号的期只进入历史回测。
- 全国接口断流会重试；发现历史缺口会尝试切换为全量同步。全国接口拒绝请求或持续失败时，先核对[全国福彩公告首页](https://www.cwl.gov.cn/ygkj/kjgg/)与当期公告；无法接续时再尝试[广东福彩近 100 期开奖](https://www.gdfc.org.cn/lottery/kl8.html?period=100)。网页备用源不冒充全国接口全量核验。备用状态会显示来源和主接口状态码，开奖金额只取当期全国福彩公告，未取得的金额显示未确认。核对发现不同号码时不以猜测覆盖。全国来源为[中国福彩网往期开奖](https://www.cwl.gov.cn/html5/ygkj/wqkjgg/kl8/)，奖级理论用[现行规则](https://www.cwl.gov.cn/c/2025/12/29/472721.shtml)。
- 35 种方案含选一至选十单式和复式、选二至选十胆拖及选四胆全拖的选 5–10。后四种全拖超出单票注数，只用于数学推演。开奖后自动保存命中或未命中及更正版本，并按先预测再观察的顺序回测。不承诺提升独立随机开奖的命中率。

## 本机验证（Python 3.10+ 与系统 tzdata）

```bash
python3 pages.py --offline
python3 -m http.server 8080 --directory _site --bind 127.0.0.1
```

浏览器打开 `http://127.0.0.1:8080/`。不要用 `file://` 直接打开 HTML。离线只构建随包或仓库快照，不访问官网。`python3 pages.py --full` 可联网本地核对但不修改仓库档案；`--persist` 才写入 `ledger/`，仍不会自行提交或推送。不要上传 `_site/`，Pages 会从源码重新构建。

如果 GitHub 运行器长期无法访问官方源，可在能访问的机器上先把工作副本快进到最新默认分支，再执行 `python3 -B pages.py --capture`。只有成功读取官方数据才会写入其核验结果。检查 `git diff` 确认仅预期的 `ledger/` 文件变更后，提交并普通推送，GitHub 便会自动重发。若本次生成了新锁号，必须在输出的 `new_lock_cutoff` 之前推送；超过截止不要推送迟到的新锁。该方式需要人工或另设可信常驻同步机，不会修复 GitHub 与福彩之间的网络限制。

```bash
python3 -B -m unittest discover -s tests -v
node --check static/app.js
node tests/test_frontend.cjs
```

参考：[GitHub Pages 自定义工作流](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages) · [GitHub Actions 定时任务说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。
