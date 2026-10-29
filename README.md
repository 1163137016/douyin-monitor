# 抖音账号监控（无头下载版）

监控指定抖音账号，发现**新作品**（视频 / 图文）就自动下载到本地，并可推送到微信（Server酱）。
支持本地手动运行、Windows 任务计划定时运行，也可用于 GitHub Actions。

## 架构说明（重要）

DouK-Downloader 的"终端交互模式"依赖**剪贴板监听**（同步阻塞调用），在无桌面/无 TTY 的
环境（沙箱、CI、定时任务）里会卡死事件循环且无法干净退出。因此本项目**不调用 DouK 的终端 UI**，
而是直接复用 DouK 的反爬签名模块 `douk/src/encrypt/douyin_params.py`（生成 `a_bogus` / `web_sign`），
自行完成完整链路：

```
签名请求 aweme/v1/web/aweme/post 接口
  → 翻页拉取账号作品列表
  → 按类型取直链（视频 mp4 / 图文 images 图集）
  → 与 state.json 对比找出新作品
  → 下载到 downloads/<昵称>/<发布时间>_<文案>/<作品文件夹>
  → 保存 文案.txt（发布时间/文案/互动数据/作品链接）
  → Server酱推送微信（可选）
  → 干净退出（适合定时任务）
```

> `douk/` 目录仍需保留（只用到它的签名模块），依赖也仍按 `douk/requirements.txt` 安装。

## 配置

### 本地运行：编辑 `.env`（已被 .gitignore 排除，不会上传）

```ini
DOUYIN_COOKIE=你的抖音cookie整串        # 必填，务必用小号
MONITOR_URLS=https://www.douyin.com/user/MS4wLjABAAAAxxx   # 必填，多个逗号分隔
SERVERCHAN_SENDKEY=SCTxxxxxxxx          # 可选，去 sct.ftqq.com 免费领取，推送微信
MAX_PAGES=5                             # 可选，每次最多翻几页，默认 5
DOWNLOAD_DIR=downloads                  # 可选，下载根目录
```

### GitHub Actions 部署

仓库已内置无头监控脚本和 DouK 的签名模块（`douk/src`），无需完整 DouK。

1. 把本仓库推到 GitHub（注意 `.env`、`downloads/`、`state.json` 已被 `.gitignore` 排除，**不会上传**）；
2. 在仓库 `Settings → Secrets and variables → Actions → New repository secret` 添加：
   | Secret 名称            | 必填 | 说明                                       |
   | -------------------- | --- | ------------------------------------------ |
   | `DOUYIN_COOKIE`      | ✅   | 抖音小号 Cookie                            |
   | `MONITOR_URLS`       | ✅   | 账号主页链接，逗号分隔                       |
   | `SERVERCHAN_SENDKEY` | ⬜   | Server酱 SendKey                           |
   | `MAX_PAGES`          | ⬜   | 默认 5                                     |
3. `Actions` 标签页启用工作流；会**每 6 小时自动跑**（`cron: 0 */6 * * *`，UTC），也可手动点 `Run workflow` 测试。
4. 增量基准 `state.json` 与已下载文件 `downloads/` 通过 **Actions Cache** 在两次运行间持久化（缓存上限 10GB），所以云端也会保留视频/图片。

> ⚠️ **云端 IP 风险**：GitHub 运行在微软云数据中心 IP，抖音可能对这类机房 IP 做风控/拦截——本地能跑通不代表云端一定能拉到数据。务必先手动 `Run workflow` 跑一次看日志确认。
> ⚠️ 缓存非永久：长期不跑可能被回收，之后会重新全量回填（被微信轰炸一次）。如需长期可靠，本地电脑常开更稳。

## 运行（本地）

```bash
# 依赖安装（首次）
pip install -r requirements.txt

# 运行（本地读 .env）
python monitor_run.py
```

首次运行会把近期作品全部当作"新作品"回填下载（正常）；之后每次只下载增量。

## 下载目录结构（每个作品一个文件夹）

```
downloads/
└── 账号昵称/
    ├── 20261029_153000_文案前30字/     ← 文件夹名 = 发布时间_文案
    │   ├── 视频.mp4                     ← 视频作品
    │   ├── 01.jpg 02.jpg ...           ← 图文作品按顺序编号
    │   └── 文案.txt                     ← 发布时间/完整文案/点赞评论收藏/作品链接
    └── ...
```

## 增量与推送原理

1. `state.json` 记录已见过的 `aweme_id` 列表；
2. 每次运行拉取最近 `MAX_PAGES` 页作品，不在列表里的即"新作品"；
3. 新作品按类型下载到独立文件夹（见上方目录结构），并自动保存 `文案.txt`；
4. 有新作品时通过 Server酱 推送标题+清单到微信；无新作品只打印日志。

## 注意事项

- **Cookie 会过期**：几周~几个月失效；推送/下载突然没动静时先手动跑一次看日志。
- **账号安全**：务必用小号 Cookie，主号有风控连坐风险。
- **签名算法**：复用 DouK 的 `DouYinParams`；若抖音改接口导致拉取失败，更新 `douk/` 里的签名模块即可，主脚本不用动。
- **合规**：下载内容自存自用，请勿二次搬运发布他人作品。
- **沙箱/代理环境**：若本机代理拦截 douyin.com，脚本已内置自动绕过代理直连（普通电脑无影响）。
