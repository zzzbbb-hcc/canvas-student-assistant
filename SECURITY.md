# Security Policy

## 凭证边界

真实 Canvas PAT 和可选 AI API Key 只能由用户在运行中的 App 输入，并通过 Python `keyring` 保存到 Windows Credential Manager。它们不得进入源码、Git、SQLite、缓存、JSON、文本文件、`.env`、命令行、URL、日志、异常信息、通知内容或 AI payload。

ChatGPT OAuth Access/Refresh/ID Token 同样只进入 Windows Credential Manager 的受保护记录。记录使用限长 ASCII 分块和最后提交的清单，避免超出 Windows 单条凭证容量或发布半份记录；没有文件、SQLite 或日志回退。公开安装标识、模型选择和资料发送偏好可存入 SQLite，但不是凭证。

当前版本不实现 Ed，因此不会收集、保存或请求 Ed Token、学校密码、SSO 密码、Cookie、Session 或 MFA 信息。

## 网络边界

- Canvas 请求仅使用 HTTPS，并验证 TLS；代码中禁止 `verify=False`。
- Authorization 只作为请求 Header 使用，不进入 URL。
- Canvas API 为只读；App 不提交、编辑或删除课程内容与作业。
- 外部课件重定向必须是公开 HTTPS 地址，并拒绝 localhost、`.local` 和非公网 IP。
- 当前 AI 本地模式不发起 AI 网络请求。
- 用户主动点击 ChatGPT 连接时，系统浏览器发起官方 OAuth；只有完成授权且 `chatgpt.allow_study_data` 为 `true` 才允许向在线模型发送当前问题、近期对话及所选课程证据。该偏好默认关闭，可随时取消；明确发起的登录和模型目录查询不属于课程内容推理请求。
- ChatGPT 请求只发送到固定的 OpenAI HTTPS 端点，不自动改用 Settings 中的可选 AI API Key；套餐资格和额度以实际授权及成功请求为准。
- 作业介绍中英对照及公告标题、重点摘录和正文中文翻译使用本机 CPU 和公开 OPUS-MT 模型；翻译服务不持有凭证、不发起 HTTP、不读取附件、不写入原文或译文。仅独立模型安装脚本从固定 HTTPS 地址下载公开模型并验证固定 SHA-256。发布脚本只复制六个明确列出的公开模型文件，不收集任何用户数据目录。

## 本地存储

SQLite 保存课程/截止缓存、公告标记、用户偏好、同步元数据、提醒去重，以及用户要求保留的问答和分组。它是本机普通 SQLite，不是加密数据库；具有当前用户文件权限的程序或备份可能读取聊天内容。

聊天消息为追加式保存：用户问题先落库，再请求回答，随后保存助手回复、来源或不含诊断细节的失败提示。重命名、移动、重启、退出账户及清理内容缓存均不删除问答。此前旧版本关闭后已丢失、从未持久化的临时问答不能恢复。

聊天文本在绑定 SQL 参数之前脱敏常见明确 key/value、Authorization/Bearer、Cookie、URL Token 参数、URL 用户信息、常见 API Key 和安全测试 Secret；来源、标题与分组名使用同一边界。数据库既有非聊天写入接口仍拒绝敏感字段和授权 Header。普通问答不作静默截断，但超出支持长度会拒绝；所有 SQL 使用参数化、事务和外键。此防护不能识别任意无标识秘密，因此不得把聊天记录当作凭证存储。

为减少用户特定 URL 泄漏风险，Assignment 持久缓存不保存 description 或 html_url。完整内容只在当前进程内显示，或按需重新从 Canvas 获取。

Canvas 资料检索的原始下载文档和独立检索数据只在内存中处理，不建立原文文件缓存；但助手回答展示或引用的资料文字（含本机检索摘录）会按保留问答的要求保存到聊天表，来源也会保留。清理缓存不能视为删除已保存的课程问答。

资料读取范围只接受固定的标准／扩展配置，并在每次检索开始时快照；最大为 200 页／张、单来源 100,000 字符、单次 400,000 字符。扩大读取范围不改变单份 8 MB 下载上限、PDF 解码／Office 解压保护、课程隔离或最多 8 个来源的限制，也不改变用户对联网发送资料的同意。切换选项不触发检索或模型请求；本机预览仍逐来源限制为 12,000 字符，保护问答存储上限。

公告已读与星标状态独立保存在本机，以课程 ID 和公告 ID 为联合键；仅保存这两个 ID、两个布尔标记及更新时间，不保存标题、正文、译文或 URL。状态更新不会请求 Canvas 的写入接口；清理内容缓存不会删除这些标记。

独立公告页的原文、重点摘录及中文译文仅在当前进程中使用，不写入 SQLite、日志、原文或译文文件。列表翻译优先处理当前可见公告；翻译结果不改变公告已读和星标状态，也不会向第三方发送任何公告内容。

