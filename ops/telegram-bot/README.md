# pnc-bot：PNC's Blog 的私人 Telegram 机器人

只听一个人（`PNC_BOT_ALLOWED_USER_ID`，且只在和机器人的私聊里）。用来发随想、建文章草稿、管评论、看状态和部署。Python 3.13，只用标准库 + 系统自带的 Pillow，长轮询（不设 webhook）。这个目录不参与站点构建，也不含任何密钥。

## 结构

| 文件 | 作用 |
| --- | --- |
| `bot.py` | 入口；`python3 -I bot.py`、`--check-config`、`--selftest-command "/status"` |
| `pnc_bot/config.py` | 环境变量 → `Config`，启动时校验；token 从文件读，不进 `repr` |
| `pnc_bot/telegram.py` | Bot API 客户端（urllib，经 `PNC_BOT_PROXY`：默认 NaiveProxy 的 socks5 `socks5h://127.0.0.1:10801`，也支持 http 代理）；建连接 2/3/6 秒限时并换新隧道重试，读超时 12 秒，可安全重发的请求没回应就重发；方法白名单，错误信息不含 token/URL |
| `pnc_bot/auth.py` | 授权判断：`from.id`、`chat.id` 都等于主人且 `chat.type == "private"` |
| `pnc_bot/app.py` | 轮询循环、路由、相册缓冲、确认按钮、git 单线程队列、只读线程池 |
| `pnc_bot/ops.py` | `/status` `/logs` `/deploy` `/restart`：固定 argv，名字只从常量表里取 |
| `pnc_bot/comments.py` | 读评论库（只读连接）、隐藏评论、新评论通知 |
| `pnc_bot/deploys.py` | 读 `deploys.jsonl`，DeployTracker 把部署结果对应到回执 |
| `pnc_bot/state.py` | 自己的 SQLite：offset、评论游标、随想 ↔ 消息映射 |
| `pnc_bot/textfmt.py` `notes.py` `images.py` `gitrepo.py` | 内容：实体转 Markdown、notes.md 编辑、图片重编码、git 提交/推送/撤销 |
| `pnc_bot/linkpreview.py` | 随想里的链接预览：找出独占一行的链接，经代理抓标题、描述、缩略图（YouTube / B 站 / X / GitHub 走各自的接口），写进 `src/data/link-previews.json` |
| `pnc_bot/util.py` | 公用小工具：HTML 转义、去密钥（redact）、时间格式 |
| `tests/` | unittest；`test_integration.py` 是端到端流程（见下文「测试」） |

线程：主线程长轮询；git 队列 1 个线程（所有改仓库的操作串行）；只读命令线程池 2 个（`/status` 不用排在 git 任务后面）；评论轮询（30 秒）和部署跟踪（5 秒）各一个后台线程。

## 用法

- 直接发文字和/或图片 → 新随想。第一行 `# 标题` 或 `标题 | 正文` 可以带标题。相册（最多 10 张）停 2.5 秒后合成一条。
- 回复机器人的回执或自己的原消息 → 追加到那条随想（那条还在处理时也行：会等它发完再追加）。回复别的消息（评论通知、`/status`、草稿回执、发送失败的回执…）不会发布，机器人会提示怎么发。
- 随想里**独占一行的 https 链接**会在页面上变成预览卡片（YouTube、B 站是点击后才加载的播放器，X 可以点开原帖，GitHub 仓库显示星标和语言）。机器人发布时抓一次标题、描述和缩略图，和随想放在同一个提交里，`/undo` 会一起撤回。每条最多处理 3 个链接；抓不到不影响发布，回执里会说一句，页面上只显示地址。句子中间的链接和 `[文字](链接)` 不变。
- `/note 文本`、`/append [id] 文本`（默认追加到机器人最近建的、还在的那条）、`/draft 标题[ | slug]`（下几行是正文，可带图）、`/list`、`/undo`。
- `/comments`、`/delcomment id`（新评论通知上也有「隐藏这条」按钮）。
- `/status`、`/logs [名称]`（`pnc-blog` `pnc-api` `pnc-reactions` `pnc-bot` `runner` `cloudflared` `deploy`）、`/deploy [sha]`、`/restart pnc-blog|pnc-api|pnc-reactions`。

`/undo` `/delcomment` `/deploy` `/restart` 都要点确认：按钮一次有效、120 秒过期、只认主人在私聊里点的。同一个按钮再点一次只会回答“已处理”，不会改掉已经显示的结果。

`/deploy` 的几种情况（确认消息里会写清楚是哪一种）：

