# 绿区自动交接

这是在原有 `pingpong.md` / `history.md` 人工交接之外的可选协议。适用单个绿区接收服务、同一个共享 Git 仓库、分开的任务和结果分支；已有手工流程保持可用。自动模式使用 `pingpong/requests/<id>.json` 和 `pingpong/results/<id>.json`，不自动修改旧交接文档。

任务聚合模式使用任务分支的 `pingpong/task-requests/<任务ID>.json` 和结果分支的 `pingpong/task-results/<任务ID>.json`。一项工作沿用同一个任务 ID，每条指令在该任务中拥有自己的指令 ID、请求内容和结果。两个协议并存；尚未升级的绿区服务不会执行聚合任务，因此必须先确认升级完成，再发送新版任务。

## 准备与边界

- 两区都能访问约定的 Git 远端；蓝区可写任务分支，绿区可写结果分支。远端应限制分支写入权限，不能仅凭 Git 作者名或内容哈希认定派单者身份。
- 服务运行时 Git 使用非交互认证；应事先在对应 Windows 用户账户下配置可用凭据。服务不会代替用户完成登录，也不会弹窗等待输入。
- 如需隔离当前用户的 Git 全局配置，可在 `git_config_global` 指定一个仅供本服务使用的本地配置文件。服务、适配器及其子进程会使用该文件，服务外的其他进程不受影响；隔离后仍需验证证书、代理和凭据助手是否按预期生效。
- 两个分支需预先存在。服务使用自己的 **bare clone** 和自己的本地状态目录，蓝区客户端也使用单独的 bare clone；不要指向用户正在工作的 checkout 或共享同一份 bare clone。
- 绿区需 Python 3.8+ 和 Git；直接命令不需要安装 AI 代理。使用代理动作时还需兼容的本地程序与适配器。示例配置在 `assets/green_config.example.json`；其中路径全是需要替换的示例，配置和凭据不提交进仓库。两侧认证与首次部署参见 [接入与认证](setup.md)。
- CodeAgent 帮助列出 `--print`、`--input-format text`、`--output-format json`、`--session-id` 和 `--resume`。`scripts/codeagent_adapter.py` 按这些入口适配，但它对返回 JSON 字段的识别、Windows `.bat` 的子进程调用、服务账户的登录与权限尚须在目标机上实测。遇到不认识的结果会标记 `unknown`，不会自动重跑。
- 自动接收等同于允许通过任务分支触发绿区代理动作。配置最小权限的运行账户、可信工作目录和仓库访问权限；按实际跨区规则确定可回传内容。结果由代理报告，不把文本本身当成已独立核验的命令执行证据。
- `command.execute` 会以服务账户权限直接执行任务分支中的命令，并原样上传输出；拥有该分支写权限的人因而能触发本机命令。仓库和分支权限应与这一能力匹配。

## 一次性部署准备

1. 在约定的共享仓库创建任务分支 `pingpong/requests` 和结果分支 `pingpong/results`，并确认写权限和基线。不要覆盖已有分支或使用强推。
2. 蓝、绿两侧分别创建专用 bare clone：

   ```powershell
   git clone --bare <共享仓库地址> C:\Pingpong\green-control.git
   ```

   蓝区使用另一条独立路径。此命令只在实际地址和路径明确、且已授权接入时运行。
3. 将本项目所需脚本复制到绿区稳定路径。复制 `assets/green_config.example.json` 到绿区本地并按实际环境填写 `git_dir`、`state_dir` 和分支。只执行直接命令时 `adapter_argv` 留空；需要代理时再填写适配器、代理程序及可信工作目录。`adapter_argv` 是参数数组，消息经标准输入传入，不拼成 shell 命令。
4. `start_after: "HEAD"` 默认把首次启动看到的任务分支头作为基线，**不执行基线之前的历史任务**。确需处理历史任务时，在启动前显式填写可信基线提交；服务只处理其之后的新增派单。初始化后再由蓝区提交任务。
5. 先在交互式 PowerShell 以前台单次模式检查基线和配置：

   ```powershell
   & <Python绝对路径> <skill目录>\scripts\green_bridge.py --config <绿区配置.json> serve --once
   ```

   两侧用测试仓库和只读任务做完整往返。需要代理时另行确认其新建/恢复、JSON 和工具权限。`serve --once` 的成功只说明本轮检查成功，不代表执行过任务。