数据库默认位置：

```text
%LOCALAPPDATA%\StudentAssistant\student_assistant.sqlite3
```

Windows 用户目录不可写时会回退到 `%TEMP%\StudentAssistant`。回退文件同样可能包含脱敏后的聊天正文，不提供额外加密；使用者应按个人学习记录保护该目录。

## AI 安全

`sanitize_for_ai()` 会递归移除大小写及常见拼写变体下的：

- access_token / refresh_token / token
- authorization / cookie / session / password
- api_key / secret
- email / student_id（默认）

AI 工具网关仅暴露固定的 Canvas 只读读取方法；不存在 raw request、CredentialStore、Shell 或 arbitrary URL 能力。Deadline 原始事实始终来自 Canvas `due_at`，空值不会被猜测。

课程资料检索器在模型边界之外持有 Canvas 连接，模型不接触凭证对象。它只读固定的课程、作业、Rubric、模块、页面和文件接口，不跟随文档中的任意网页链接；只解析同源 Canvas 文件及选定课程页面的身份。签名下载 URL 不进入来源，来源使用编号与规范 Canvas 路径。下载重定向不携带 Canvas Authorization，并受 HTTPS/公网目标限制。

检索最多保留 8 个来源、100,000 字符，单源 25,000 字符、单文件 8 MB；课程/作业索引分页、最多 8 个模块、页面、Office ZIP/XML、PDF 页数及解码资源都有上限。PDF 安全配置使用 `pypdf` 的上下文局部解码上限，并禁用外部 JBIG2 程序；Office XML 拒绝实体声明。不会执行宏、脚本或文档指令，不做隐式 OCR。歧义、权限不足、解析失败或达到限制均以提示披露，不能声明完整课程已读。

在线模型仅获得当前对话最近最多 40 条、合计最多 24,000 字符的完整用户/助手消息，错误消息不发送；本机完整历史不受这个推理窗口裁剪。课程证据和历史中的文字是非可信数据，不得改变系统策略、授权边界或截止时间事实。在线请求设置 `store: false`，不继承 ChatGPT 或 Codex 中的其他会话；这不等于对 OpenAI 服务的数据处理作额外承诺。[官方推理接入说明](https://developers.openai.com/siwc/token-sharing-open-source)

## ChatGPT 授权安全

OAuth 使用系统浏览器、随机 state/nonce、PKCE S256 和绑定 `127.0.0.1` 的临时 HTTP 回调；回调路径、Host、state、发行者及单次使用均校验。授权码只用于交换凭证，不写入普通存储或日志。不会读取 Codex 登录文件、浏览器 Cookie、已有登录令牌或学校登录密码；用户亲自完成认证。

ID Token 通过 OpenAI HTTPS 发现文档和 JWKS，使用 PyJWT 验证受限签名算法、签名、issuer、audience、有效期、subject 及登录 nonce。不会把 ID Token 放进授权 URL、`id_token_hint` 或其他 URL 提示。身份登录与 `chatgpt.tokens.use.direct` 套餐授权分别确认；缺少套餐使用权限不得请求推理。[官方注册与登录流程](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)

账户凭证与注册分开保存，刷新会序列化以避免旋转 Refresh Token 竞争；访问可用模型时读取当前账户目录，不写死某个模型。每次推理前检查资料发送偏好及当前账户，完成后再次确认；取消后不再开始新的课程资料请求。退出会清除本机登录凭证，远端撤销无法确认时提示用户到 ChatGPT 设置断开 App，不静默声称撤销成功。

当前验证包括模拟授权、凭证库、网络和流式响应。未经用户亲自登录及首次完整在线回答，不宣称某个真实账户、套餐或模型已经可用。

## Deadline 提醒

提醒只在本地 App 运行期间发送。通知数据库只保存 assignment ID、提醒类型和发送时间。默认跳过已提交作业，并通过 `(assignment_id, reminder_type)` 唯一键防止重复通知。

## 日志与异常

默认日志级别为 INFO，HTTP 客户端详细日志被关闭。集中日志过滤器会脱敏常见 credential key/value。发布版不写普通日志文件，并禁用窗口化完整 traceback，降低异常信息携带敏感 Header 的风险。

## 发布审计

发布前运行：

```powershell
python -m pytest -q
python scripts\security_audit.py
python scripts\security_audit.py --include-build
```

审计会检查回归 Secret、TLS 绕过、URL 中的凭证和 PyInstaller 数据收集规则；若安装了 gitleaks，可附加 `--gitleaks`。

报告安全问题时只使用脱敏测试数据，不要发送真实 Token、密码、Cookie、Session、数据库或完整异常转储。

