# GitHub 新手操作指引：把抖音监控跑起来

> 全程在网页上点，不用装 Git。
> 界面是英文的，下面每个按钮都标了英文原名，照着找就行。

---

## 第 1 步：新建私有仓库（2 分钟）

1. 浏览器打开 https://github.com 并登录
2. 点右上角 **「+」** → 选 **New repository**
3. 填写：
   - **Repository name**：随便起，例如 `douyin-monitor`
   - 下面选 **Private**（私有，别人看不到，视频也只有你能看）
   - 其它都别勾（不要勾 Add a README file，避免冲突）
4. 点绿色按钮 **Create repository**

成功后你会看到一个空仓库页面，上面有一行提示 "Quick setup — if you've done this kind of thing before"。

---

## 第 2 步：上传两个文件（3 分钟）

在空仓库页面，找到中间那行蓝色链接 **uploading an existing file**，点它。

1. 打开你电脑上的文件夹：
   `C:\Users\Administrator\WorkBuddy\2026-10-26-16-22-11\douyin-monitor\`
2. 把里面的 **`monitor_run.py`** 和 **`README.md`** 两个文件，直接**拖进浏览器页面**的虚线框里
3. 页面下方 **Commit changes** 输入框随便写一句 `init`，点绿色 **Commit changes**

> 如果看不到虚线框，也可以点 **choose your files** 手动选择这两个文件。

---

## 第 3 步：创建定时任务文件（关键，别跳过）

`.github/workflows/` 这种带点的目录，用上传容易漏，**手工建最稳**：

1. 回到仓库首页，点 **Add file** → **Create new file**
2. 在文件名输入框里，**直接输入完整路径**：
   ```
   .github/workflows/monitor.yml
   ```
   （输入 `/` 时会自动变成目录层级，这是正常的）
3. 打开电脑上的 `douyin-monitor\.github\workflows\monitor.yml`（用记事本打开），
   **全选复制**，粘贴到网页的大文本框里
4. 点右上绿色 **Commit changes** → 再点一次 **Commit changes**

完成后仓库里应该能看到 `.github/workflows/monitor.yml` 这个文件。

---

## 第 4 步：填写 4 个密钥（Secrets）

1. 仓库顶部菜单点 **Settings**（齿轮图标，在 Code / Issues 那一排的最右边）
2. 左侧栏找到 **Secrets and variables** → 点开 → 选 **Actions**
3. 点绿色按钮 **New repository secret**，逐个添加下面 4 个：

| Name（名字，必须一模一样） | Secret（值） |
|---|---|
| `DOUYIN_COOKIE` | 抖音网页版登录后的 Cookie（见下方"怎么拿 Cookie"） |
| `MONITOR_URLS` | 要监控的账号主页链接，例如 `https://www.douyin.com/user/MS4wLjABAAAA...` |
| `SERVERCHAN_SENDKEY` | Server酱的 SendKey（见下方） |
| `DOWNLOAD_CMD` | `cd douk && python main.py` |

每填一个点 **Add secret**，四个都加完。

### 怎么拿抖音 Cookie（重要：一定用小号）
1. 浏览器打开 https://www.douyin.com 并登录
2. 按 **F12** 打开开发者工具 → 点 **Network（网络）** 标签
3. 按 **F5** 刷新页面，随便点一个请求
4. 在右侧 **Headers** 里找到 **Request Headers** → 找到 `Cookie:` 那一行
5. 复制 `Cookie:` 后面**整行内容**，粘贴成 Secret

### 怎么拿 Server酱 SendKey
1. 打开 https://sct.ftqq.com
2. 用微信扫码登录
3. 页面上会显示一串 `SCT...` 开头的 **SendKey**，复制即可
4. 顺手按页面提示"绑定微信"，这样消息才能收到

---

## 第 5 步：开启并手动跑一次（验证）

1. 点仓库顶部 **Actions** 标签
2. 如果出现绿色按钮 **I understand my workflows, go ahead and enable them**，点它
3. 左侧列表点 **douyin-monitor**
4. 右侧点 **Run workflow** → 绿色按钮 **Run workflow** 确认
5. 等十几秒刷新页面，会出现一条运行记录，点进去 → 点 **monitor** → 展开每一步看日志

看到 `==> 监控账号数: 1`、`==> 当前文件总数: ...` 就说明跑起来了。

---

## 第 6 步：之后就不用管了

- 每 6 小时自动跑一次（北京时间 02:00 / 08:00 / 14:00 / 20:00）
- 有新作品 → 自动下载进仓库 `downloads/` 文件夹 → 微信收到推送
- 想立刻试：Actions 页面点 **Run workflow** 手动触发

---

## 常见问题

**Q: 运行记录显示红色叉叉？**
点进去看哪一步出错。常见是 `DOWNLOAD_CMD` 或 Cookie 问题，把日志最后 20 行发给我。

**Q: 微信没收到推送？**
先确认 Server酱已绑定微信；再看日志里是否有 `==> 未配置推送`（说明 SendKey 没填对）。

**Q: 一直提示 Cookie 无效？**
Cookie 过期了（一般几周~几个月），重新按第 4 步拿一次新的即可。

**Q: 会不会花钱？**
不会。私有仓库每月免费 2000 分钟，你每月只用 400~700 分钟。
