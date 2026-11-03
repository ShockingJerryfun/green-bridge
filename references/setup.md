# 接入、认证与首次测试

本指南以绿区 Windows、GitHub HTTPS 为例，蓝区分别提供 macOS/Linux 和 Windows 命令。先搭建直接命令通道，再做一次不读取业务数据的往返。两侧需要 Git、Python 3.8+ 和访问同一个 Git 项目的网络；Windows 使用 PowerShell 5.1 或更高版本。直接命令不需要 CodeAgent。

## 1. 准备私有 Git 项目

在 GitHub 创建用于传输的私有项目，勾选初始化 README，使默认分支有首个提交。已有项目也可使用，但必须先确认派单和原始输出允许放在其中，并检查本指南的两个分支不存在；不要覆盖已有分支。项目创建入口和权限设置可参考 [GitHub 创建仓库说明](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository)。

复制项目页面“Code”里的 HTTPS 克隆链接，格式为 `https://github.com/<用户名>/<仓库名>.git`。下文的 `<Git项目链接>` 指这个地址；不要在链接中放令牌。如果请 AI 协助部署，把这个链接和两侧操作系统告诉它即可。实际项目地址、本机用户名和工作目录按组织要求处理。

任务分支的写入者能触发绿区服务账户执行命令；结果分支的读取者能看到原始输出。两侧都需要读取项目，蓝区要写任务分支，绿区要写结果分支；首次创建分支还需相应写权限。

## 2. 为两侧准备 GitHub 凭据

GitHub 网页已登录不代表本机 Git 已认证。蓝区和绿区分别在自己的设备上设置凭据；绿区必须在运行计划任务的 **Windows 登录账户**下完成。可以同一账号为两台设备分别创建令牌，或者使用分别具有项目权限的账号。

推荐为每台设备创建一枚 [GitHub 细粒度个人访问令牌](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)：

1. GitHub 头像 → **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**。
2. 选择项目所属的 **Resource owner**，在 **Repository access** 选择 **Only select repositories**，只选择刚准备的私有项目。
3. 在 **Repository permissions** 中将 **Contents** 设置为 **Read and write**，Metadata 保持 GitHub 要求的只读；设置到期时间并生成。组织项目可能需要管理员批准。
4. 令牌仅在生成时显示一次。只在对应设备的 Git 凭据提示中输入，作为密码使用；不要发到聊天、写入 URL、命令、配置文件或项目。两台设备分别保存各自令牌。