- 不带参数：部署 main 的最新提交，**以 deploy.sh 真正开始时的 main 为准**（不传 `DEPLOY_SHA`，deploy.sh 拿到部署锁之后自己解析 `origin/main`）。确认消息里的 sha 只是"现在 main 是什么"；确认之前又推上去的随想不会被旧提交覆盖掉。
- 带 sha、且那个提交还保留着构建好的版本，并且 `state/deploys.jsonl` 里这个版本最后一次部署/回滚是成功的：`deploy.sh --rollback <版本>`，几秒切过去（健康检查不通过时 deploy.sh 会切回原来的版本）。确认之后、开始之前这个版本被清理掉或者又失败过一次的话，改走下一条。
- 带 sha、保留的版本上次没通过检查（或者没有记录）：走 `DEPLOY_SHA=<sha> deploy.sh`，它同样复用这个版本（几秒），但会检查提交在 main 上，健康检查不通过就自动切回原来的版本。
- 带 sha、没有保留的版本：`DEPLOY_SHA=<sha> deploy.sh` 重新构建（约 1 分钟）。

部署在 `systemd-run --user` 的临时单元 `pnc-bot-deploy-<时间>-<请求 id>` 里跑（名字每次都不同），限制和 Actions runner 跑同一个 deploy.sh 时一样：`NoNewPrivileges=yes`、`RestrictSUIDSGID=yes`、`MemoryMax=4G`、`RuntimeMaxSec=1800`（对应 workflow 的 30 分钟超时，超时后 systemd 发 SIGTERM，deploy.sh 记一条失败）。网络和文件系统不做限制：deploy.sh 要从 GitHub 拉代码、用 `~/.local/bin/npm`、写 `~/services/pnc-blog` 和 node-exporter 的 textfile 目录。机器人只有在 `systemctl --user is-active` 明确回答 `inactive`/`failed` 时才认为部署进程结束了；查询超时或连不上用户总线都算"不知道"，不会误报失败。

发出的随想会经历：`⏳ 处理中…` → `📝 已提交 sha，部署中…` → `✅ 已上线`（附链接，先在本机确认页面 200）或 `❌ 部署失败`（附错误和日志尾部）。没人等待的部署失败也会单独发一条通知。

## 安全要点

- 非主人、群组（包括告警群）、频道、编辑过的消息、内联查询等一律静默丢弃，日志只记 `ignored <类型>`；在授权判断之前不下载、不缓冲、不回复。
- 不执行任意命令：所有子进程都是固定 argv，服务名来自常量表，SHA 先过 `^[0-9a-f]{7,40}$` 再交给 git 解析，回滚目标来自 releases 目录列表，而且只回滚到部署记录里最后一次成功的版本；从不 `shell=True`。
- `PNC_BOT_ALLOWED_USER_ID` 没填（示例文件里是空的）或者还是旧示例值 `123456789` 时，机器人拒绝启动，不会默默听别人的。
- 只调用白名单里的 Bot API 方法，永远不会碰 `setWebhook`/`deleteWebhook`/`logOut`/`close`（token 和告警链共用）。
- 发往 Telegram 的日志文本会去掉形如 bot token、GitHub token 的内容；所有动态内容都做 HTML 转义。
- 日志里没有消息正文、文件 id、token 或用户 id。
- 图片一律重编码成不含 EXIF/XMP/ICC 的 WebP（链接缩略图也一样）。
- 抓链接预览时只请求 `https://`、默认端口、带点的域名：IP 字面量、`localhost`、`.local` / `.internal` / `.ts.net` 等内网后缀一律拒绝，每次重定向（最多 3 跳）和缩略图地址都重新检查；不带 cookie，User-Agent 是不含站点信息的通用值；HTML 最多读 512 KB、图片 5 MB，单次请求 8 秒、一个链接总共 20 秒。请求和 Telegram 一样经 `PNC_BOT_PROXY` 出去，域名由代理解析，所以对方看到的是代理出口而不是这台机器，这台机器的本地服务也够不着。唯一的例外是 B 站：它对代理出口一律回 412，所以 `linkpreview.DIRECT_HOSTS` 里那几个固定域名（`api.bilibili.com`、`www.bilibili.com`、`b23.tv` 等）不经代理直连，照常校验证书。抓回来的文字只当纯文本存，站点渲染时转义。

## 配置与部署