6. Windows 11 / PowerShell 5.1 可在用户明确需要并确认路径后注册任务：

   ```powershell
   & <skill目录>\scripts\install_green_task.ps1 -PythonExe <Python绝对路径> -ConfigPath <绿区配置.json>
   ```

   脚本以**当前登录用户、普通权限、登录触发**注册并请求启动；已有同名任务时停止，不覆盖。用户注销或机器关机时不能保证服务运行；重启后须再次登录。检查任务计划程序和 `state_dir\service.log`，注册/启动请求不等于服务运行正常。Python / 配置或任务计划程序的失败应在绿区排查；该脚本和 Windows 环境尚未实测。

`scripts/upgrade_green.py` 仅适用于 Windows 下特定的现有布局：`adapter_argv[1]` 与服务脚本同目录，传输仓库的 Git 远端 `main` 中也包含指定版本的服务源码，计划任务名为 `BlueGreenPingpong`。它核对旧版、等待派单发布及空闲后重启，并在部分失败场景尝试恢复。锁和自动回滚须在目标机器验证。

本文推荐的**无代理配置**及**代码仓、传输仓分离**布局不满足这些前提，不能直接使用该自动升级脚本。升级时先停止派单、等待活动任务和结果发布完成，保留状态目录，按本机运维流程备份并替换经核验的服务文件，再启动和验证；不要清空状态或自动重发已启动的任务。

## GitHub 快速检测

`watch_mode` 默认是 `git`，每轮同步和处理结束后再等 `poll_seconds`。对于 HTTPS 的 github.com 远端，可选 `watch_mode: "github_api"`：绿区从 Git 凭据助手读取令牌，使用 Git 的 HTTP 代理和经过证书验证的持久 HTTPS 连接，带 ETag 条件请求检查任务分支。`poll_seconds` 是目标检查周期；服务忙碌、网络延迟或 GitHub 要求更长周期时可能超过该值。这是快速轮询，不是仓库推送事件。

其他 Git 服务器使用 `watch_mode: "git"`。同一轮 Git 同步和处理用时会加到 `poll_seconds` 之前，需按两侧网络实测选择周期。

快速检查只决定何时执行原有 Git 同步，任务去重与原始输出协议不变。在 GitHub 快速检测模式下，并发服务按 `reconcile_seconds` 周期尝试 Git 同步请求和结果分支，作为检查遗漏变化及分支历史的兜底，也在检测到任务分支变化时同步；待处理任务和结果在独立执行、发布通道中推进。这个周期与正常任务的 `poll_seconds` 检查周期不同；同步和网络耗时会使实际间隔变长。API 失败时立即退回 Git 检查，并延后重试；遇到 GitHub 限流时遵守返回的等待时间。Git 凭据、代理和认证头只在本机使用，不写入任务或结果。该模式需要可用的 GitHub 仓库读取权限及其 API 的 HTTPS 访问；不支持的代理或其他 Git 服务器自动回退到 Git。

直接命令的结果默认在 Git push 后再次拉取核对，可选 `result_confirmation: "push_ack"` 以成功的 push 应答确认提交，避免第二次网络往返阻塞后续任务。上传前仍校验每段输出；蓝区取回时继续按 SHA-256 校验。若推送结果不确定，保留本地结果并在下一轮同步后重试回传，不重新执行命令；下一轮 Git 同步继续检查结果分支历史。

## 直接命令并发

已有配置未设置 `concurrent_tasks` 时保持原有串行服务；示例配置启用该选项。设置为 `true` 后，常驻服务持续检测任务，最多同时执行 `command_workers` 个直接命令（默认 4），并以最多 `publish_workers` 个独立任务并发上传结果（默认 4）；可选的 CodeAgent 调用保留一条顺序执行通道。任务、输出和状态仍按 ID 独立保存。`serve --once` 保持串行单轮行为。