先在两侧终端检查 `git --version`、`python3 --version` 或 `python --version`，并运行 `git config --show-origin --get-all credential.helper`。macOS 可使用 `osxkeychain`，Windows 的 Git for Windows 通常包含 Git Credential Manager（GCM）；没有可用助手时先按 [GitHub 凭据缓存说明](https://docs.github.com/en/get-started/git-basics/caching-your-github-credentials-in-git) 配置。不要使用明文 `credential.helper=store`。

下面的克隆命令仅对这次操作开启 `credential.useHttpPath`，使支持路径的凭据助手区分不同项目；克隆后将相同设置写入**该 bare 仓库的本地配置**，不修改其他 Git 项目。它只影响凭据匹配，不能代替令牌自身的仓库权限限制。命令中的 `credential.gitHubAuthModes=pat` 供支持此选项的 GCM 使用；助手弹出输入窗口时，选择令牌方式并在本机输入。若使用其他助手，按其提示输入 GitHub 用户名和令牌。

## 3. 蓝区：获取源码、创建分支与配置

以下示例将 skill 放在 `$HOME/green-bridge-data/green-bridge`。请先确认这个目录和下方目标文件不存在；不要覆盖已有安装。两种获取方式选择一种：

**方式一：克隆 skill 源码。**将 `<Skill源码仓库链接>` 替换为你获取本项目的地址。这个地址与第 1 节用于传输任务的私有项目地址不同：

```bash
mkdir -p "$HOME/green-bridge-data"
git clone "<Skill源码仓库链接>" "$HOME/green-bridge-data/green-bridge"
```

**方式二：解压源码包。**将 `<压缩包路径>` 换成实际路径：

```bash
mkdir -p "$HOME/green-bridge-data"
unzip "<压缩包路径>" -d "$HOME/green-bridge-data"
```

解压后确认 `SKILL.md` 位于 `$HOME/green-bridge-data/green-bridge/`。GitHub 的 `main` 分支源码包通常解压为 `green-bridge-main`；若看到该目录，将它重命名为 `green-bridge`：

```bash
mv "$HOME/green-bridge-data/green-bridge-main" "$HOME/green-bridge-data/green-bridge"
```

将 `<Git项目链接>` 替换为第 1 节复制的 HTTPS 地址。首次克隆按本机凭据助手提示完成认证：

```bash
git -c credential.https://github.com.useHttpPath=true \
    -c credential.gitHubAuthModes=pat \
    clone --bare "<Git项目链接>" "$HOME/green-bridge-data/blue-control.git" &&
git --git-dir "$HOME/green-bridge-data/blue-control.git" \
    config --local credential.https://github.com.useHttpPath true
```

确认两个分支**都不存在**；若命令失败，先排查，不能把失败当成不存在。若已有输出，应检查分支用途，不执行下面的创建命令：

```bash
git --git-dir "$HOME/green-bridge-data/blue-control.git" ls-remote --heads origin \
  refs/heads/pingpong/requests refs/heads/pingpong/results
```

首次使用、两个分支均不存在且默认分支已有提交时，创建并推送两个分支：

```bash
git --git-dir "$HOME/green-bridge-data/blue-control.git" branch pingpong/requests HEAD &&
git --git-dir "$HOME/green-bridge-data/blue-control.git" branch pingpong/results HEAD &&
git --git-dir "$HOME/green-bridge-data/blue-control.git" push origin \
  refs/heads/pingpong/requests:refs/heads/pingpong/requests \
  refs/heads/pingpong/results:refs/heads/pingpong/results
```

创建蓝区配置；脚本使用独立的 bare clone 和状态目录，若配置已存在会报错，不会覆盖：

```bash
python3 - <<'PY'
import json
from pathlib import Path

root = Path.home() / "green-bridge-data"
skill = root / "green-bridge"
config = json.loads((skill / "assets/blue_config.example.json").read_text(encoding="utf-8"))
config["git_dir"] = str(root / "blue-control.git")
config["state_dir"] = str(root / "blue-state")
target = skill / "local/blue.json"
target.parent.mkdir(parents=True, exist_ok=True)
with target.open("x", encoding="utf-8") as output:
    json.dump(config, output, ensure_ascii=False, indent=2)
    output.write("\n")
print("蓝区配置已创建：", target)
PY
```

支持 `SKILL.md` 的 AI 工具可按自身安装方式安装目录 `green-bridge`。也可直接让工具读取该目录下的 `SKILL.md`；工具是否支持执行命令由其自身权限决定。安装到另一个目录时，确认该位置的 `local/blue.json` 指向上述 bare clone 和状态目录。蓝区配置只放本机，不包含凭据。

可选地，将 `assets/servers.example.json` 复制为同一 skill 目录下的 `local/servers.json`，填写本机首选 Python、服务器别名和使用规范；已有文件时直接编辑，不覆盖。`preferred_python` 可填包含 `path` 和 `last_verified` 的对象，`green_service` 可记录现有服务部署线索，`server_rules` 填规则列表，`servers` 可按 `alias`、`transport` 和核验时间记录绿区 SSH 别名；`historical_hosts` 只存待核验线索。历史服务器记录仅用于提示，使用时仍需核实连接和身份。该文件留在本机，打包分享时排除整个 `local/` 目录。

### 蓝区为 Windows 时

蓝区是 Windows 时，用下面的 PowerShell 命令代替本节的 macOS/Linux 命令。将两个占位符换成压缩包完整路径和 Git 项目链接；两个分支已存在时会停止。命令仅创建蓝区的独立目录和配置，不覆盖已有文件：

```powershell
$BridgeHome = Join-Path $env:USERPROFILE 'green-bridge-data'
$SkillDir = Join-Path $BridgeHome 'green-bridge'
$BlueGit = Join-Path $BridgeHome 'blue-control.git'
$BlueConfig = Join-Path $SkillDir 'local\blue.json'
if ((Test-Path -LiteralPath $SkillDir) -or (Test-Path -LiteralPath $BlueGit)) {
    throw '蓝区目录已存在，请先检查。'
}
New-Item -ItemType Directory -Force $BridgeHome -ErrorAction Stop | Out-Null
Expand-Archive -LiteralPath '<压缩包完整路径>' -DestinationPath $BridgeHome -ErrorAction Stop
$GitHubSourceDir = Join-Path $BridgeHome 'green-bridge-main'
if ((-not (Test-Path -LiteralPath $SkillDir)) -and (Test-Path -LiteralPath $GitHubSourceDir -PathType Container)) {
    Rename-Item -LiteralPath $GitHubSourceDir -NewName 'green-bridge' -ErrorAction Stop
}
if (-not (Test-Path -LiteralPath (Join-Path $SkillDir 'SKILL.md') -PathType Leaf)) {
    throw '未找到 green-bridge 目录；请检查压缩包顶层目录并更名。'
}
git -c credential.https://github.com.useHttpPath=true `
    -c credential.gitHubAuthModes=pat `
    clone --bare '<Git项目链接>' $BlueGit
if ($LASTEXITCODE -ne 0) { throw '克隆失败，请先检查网络和认证。' }
git --git-dir $BlueGit config --local credential.https://github.com.useHttpPath true
if ($LASTEXITCODE -ne 0) { throw '仓库凭据配置失败，请先检查。' }
$Existing = @(git --git-dir $BlueGit ls-remote --heads origin `
    refs/heads/pingpong/requests refs/heads/pingpong/results)