```bash
# 代码（部署副本）
rsync -a --delete ops/telegram-bot/ personal:services/pnc-infra/bot/

# 配置：只有用户 id 必填，其余见注释里的默认值
install -d -m 700 ~/.config/pnc-bot
install -m 600 ~/services/pnc-infra/bot/config.env.example ~/.config/pnc-bot/config.env  # 仅首次（会覆盖）
$EDITOR ~/.config/pnc-bot/config.env  # 必须填 PNC_BOT_ALLOWED_USER_ID（你自己的数字 id），空着不能启动
mkdir -p ~/.local/state/pnc-bot && chmod 700 ~/.local/state/pnc-bot

# 机器人自己的 clone：只用写权限的 deploy key（~/.config/pnc-bot/deploy_key，0600）推送。
# github.com 的主机密钥要先在 ~/.ssh/known_hosts 里（和 GitHub 公布的指纹核对过）。
SSH_CMD="ssh -F /dev/null -i /home/lyy/.config/pnc-bot/deploy_key -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/home/lyy/.ssh/known_hosts -o ConnectTimeout=20 -o ServerAliveInterval=30 -o ServerAliveCountMax=3"
install -d -m 700 ~/services/pnc-bot
GIT_SSH_COMMAND="$SSH_CMD" git clone git@github.com:Boshugege/boshugege.github.io.git ~/services/pnc-bot/repo
git -C ~/services/pnc-bot/repo config core.sshCommand "$SSH_CMD"
git -C ~/services/pnc-bot/repo config user.name pnc-bot
git -C ~/services/pnc-bot/repo config user.email pnc-bot@parityncsvt.top
git -C ~/services/pnc-bot/repo config core.hooksPath /dev/null

# 先自检（--selftest-command 不读 token、不连 Telegram，只允许 /help /status /logs /list /comments；/list 会先 fetch 机器人的 clone）
set -a; . ~/.config/pnc-bot/config.env; set +a
python3 -I ~/services/pnc-infra/bot/bot.py --check-config
python3 -I ~/services/pnc-infra/bot/bot.py --selftest-command "/status"

# 服务
install -m 644 ~/services/pnc-infra/bot/pnc-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now pnc-bot
journalctl --user -u pnc-bot -f
```

第一次启动会先丢掉 Telegram 里积压的旧更新，然后才开始处理。`409 Conflict` 说明还有别的进程在 `getUpdates`，机器人会每 30 秒重试，不会去删 webhook。

停止或重启（SIGTERM）时，机器人会在几秒内尽量把没做完的事改成提示：排队中的随想/操作 → “机器人重启了，这条没有处理，请重新发送”；正在处理的 → “可能没处理完，先 /list 或 /status 看看”；已提交还在等部署的 → “没等到部署结果，用 /status 看看”；缓冲中的相册 → 回复提示重发。重启后不会重放任何东西。推送时连接断开或超时，机器人会先 fetch 确认远端是否已经收下这个提交，收下了就按成功处理。发往 Telegram 的最终结果（回执更新、失败通知）遇到网络错误、5xx 或 429 会在 2、5、15 秒后重试。

`core.sshCommand` 里的 `-F /dev/null` 不能省：`pnc-bot.service` 是带 `ProtectSystem=strict` 的用户单元，systemd 为它建了用户命名空间，root 拥有的 `/etc/ssh/ssh_config.d/*.conf` 在里面显示成 `nobody` 所有，ssh 认为这些配置文件属主不对而直接拒绝运行（在普通 shell 里 fetch/push 正常，只有服务里失败）。`-F /dev/null` 让 ssh 不读任何配置文件，所以需要的选项（密钥、`IdentitiesOnly`、`BatchMode`、严格主机密钥检查和 known_hosts 路径、超时）都写在命令里。改过之后在机器人里发一次 `/list`（它会先 fetch）确认服务里的 git 能连上 GitHub。

## 测试

```bash
rsync -a --delete ops/telegram-bot/ personal:/tmp/pnc-bot-test/
ssh personal 'cd /tmp/pnc-bot-test && python3 -m unittest discover -s tests -t .'
```

测试全部离线：Telegram、子进程、git 远端都是假的或临时本地仓库；评论库用真实表结构的临时 SQLite。需要 Python 3.13（Mac 上的 3.9 太旧）。

`tests/test_integration.py` 把真实的 `App`（含工作线程）、`TelegramClient`、`State`、`gitrepo.Repo`、Pillow 图片处理和 `DeployTracker` 接在一起，只把 Bot API 的 HTTP 层换成假的（记录每次调用、提供文件下载），git 远端是临时 bare 仓库。覆盖：带 EXIF/GPS 的照片 → 随想、相册、回复追加、`/append`、`/draft`、两次 `/undo`、陌生人/群组/频道全部无效、确认按钮的重放/过期/伪造、推送竞争后重算 id、部署成功（后代提交）/失败后回执的变化、首次启动丢弃积压、回复仍在推送的回执、回复非随想消息不发布、撤销后 id 复用、推送断开但已落地。
