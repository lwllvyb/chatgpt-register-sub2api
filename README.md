# chatgpt-register-sub2api

ChatGPT 账号注册、工作空间上下文刷新、Sub2API JSON 导出工具。

这是脱敏后的开源版本，只包含源码和示例配置，不包含真实邮箱、密码、refresh token、workspace ID、运行日志、状态文件或导出的账号 JSON。

## 功能

- Outlook OAuth 邮箱池：`email----password----client_id----refresh_token`
- Gmail OAuth 邮箱池：`email----client_id----client_secret----refresh_token`
- Outlook `+数字` 别名扩展，例如 `user+1@outlook.com`
- 工作空间加入/申请流程
- 账号上下文刷新和 K12/workspace 计划识别
- Sub2API 格式 JSON 导出
- 注册、加入工作空间、刷新、导出阶段支持并发
- 每次完整运行自动归档到时间戳目录

## 安装

```bash
git clone <this-repo>
cd chatgpt-register-sub2api
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

依赖很少：

- `curl-cffi`
- `pyyaml`

## 快速开始

生成本地配置：

```bash
chatgpt-register init
```

编辑生成的 `config.yaml`，填入自己的邮箱池、代理和 workspace ID，然后运行：

```bash
chatgpt-register run -c config.yaml -n 10 -t 3 --workspace-id <workspace-uuid> -v
```

默认会在 `runs/` 下创建本次运行目录：

```text
runs/
  20260706-093012_10_accounts/
    registered_accounts.json
    sub2api_bundle.json
    test_run.log
```

邮箱/别名使用状态保存在共享的 `data/outlook_token_state.json`，用于避免后续运行重复使用已经消耗过的 Outlook 邮箱或别名。

### 最简单：1 个 Outlook 邮箱注册 6 个号

只填 1 个 Outlook 邮箱，开启别名，然后跑 `-n 6`。

1. 生成配置：

```powershell
cd D:\Desktop\注册机开源
python -m chatgpt_register_sub2api.cli init
```

2. 打开 `config.yaml`，填写一个 Outlook 邮箱，并开启别名：

```yaml
mail:
  providers:
    - type: outlook_token
      enable: true
      mode: auto
      alias_enabled: true
      alias_limit_per_mailbox: 6
      mailboxes: |
        你的邮箱@outlook.com----邮箱密码----client_id----refresh_token
```

3. 填代理和 workspace ID：

```yaml
proxy:
  url: "socks5://127.0.0.1:10808"

workspace:
  enabled: true
  ids:
    - "你的workspace-id"
  route: k12_request
  re_login_enabled: false
```

4. 一次跑 6 个：

```powershell
python -m chatgpt_register_sub2api.cli run -c .\config.yaml -n 6 -t 3 --workspace-id 你的workspace-id -v
```

它会依次使用：

```text
你的邮箱@outlook.com
你的邮箱+1@outlook.com
你的邮箱+2@outlook.com
你的邮箱+3@outlook.com
你的邮箱+4@outlook.com
你的邮箱+5@outlook.com
```

结果在 `runs\时间戳_6_accounts\`，其中 `sub2api_bundle.json` 就是导出的结果。

## 配置示例

以 `config.example.yaml` 为模板。下面都是占位示例，不要把真实账号提交到仓库。

```yaml
mail:
  providers:
    - type: outlook_token
      enable: true
      label: Outlook Pool
      mode: auto
      imap_host: outlook.office365.com
      message_limit: 10
      alias_enabled: false
      alias_limit_per_mailbox: 6
      mailboxes: |
        user1@outlook.com----password1----client_id_1----refresh_token_1
        user2@hotmail.com----password2----client_id_2----refresh_token_2
    - type: gmail_oauth
      enable: false
      label: Gmail OAuth Pool
      imap_host: imap.gmail.com
      message_limit: 10
      mailboxes: |
        user1@gmail.com----google_client_id----google_client_secret----google_refresh_token

proxy:
  url: "socks5://127.0.0.1:1080"
  flaresolverr_url: ""