if ($LASTEXITCODE -ne 0) { throw '检查分支失败，请先排查。' }
if ($Existing.Count -gt 0) { throw '目标分支已存在，请先检查。' }
git --git-dir $BlueGit branch pingpong/requests HEAD
if ($LASTEXITCODE -ne 0) { throw '创建任务分支失败。' }
git --git-dir $BlueGit branch pingpong/results HEAD
if ($LASTEXITCODE -ne 0) { throw '创建结果分支失败。' }
git --git-dir $BlueGit push origin `
    refs/heads/pingpong/requests:refs/heads/pingpong/requests `
    refs/heads/pingpong/results:refs/heads/pingpong/results
if ($LASTEXITCODE -ne 0) { throw '推送分支失败，请先检查。' }
$config = Get-Content -Raw -Encoding UTF8 (Join-Path $SkillDir 'assets/blue_config.example.json') |
    ConvertFrom-Json
$config.git_dir = $BlueGit
$config.state_dir = Join-Path $BridgeHome 'blue-state'
New-Item -ItemType Directory -Force (Join-Path $SkillDir 'local') -ErrorAction Stop | Out-Null
if (Test-Path -LiteralPath $BlueConfig) { throw '蓝区配置已存在，请先检查。' }
[System.IO.File]::WriteAllText(
    $BlueConfig, ($config | ConvertTo-Json -Depth 10),
    [System.Text.UTF8Encoding]::new($false)
)
```

后续使用本节同样的技能安装说明；第 6 节另有 Windows 首次测试命令。

## 4. 绿区：解压、认证与配置

通过允许的方式把相同压缩包交给绿区用户。以下命令在绿区 **Windows PowerShell** 运行，将 `<压缩包完整路径>` 替换为绿区实际路径。如果目标目录已存在，停止并检查；不要覆盖现有服务。

```powershell
$BridgeHome = Join-Path $env:USERPROFILE 'green-bridge-data'
$SkillDir = Join-Path $BridgeHome 'green-bridge'
$GreenGit = Join-Path $BridgeHome 'green-control.git'
$GreenConfig = Join-Path $BridgeHome 'green.json'
if (Test-Path -LiteralPath $SkillDir) { throw '技能目录已存在，请先检查。' }
New-Item -ItemType Directory -Force $BridgeHome -ErrorAction Stop | Out-Null
Expand-Archive -LiteralPath '<压缩包完整路径>' -DestinationPath $BridgeHome -ErrorAction Stop
$GitHubSourceDir = Join-Path $BridgeHome 'green-bridge-main'
if ((-not (Test-Path -LiteralPath $SkillDir)) -and (Test-Path -LiteralPath $GitHubSourceDir -PathType Container)) {
    Rename-Item -LiteralPath $GitHubSourceDir -NewName 'green-bridge' -ErrorAction Stop
}
if (-not (Test-Path -LiteralPath (Join-Path $SkillDir 'SKILL.md') -PathType Leaf)) {
    throw '未找到 green-bridge 目录；请检查压缩包顶层目录并更名。'
}
New-Item -ItemType Directory -Force (Join-Path $BridgeHome 'work') -ErrorAction Stop | Out-Null
```

克隆时将 `<Git项目链接>` 替换为蓝区使用的**同一个** HTTPS 地址，并在绿区凭据提示中输入绿区令牌：

```powershell
git -c credential.https://github.com.useHttpPath=true `
    -c credential.gitHubAuthModes=pat `
    clone --bare '<Git项目链接>' $GreenGit
if ($LASTEXITCODE -ne 0) { throw '克隆失败，请先检查网络和认证。' }
git --git-dir $GreenGit config --local credential.https://github.com.useHttpPath true
if ($LASTEXITCODE -ne 0) { throw '仓库凭据配置失败，请先检查。' }
git --git-dir $GreenGit ls-remote --heads origin `
    refs/heads/pingpong/requests refs/heads/pingpong/results