多个结果可以独立完成和上传。同一结果分支同时推进时，失败的上传会核对最新分支，并在历史仍连续时基于新分支头重建提交、重试回传，不重新执行命令或强推。同步时发现外部改写分支历史会暂停新任务并要求检查。Git 检查与普通推送并非原子操作：若其间结果分支被强制回退，非强制推送仍可能重新推进被回退的历史，因此所有写入者都不得强制改写结果分支。一个任务完成并回传无需等待其他长任务；并发配额用满时新任务仍需等待。独立命令可并行；有先后依赖的步骤须写入同一个脚本，或在前一步完成后再派发下一步。

并发服务先在本地记录命令已启动，再异步上传可选的运行状态；状态上传不阻塞命令启动。短任务可能在运行状态发布前已结束，此时只看到最终结果。命令完成后的结果发布单独调度，延迟或失败只影响交付，不重新执行命令。

同一任务中同时完成的少量小输出可合并到一次结果提交；每条输出仍与本地快照逐字节核对。为聚合同一任务的并发结果，服务在有其他命令仍在运行时最多短暂等待 0.4 秒；结果发布失败后会逐条重试，大输出仍走独立发布与必要的分段恢复。

### 可选的后台同步和 API 回传

`background_sync` 和 `api_results` 默认均为 `false`。在目标环境验证后，并发服务可设置 `background_sync: true`，使 Git 分支同步在后台进行，已核验任务的执行可以继续。常规 Git 结果发布会等待同步核验，避免使用过期的结果分支头。`api_results: true` 还要求 `concurrent_tasks: true`、`background_sync: true` 和 GitHub 结果分支写权限。

API 回传仅用于聚合任务中标准输出和标准错误各不超过 4 KiB 的终态直接命令；服务先核验本地输出快照，再按预期分支头原子提交。成功后蓝区可能立即看到结果，服务仍通过 Git 复核才在本地标记已交付。提交失败、冲突或回执不明时先同步核验，再回退 Git 重试交付，不重新执行命令。收到明确限流信号后，等待期间的新结果走 Git。API 请求整体限制 96,000 字节，长期积累的任务结果文件超限时也会改走 Git。

后台同步不会消除所有等待，例如 GitHub 分支检查仍可能因网络阻塞。API 与 Git 推送都需要现有的分支权限和证书校验。

如果服务仓库使用 Git Credential Manager 中已保存的静态令牌，绿区可选择 `gcm_skip_store: true`，使服务自己的 Git 操作在认证成功后不重复保存凭据；读取凭据和认证失败时的失效处理仍由 Git Credential Manager 完成。默认值为 `false`；使用自动刷新令牌的认证方式、其他凭据助手或不确定认证类型时保持默认值。该选项不修改全局 Git 配置，也不注入到派发的命令中。需重新登录或更换令牌时，先使用正常 Git 凭据流程完成认证。

新任务结果中的 `timing` 记录绿区接收、接受、运行和完成时间，直接命令的 `command_result` 还记录进程开始、结束及本地耗时；这些时间不包含蓝区提交前准备或蓝区读到结果的等待。Git 推送耗时和重试类别在绿区服务日志／本地状态中，完整端到端时延须由蓝区单独计时。

## 蓝区派单与取件

蓝区配置也提供 `git_dir`、`state_dir`、`remote` 和两个分支；提交/查询不使用 `adapter_argv`。例如，保存任务 JSON 后：

使用 Git 提交时，蓝区会在本地保留已确认的任务分支提交；GitHub API 核对远端分支后，如果所需提交已在本地，则复用本地对象以减少一次 Git 拉取。API 不可用或本地缺少提交时退回 Git 拉取；遇到已确认分支回退会停止提交，其他写入者推进分支时仍按 Git 非强制推送及冲突重试处理。

蓝区可选择 `api_submit: true`，使 GitHub 仓库中使用 `--fast` 的聚合任务优先通过原子 API 追加请求。客户端核对已观察的分支历史和任务文件字节，再以预期分支头提交；并发更新、回执不明、API 不可用或文件过大时经 Git 核验后重试，不改变指令 ID。API 限流期间遵守等待时间并使用 Git。该选项默认关闭，其他 Git 仓库继续使用 Git。API 返回的提交可能尚未拉到本地 Git 仓库；需要在本地查看时先同步。

