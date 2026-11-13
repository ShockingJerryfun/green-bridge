# 蓝绿协作监控台

本地网页控制台，重点回答两个问题：**实际发到远程服务器的是什么命令**、**结果和原始输出是什么**。

有两个视图：

- `http://127.0.0.1:8899/`：任务视图，适合翻历史、按任务判读
- `http://127.0.0.1:8899/chat`：对话视图，命令以聊天气泡展示，底部输入框 + 环境选择器

另带一个命令构造器：选择目标环境、只输入远程命令（如 `npu-smi info`），自动包装成"绿区 SSH 到目标环境"的完整调用。

## 启动

先准备两个本机文件（都不进 Git）：

```bash
cp dashboard/servers.example.json local/servers.json   # 填你的目标环境
cp assets/blue_config.example.json local/blue.json     # 填你自己的传输仓库
```

然后：

```bash
bash dashboard/start.sh
```

默认打开 `http://127.0.0.1:8899`。常用参数：

```bash
bash dashboard/start.sh --port 8899 --no-open      # 不自动开浏览器
bash dashboard/start.sh --interval 5               # 每 5 秒同步一次
bash dashboard/start.sh --config /path/to/blue.json
```

Windows 下直接运行：

```powershell
python dashboard\dashboard.py --config local\blue.json
```

## 阅读重点

- **实际远程命令**：SSH 包装会显示它真正在目标机执行的脚本；`echo <base64> | base64 -d | bash` 会自动解码，多跳脚本会标出还会连接哪些主机
- **传输层命令**：绿区实际启动的进程（含 SSH 参数）默认折叠，需要核对时再展开
- **状态与输出**：退出码、耗时、stdout / stderr 分栏，支持下载原始字节
- **任务列表**：可筛选；旧任务没有中文说明时，用"目标 + 脚本标题"生成可读摘要
- **协同区**：本机备注、预留的审批 / 多绿区 / 模板位置

## 对话视图

`/chat` 是给日常使用的：

- 上面是消息流：你的命令是右侧气泡，绿区执行结果（状态、退出码、耗时、stdout/stderr）是左侧卡片
- 长命令默认只显示前三行，点"展开完整命令"看全文
- 最新的几条结果会自动加载输出，其余点"查看输出"
- 底部输入框：Enter 发送，Shift+Enter 换行
- 左下角的环境选择器就是"模型选择器"的位置：选它等于选这次命令从绿区 SSH 到哪台目标机

注意：这是**命令对话**，输入的内容会当 shell 命令执行，不是自然语言聊天。输入 `npu-smi info` 可以，输入"帮我看看卡"会被当成一条命令原样发过去（大概率失败）。如果要让它理解自然语言，需要另接一个模型作为 agent 后端。

## 保持目录（仿 SSH 的第一步）

对话页和命令构造器都有"保持目录"开关（默认开）。打开后：

- 每条命令会先 `cd` 到这台目标机上一次的目录，再执行你的命令
- 执行完把新的 `pwd` 写回目标机的 `~/.green-bridge/cwd-<id>`
- 所以 `cd /path` 之后，下一条 `ls`、`cat xxx.sh` 都在 `/path` 下
- 状态按"目标机 + 用户"共享，换设备/刷新页面不会丢

它**只保持工作目录**，不保持其他东西：

- 命令必须**按顺序执行**：对话页会自动排队，等上一条完成才发下一条；任务页的构造器不会排队，发第二条前请确认第一条已完成，否则 `cd` 和 `ls` 会并发抢同一个目录
- `export FOO=bar` 下一条不会生效（每条命令仍是独立进程）
- shell 函数、alias、`cd -` 的历史、后台任务都不会保留
- `vim`、`top`、`less`、需要输入密码的程序仍然用不了（没有 PTY）
- 想让目录回到 `~`，发一条 `cd ~` 即可

`cat 脚本`、`ls`、`grep`、`tail` 这类非交互命令都正常。

## 命令构造器

1. 在右侧选择环境（来自 `local/servers.json`）
2. 只填远程要执行的命令，例如 `npu-smi info`
3. 点"生成命令"，会显示实际远程命令和完整请求 JSON
4. 可选：
   - **复制 JSON**：交给蓝区代理派单
   - **保存待派单**：写到 `local/outbox/`，之后对代理说"发出去"
   - **派单到绿区**：显式确认后调用蓝区 `green_bridge.py submit` 写入传输仓库