if ($LASTEXITCODE -ne 0) { throw '读取分支失败，请先检查网络和认证。' }
```

成功时应看到两个分支。接着创建绿区配置，自动记录当前 Git 和 Python 的绝对路径，并以无 BOM 的 UTF-8 写入 JSON；只使用直接命令时 `adapter_argv` 保持空数组。配置文件已存在时命令会停止：

```powershell
$GitExe = (Get-Command git -CommandType Application | Select-Object -First 1).Source
$PythonExe = & python -c 'import sys; print(sys.executable)'
if ($LASTEXITCODE -ne 0 -or -not $PythonExe) {
    throw '未找到可运行的 Python，请先安装并检查 PATH。'
}
$PythonExe = "$PythonExe".Trim()
if (-not (Test-Path -LiteralPath $PythonExe)) { throw 'Python 路径无效，请先检查。' }
if (Test-Path -LiteralPath $GreenConfig) { throw '绿区配置已存在，请先检查。' }
$config = Get-Content -Raw -Encoding UTF8 (Join-Path $SkillDir 'assets/green_config.example.json') |
    ConvertFrom-Json
$config.git_dir = $GreenGit
$config.state_dir = Join-Path $BridgeHome 'green-state'
$config.git_executable = $GitExe
[System.IO.File]::WriteAllText(
    $GreenConfig, ($config | ConvertTo-Json -Depth 10),
    [System.Text.UTF8Encoding]::new($false)
)
```

在同一用户下打开新的 PowerShell 窗口，先临时关闭交互提示，再检查认证能否供后台使用。下面只检查读取；实际写权限由后面的首次往返确认：

```powershell
$GreenGit = Join-Path $env:USERPROFILE 'green-bridge-data\green-control.git'
$env:GIT_TERMINAL_PROMPT = '0'
$env:GCM_INTERACTIVE = 'Never'
git --git-dir $GreenGit ls-remote --heads origin `
    refs/heads/pingpong/requests refs/heads/pingpong/results
```