蓝区的 `state_dir/client_api.sqlite3` 保存已观察的请求分支头和 API 回退状态，不保存凭据或任务内容。保留该数据库以延续历史回退检查；多个本地会话应使用同一个蓝区配置和状态目录。

GitHub 连接器可用来读取结果 JSON 和状态。取回原始命令输出时应通过 `output` 从 Git 对象取字节并校验 SHA-256；连接器对非 UTF-8 内容可能作文本转换，不能只按其显示内容推断字节完整性。

```text
python green_bridge.py --config <蓝区配置.json> submit <任务.json>
python green_bridge.py --config <蓝区配置.json> status <任务ID>
```

`submit` 将任务规范化后以新文件提交并推送，只接受新的任务 ID；同 ID、相同内容可安全重试，不同内容会报错。`status` fetch 结果分支并读取该 ID 的当前结果，不改工作树。不要把任务编号、提交成功或代理完成等同于命令执行成功。

聚合模式保留每条指令原有 JSON 格式，在 CLI 增加稳定任务 ID：

```text
python green_bridge.py --config <蓝区配置.json> submit <指令.json> --task <任务ID> --fast
python green_bridge.py --config <蓝区配置.json> submit-batch <批量.json> --task <任务ID> --fast
python green_bridge.py --config <蓝区配置.json> status <指令ID> --task <任务ID>
python green_bridge.py --config <蓝区配置.json> output <指令ID> --task <任务ID> --stream stdout --output <新文件路径>
```

`--fast` 仍仅用于 `report_running: false` 的直接命令。任务文件只允许追加，既有指令不能修改、删除或调序；同任务同指令 ID 和相同内容可安全重试，内容不同会拒绝。同名指令可以属于不同任务。蓝区或绿区并发更新同一任务时，冲突方读取最新分支并重新合并自己的指令或结果，不强推、不覆盖其他记录。历史任务继续按旧接口读取，不会自动迁移或删除。

批量文件格式为 `{"commands":[<指令对象1>,<指令对象2>,...]}`。`submit-batch` 将同一任务的一组指令放在一次 Git 提交中；服务仍按指令 ID 分别去重、执行和回传。仅把可并行的独立命令放进同一批次；有依赖的步骤先等待前序结果，或合并成一个脚本。

任务文件上限为 16 MiB；单条指令仍受原有请求上限约束。长期任务接近上限时应先确认范围和归档策略，服务不会擅自删除历史或把它当成新任务。聚合主要减少 JSON 文件数量，并不保证单次通信更快。

GitHub 连接器可用时，蓝区也可先按相同协议校验任务，再用连接器的 `create_file` 向 `pingpong/requests` 分支创建 `pingpong/requests/<任务ID>.json`，并通过 `fetch_file` 从结果分支读取结果。内容使用 UTF-8 JSON、键排序、两空格缩进和末尾换行，与 `submit` 的规范化字节一致。不得更新或删除已有任务文件。创建结果不明时先读取同一路径并逐字节比较；同内容继续查询原任务，不同内容停止。读取二进制输出时使用 Base64，解码后按结果清单逐段和整体校验 SHA-256；若连接器无法返回大文件内容，使用下面的 Git 下载方式。连接器只改变蓝区访问仓库的方式，不改变绿区处理和去重机制。

示例：

```json
{"version":1,"id":"r001-new","action":"session.new","session":"project-1"}
```

默认由蓝区组织命令或脚本，不经 CodeAgent 直接执行；只有用户明确要求时才使用代理。例如：

```json
{
  "version": 1,
  "id": "r003-command",
  "action": "command.execute",
  "session": "project-1",
  "command": {
    "mode": "powershell",
    "cwd": "C:\\Pingpong\\work",
    "script": "Write-Output 'PINGPONG_DIRECT'; [Console]::Error.WriteLine('DIRECT_STDERR'); exit 0"
  },
  "timeout_seconds": 120
}
```