SSH 是从**绿区**发起的，所以免密登录、密钥、known_hosts 都留在绿区；这里只负责拼参数和传命令，不需要也不会收集密码。

`local/servers.json` 示例：

```json
{
  "servers": [
    {"label": "target-1", "user": "root", "host": "target-1.example.com"}
  ],
  "ssh_options": ["-T", "-n", "-o", "BatchMode=yes",
                  "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8"],
  "cwd": "C:\\",
  "default_timeout_seconds": 180
}
```

## 派单上下文（不调用模型）

派单时可以自动附带"这单之前用户和 agent 聊了什么"：

- 后台直接读本机最近的 Kimi Code 会话日志（`~/.kimi-code/sessions/*/session_*/agents/main/wire.jsonl`），提取最近几条用户/助手消息
- 不做摘要、不调用任何模型；原文截断后原样写入请求的 `context` 字段
- 非 Kimi Code 环境读不到会话时，这部分自动为空，不影响其他功能
- 命令构造器里有"附带最近对话上下文"开关，另有"补充说明"输入框；两者都会随请求写入
- 任务详情里会显示"派单上下文"，点开可看当时为什么派这单

给 agent 用时，可以命令行生成同样的结构：

```bash
python3 dashboard/context.py --limit 6 --note "这次只读" > context.json
```

注意：`context` 会进入传输仓库，对话里不要放凭据；不想带对话时取消勾选，或只填补充说明。

## 只读保证与例外

监控本身不会影响正在进行的任务：

- 使用自己独立的 bare clone：`<蓝区 git_dir 所在目录>/monitor-control.git`
- 对传输仓库只执行 `git fetch`，从不提交、不推送、不修改分支
- 不碰蓝区客户端使用的 `blue-control.git` 和状态目录
- 备注只写本机 `local/dashboard-notes.json`

**唯一的写入路径**是"派单到绿区"按钮：它调用蓝区客户端提交请求，与手动派单等价。不点这个按钮就不会写入传输仓库。

## 数据来源

直接读取传输仓库的两个分支：

- `pingpong/task-requests/<任务ID>.json` + `pingpong/task-results/<任务ID>.json`（聚合模式）
- `pingpong/requests/<ID>.json` + `pingpong/results/<ID>.json`（旧格式）
- `pingpong/outputs/<内部ID>/<stream>.NNNNNN`（原始输出分段）

输出在服务端按清单逐段读取并校验 SHA-256，和 `output` 命令的校验口径一致。

## 常驻运行

先前台确认可用，再按需用 launchd 托管：

```bash
launchctl submit -l greenbridge-dashboard \
  -o "$HOME/.kimi-code/logs/dashboard.out" \
  -e "$HOME/.kimi-code/logs/dashboard.err" \
  -- /usr/bin/python3 "$HOME/.kimi-code/skills/green-bridge/dashboard/dashboard.py" \
     --config "$HOME/.kimi-code/skills/green-bridge/local/blue.json" --port 8899 --no-open
```

停止：

```bash
launchctl remove greenbridge-dashboard
```

## 接口

| 路径 | 用途 |
|---|---|
| `GET /api/state` | 任务摘要（轮询用，体积小） |
| `GET /api/version` | 分支头与同步状态（极轻量轮询） |
| `GET /api/task?task=` | 单个任务完整详情 |
| `GET /api/recent?limit=` | 最近任务完整详情（对话页用） |
| `GET /api/output?task=&cmd=&stream=` | 输出文本（最多 128 KB） |
| `GET /output?task=&cmd=&stream=&download=1` | 原始输出下载 |
| `GET /api/servers` | 本机环境列表 |
| `GET /api/context?limit=` | 最近会话上下文（不调模型） |
| `POST /api/compose` | 生成包装后的请求（不写入） |
| `POST /api/outbox` | 保存到本机待派单目录 |
| `POST /api/dispatch` | 通过蓝区客户端正式提交 |
| `GET/POST /api/notes` | 本机协同备注 |
| `POST /api/refresh` | 立即同步 |
| `GET /healthz` | 健康检查 |

服务只绑定 `127.0.0.1`，不对外网开放。