完成后关闭这个检查窗口。若网络或认证失败，先在绿区本机修复；不要把令牌、代理密码或完整 Git 配置发到聊天。最初部署曾遇到 Git 配置错误和 GitHub 网络超时：应区分配置解析、DNS/代理、连接、认证和仓库权限，不关闭 TLS 证书校验。计划任务运行环境可能与交互终端不同，最终仍要以真实回传验证。

## 5. 绿区：建立基线并启动常驻服务

回到第 4 节的原 PowerShell 窗口。若该电脑已有 Green Bridge 服务，先核对已有计划任务及状态目录，避免重复注册服务。必须先建立基线、启动服务，**之后**再从蓝区提交首次测试。默认 `start_after: "HEAD"` 只接收建立基线之后的新任务；如果先提交任务，再用 HEAD 初始化，该任务会被跳过。

```powershell
& $PythonExe (Join-Path $SkillDir 'scripts\green_bridge.py') `
    --config $GreenConfig serve --once
if ($LASTEXITCODE -ne 0) { throw '基线初始化失败，请先查看错误。' }
& (Join-Path $SkillDir 'scripts\install_green_task.ps1') `
    -PythonExe $PythonExe -ConfigPath $GreenConfig
Get-ScheduledTask -TaskName GreenBridge | Select-Object TaskName, State
Get-Content -Tail 20 (Join-Path $BridgeHome 'green-state\service.log')
```

安装脚本为当前用户注册 `GreenBridge` 登录任务并请求启动；同名任务存在时会停止，不会覆盖。注册成功不等于服务实际运行或结果可写。脚本和 Windows 计划任务需要在目标机器验证；如执行策略阻止脚本，按所在组织的方式处理。登录触发只在用户登录后运行，注销或关机期间不保证运行。