`cwd` 是绿区存在的绝对工作目录。PowerShell 模式运行非交互式进程，短脚本通过 UTF-16LE 编码参数传入；超过命令行限制时，在服务的专用任务目录创建带 UTF-8 BOM 的 `.ps1` 文件并通过 `-File` 执行，遵守现有执行策略。可在本机配置 `powershell_executable`，Windows 默认是 `powershell.exe`。也可用 `{"mode":"argv","cwd":"<绝对目录>","argv":["<程序>","<参数>"]}` 直接启动程序，参数不会由服务拼成 shell 字符串。标准输入关闭，不适合需要交互输入的命令。PowerShell 脚本要显式传播外部程序的退出码，例如适当地使用 `exit $LASTEXITCODE`。

简单远端命令可以直接用 `argv` 启动绿区的 `ssh.exe`，由绿区已有配置解析服务器别名和认证。只在用户授权的范围内连接；非交互式只读检查可使用 `-T`、`-n`、`BatchMode=yes`、`StrictHostKeyChecking=yes`、`UpdateHostKeys=no` 和有限的 `ConnectTimeout`。SSH 的远端命令仍由远端 shell 解析，应明确参数及转义。不要输出或传输密钥、密码、配置文件内容或不需要的内网地址。

短而明确的命令可在 `command` 中设 `"report_running": false`，省去中间运行状态的远端推送；结果到达前 Git 查询可能显示 `not_found`。服务仍会在本地持久记录启动状态，中断后标记 `unknown`，不会自动重跑。默认值是 `true`，适合希望在 Git 中观察运行状态的任务。

对这种短命令，蓝区可用 `submit <任务.json> --fast`：默认以 Git push 的远端成功应答确认派单，不额外拉取分支复查；启用 `api_submit` 时，符合条件的聚合任务优先使用上述 API 提交。派单不确定时只能核对或重试同 ID、同内容，不得换 ID 重发；普通提交仍会在推送后拉取核对。

标准输出、标准错误按进程产生的原始字节分别存放在结果分支 `pingpong/outputs/<任务ID>/` 下；每段最多 4 MiB，结果 JSON 按顺序记录路径、字节数和 SHA-256。不会自动脱敏或转码，因此应确认允许将命令输出上传到该仓库。两条输出流分别保留，不记录它们之间的原始交错顺序。下载并校验原始字节：

聚合任务的输出路径使用内部稳定标识，不能从指令 ID 直接猜测；使用 `output --task` 按结果清单下载。聚合任务中的空输出使用空分段列表和空字节哈希；不超过 4 KiB 的非空输出以 Base64 保存在结果清单中。两者都不创建额外输出文件，下载时仍按原始字节和 SHA-256 校验；较大输出仍使用独立文件。

```text
python green_bridge.py --config <蓝区配置.json> output <任务ID> --stream stdout --output <新文件路径>
python green_bridge.py --config <蓝区配置.json> output <任务ID> --stream stderr --output <新文件路径>
```

输出目标必须不存在。大输出会拆成多个 Git 文件，但下载命令会按顺序无损重组。直接命令的 `session` 是可选的 Git 记录标签，不会向对应 CodeAgent 会话注入消息；如后续代理需要结果，应在新的代理任务中明确提供相关证据。

如果刚用 `status` 查过结果，可在两条 `output` 命令末尾加入 `--results-commit <status返回的results_branch_commit>`，直接读取本地已获取的对象，避免为每条输出再次访问远端。

```json
{
  "version":1,
  "id":"r002-run",
  "action":"task.execute",
  "session":"project-1",
  "context":{"code_commit":"<已推送并可获取的代码提交>","prior_evidence":["<上一轮结果索引>"]},
  "message":"在约定机器和目录执行已写明的命令；列明前置条件、参数、停止条件、退出码和允许回传的证据。",
  "timeout_seconds":1800
}
```

动作标识：

- `session.new`：新建别名对应的原生会话，别名必须是新的。
- `session.attach`：把已有原生会话 ID 与新别名关联；需提供 `native_session_id`，并由适配器验证恢复。
- `session.switch`：检查并选择已登记的别名。后续每次调用仍以原生 ID 显式恢复。
- `task.execute`：执行当前任务；可指定 `session`，省略时使用已选择会话。
- `command.execute`：不启动 CodeAgent，执行明确指定的 PowerShell 脚本或程序参数，回传原始 stdout、stderr 和退出码。
- `session.checkpoint`：请求短摘要；可指定 `session`。摘要是代理报告，不能代替原始证据。