registration:
  threads: 3
  total: 10

parallel:
  join_threads: 3
  refresh_threads: 3
  login_threads: 1

workspace:
  enabled: true
  ids:
    - "your-workspace-uuid"
  route: k12_request
  re_login_enabled: false
  export_plan_type: k12
  max_retries: 3
  retry_backoff_ms: 5000

sub2api:
  enabled: true
  output_file: "sub2api_bundle.json"
  require_team_tokens: auto

output:
  archive_runs: true
  runs_dir: runs

logging:
  level: INFO
  file: "test_run.log"
```

## 邮箱池格式

Outlook token 池：

```text
email----password----client_id----refresh_token
```

Gmail OAuth 池：

```text
email----client_id----client_secret----refresh_token
```

## Outlook 别名

可以在 Outlook provider 或全局 mail 配置里开启别名：

```yaml
alias_enabled: true
alias_limit_per_mailbox: 6
```

当 `alias_limit_per_mailbox: 6` 时，一个主邮箱最多对应 6 个注册地址：

```text
user@outlook.com
user+1@outlook.com
user+2@outlook.com
user+3@outlook.com
user+4@outlook.com
user+5@outlook.com
```

验证码仍然从主 Outlook 邮箱读取。状态文件会按具体别名记录使用情况，所以后续运行不会重复使用同一个别名。

## 命令

| 命令 | 说明 |
| --- | --- |
| `init` | 生成 `config.yaml` |
| `register` | 只注册账号 |
| `join-workspace` | 对已有账号执行 workspace 加入/申请 |
| `refresh` | 刷新 token 并检查账号/workspace 上下文 |
| `login-team` | 实验性 team/workspace 重新登录流程 |
| `export` | 把已有账号记录导出为 Sub2API JSON |
| `run` | 完整流水线：注册 -> 加入 workspace -> refresh/check -> export |

常用示例：

```bash
chatgpt-register run -n 10 -t 5 -v
chatgpt-register refresh -i registered_accounts.json --workspace-id <workspace-uuid> -t 5 -v
chatgpt-register export -i registered_accounts.json -o sub2api_bundle.json -v
```

## 输出文件

开启 `output.archive_runs: true` 后，完整 `run` 会写入：

```text
runs/YYYYMMDD-HHMMSS_<账号数量>_accounts/
```

常见文件：

- `registered_accounts.json`：本次运行生成的账号记录
- `sub2api_bundle.json`：Sub2API 格式 bundle
- `test_run.log`：当 `logging.file` 是相对路径时，会一起放进本次运行目录

共享状态文件：

- `data/outlook_token_state.json`：邮箱和别名的 used/failed/in_use 状态

## 开源脱敏清单

公开目录建议只保留：

- `chatgpt_register_sub2api/`
- `README.md`
- `pyproject.toml`
- `.gitignore`
- `config.example.yaml`

不要提交：

- `config.yaml` 或 `config.local.yaml`
- `data/` 或 `runs/`
- `registered_accounts*.json`
- `sub2api*.json`
- `*.log` 或 `test_run*.log`
- `tests/`、`__pycache__/`、`.pytest_cache/`
- 虚拟环境、构建产物、缓存目录

发布前可以先扫一遍：

```bash
rg -n "outlook.com|refresh_token|access_token|id_token|session_token|workspace" .
```

示例配置里的占位字段可以保留；真实邮箱行、OAuth token、导出 JSON、workspace ID 不要保留。

## 注意事项

- 只在你有权限使用的邮箱和 workspace 上运行。
- `workspace.route` 支持 `accept`、`request`、`k12_request`。
- `require_team_tokens: auto` 会跟随 `workspace.re_login_enabled`。
- 如果关闭 `workspace.re_login_enabled`，导出会依赖 `/accounts/check` 刷新的账号上下文。
- 并发建议保守设置。过高并发可能触发邮箱服务或目标服务的限制。

## 致谢

核心注册流程参考了社区围绕 `chatgpt2api` 的实现，并在此基础上增加了 workspace 处理和 Sub2API 导出。