示例配置使用普通 Git 轮询，`poll_seconds` 为 60 秒，是每轮处理结束后的等待时间；可在符合平台和组织要求的前提下，根据实测调整。GitHub HTTPS 可在功能验证后按 [服务说明](green_service.md#github-快速检测)评估可选的 API 快速检测。

## 6. 蓝区：发送并读取一次只读测试

在绿区 PowerShell 运行 `Join-Path $BridgeHome 'work'` 查看工作目录。按允许的方式将该路径告诉蓝区；不要发送凭据。蓝区运行以下命令，输入这个绿区**绝对路径**，生成不读取业务数据的测试请求；JSON 路径由程序转义。若测试请求已存在则停止，不覆盖：

```bash
printf '绿区工作目录绝对路径：'
IFS= read -r GREEN_BRIDGE_WORKDIR
python3 - "$GREEN_BRIDGE_WORKDIR" <<'PY'
import json
import sys
from pathlib import Path

root = Path.home() / "green-bridge-data"
skill = root / "green-bridge"
request = json.loads((skill / "assets/command.example.json").read_text(encoding="utf-8"))
request["command"]["cwd"] = sys.argv[1].strip()
if not request["command"]["cwd"]:
    raise SystemExit("绿区工作目录不能为空。")
target = root / "hello.json"
with target.open("x", encoding="utf-8") as output:
    json.dump(request, output, ensure_ascii=False, indent=2)
    output.write("\n")
print("测试请求已创建：", target)
PY
```

提交后查询同一个指令，直到出现最终状态；`not_found` 只表示结果尚未可见，不代表没有执行，**不要换 ID 重发**：

```bash
python3 "$HOME/green-bridge-data/green-bridge/scripts/green_bridge.py" \
  --config "$HOME/green-bridge-data/green-bridge/local/blue.json" \
  submit "$HOME/green-bridge-data/hello.json" --task first-check --fast

python3 "$HOME/green-bridge-data/green-bridge/scripts/green_bridge.py" \
  --config "$HOME/green-bridge-data/green-bridge/local/blue.json" \
  status hello-01 --task first-check
```

确认状态为 `completed`、退出码为 0 后，再下载并校验原始输出。输出目标须事先不存在；打开文件应看到 `PINGPONG_OK`：

```bash
python3 "$HOME/green-bridge-data/green-bridge/scripts/green_bridge.py" \
  --config "$HOME/green-bridge-data/green-bridge/local/blue.json" \
  output hello-01 --task first-check --stream stdout \
  --output "$HOME/green-bridge-data/hello-output.txt"
cat "$HOME/green-bridge-data/hello-output.txt"
```

### 蓝区为 Windows 时

蓝区 Windows 在 PowerShell 中运行下列命令，输入绿区工作目录的绝对路径。等待状态为 `completed`、退出码为 0 后，才运行最后一段下载输出的命令；如果配置或测试文件已存在，先检查，不覆盖。

```powershell
$BridgeHome = Join-Path $env:USERPROFILE 'green-bridge-data'
$SkillDir = Join-Path $BridgeHome 'green-bridge'
$BlueConfig = Join-Path $SkillDir 'local\blue.json'
$Request = Join-Path $BridgeHome 'hello.json'
if (Test-Path -LiteralPath $Request) { throw '测试请求已存在，请先检查。' }
$requestData = Get-Content -Raw -Encoding UTF8 (Join-Path $SkillDir 'assets/command.example.json') |
    ConvertFrom-Json
$requestData.command.cwd = Read-Host '绿区工作目录绝对路径'
if (-not $requestData.command.cwd) { throw '绿区工作目录不能为空。' }
[System.IO.File]::WriteAllText(
    $Request, ($requestData | ConvertTo-Json -Depth 10),
    [System.Text.UTF8Encoding]::new($false)
)
& python (Join-Path $SkillDir 'scripts\green_bridge.py') --config $BlueConfig `
    submit $Request --task first-check --fast
if ($LASTEXITCODE -ne 0) { throw '提交结果不明或失败，请先检查原任务 ID。' }
& python (Join-Path $SkillDir 'scripts\green_bridge.py') --config $BlueConfig `
    status hello-01 --task first-check
```

```powershell
& python (Join-Path $SkillDir 'scripts\green_bridge.py') --config $BlueConfig `
    output hello-01 --task first-check --stream stdout `
    --output (Join-Path $BridgeHome 'hello-output.txt')
if ($LASTEXITCODE -ne 0) { throw '读取输出失败，请先检查任务状态。' }
Get-Content (Join-Path $BridgeHome 'hello-output.txt')
```

这一步同时验证了蓝区派单、绿区执行和结果回传。若状态为 `unknown`、超时或长时间没有结果，先检查计划任务、日志和绿区实际进程；不得自动重跑已启动任务或换 ID 避免去重。相同任务 ID 和指令 ID 只能用于相同内容重试。

## 7. 后续使用

- 同一个工作目标使用稳定的任务 ID，每条指令使用唯一的指令 ID。独立命令可并发；有依赖的步骤按顺序执行。
- 原始 stdout、stderr 和退出码保存在传输项目中，不自动脱敏；派单前确认允许回传的内容。
- CodeAgent 适配器是可选功能，依赖具体 CLI、工作目录和服务账户；按 [服务说明](green_service.md)单独验证。
- 首次往返成功后，GitHub HTTPS 仓库可按 [服务说明](green_service.md#github-快速检测)选择快速检查、后台同步和 API 提交；示例配置中的 API 和后台选项默认关闭。其他 Git 服务器先使用普通 Git 同步，并按实际环境验证。
- 首次测试完成后，可把自己的 Git 项目链接和蓝区配置位置提供给 AI 工具，并要求其使用 `green-bridge`；不要提供令牌或私钥。