ID 和会话别名用小写字母、数字、点、下划线和连字符。请求文件一经发布不可修改或删除；修订或重跑必须使用新 ID。任务的代码基线、目标环境、工作目录、完整命令、前置条件和回传范围应写清；服务不会从代码提交中自动推导或切换目标 checkout。

## 状态、恢复与上下文

- `accepted` 表示已接收，`running` 表示即将启动或正在执行，`completed` 表示代理调用结束；`failed`、`needs_input` 和 `unknown` 均需按结果处理。`completed` 不证明命令成功；核对 `agent_report.command_evidence` 与原始日志。
- 对直接命令，`completed` 表示主进程退出码为 0，非零为 `failed`；超时为 `unknown`，回传的是标记为不完整的输出快照。超时可能留下子进程，不自动重复执行。异步子进程在主进程退出后继续产生的输出不属于完成时的快照。
- 执行前记录状态，超时或服务在执行中退出后标为 `unknown`，**不自动重跑**。超时可能只终止适配器而留下子进程，须先核实绿区进程和副作用，再决定是否发新任务。
- 推送结果失败时，本地数据库保留结果并重试回传，不再次调用代理。服务发现任务/结果分支历史被重写、请求文件被修改或删除时停止并报告；结果分支检查与推送之间仍有上述竞争窗口。服务单实例锁和任务去重只适用于同一状态目录；多机协调未实现。
- `delivery_pending` 表示执行已结束但原始输出仍在回传，`execution_state` 保留执行结果；不是命令未执行。对于一次性推送失败且总输出为 64 KiB 至 8 MiB 的直接命令，服务核对已有快照后尝试以不超过 64 KiB 的输出分段分次提交，全部分段就位后才发布最终结果；认证或限流失败不采用此恢复路径。其他失败保留本地快照并退避重试。`delivery_attempts` 记录去敏的失败类别、次数及下次重试时间，不记录 Git 原始错误或凭据。
- `state_dir/state.sqlite3` 保存任务、水位、会话 ID 与最近 checkpoint，`state_dir/logs` 保存适配器标准输出/错误。Git 的请求和结果是跨区共享记录；绿区日志路径不表示蓝区可以访问。服务日志会轮转。
- 每轮把必要事实、基线、验收条件及前序结果索引放在请求的 `context`；不要依赖模型会话记忆。每次完成后如代理返回 `checkpoint`，服务保存并传给下一轮；摘要是代理生成，蓝区仍需对照 Git 结果和原始证据。
- 适配器保留 CLI 给出的单次调用 `usage` 和代理自报的上下文情况。**单次 token 用量不是上下文剩余额度**；未知即未知，不推断压缩阈值。若观察到真实压缩事件或代理反馈上下文丢失，蓝区应先让其输出可核对的阶段结果，再用新别名创建新会话，并将必要事实和证据索引放进新任务。进行中的调用无法由该版本可靠感知或提前阻止自动压缩。
- `--print` 会跳过 CodeAgent 的工作区信任对话；仅在已确认可信的工作目录运行。不要默认跳过权限审批；若工具需要交互批准，服务应返回阻塞或超时状态，而不是擅自扩大权限。

## 适配器契约

服务用 `adapter_argv` 启动本地适配器，将 JSON 写入其 stdin；适配器只能在 stdout 输出一个 JSON 对象。输入含 `version`、`id`、`request_commit`、`request_sha256`、`action`、`session`、`native_session_id`、`message`、`context` 和 `previous_checkpoint`。

响应至少包含 `version: 1`、`status`（`completed` / `failed` / `needs_input`）和 `native_session_id`。可返回 `summary`、`command_evidence`、`artifacts`、`context` 和 `checkpoint`。非零退出、非 JSON、缺字段、错误会话 ID 或超时均标为 `unknown`；不因为无法识别而重试执行。原始 stdout/stderr 仅留在绿区本地。
