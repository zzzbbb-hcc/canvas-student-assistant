# Student Assistant

Windows 本地桌面学习助手，使用 Python 3.12、PySide6、Canvas 官方只读 API、Windows Credential Manager 和 SQLite。

当前版本包括：

- Canvas 课程、作业、公告、Pages、Modules 与课件预览
- Quizzes 独立测验页面（Classic / New Quizzes），并将可读取的测验截止时间并入全局 Deadlines
- Dashboard、Deadline Center 和本地时区倒计时
- 首页四个统计卡片可点击，联动显示相应日期范围内的作业和测验
- 中文公告页、本机重点摘录、完整原文和离线中文翻译
- 运行期间的 Windows Deadline 提醒与去重
- 本地持久缓存，以及进入页面时后台刷新
- 学习助手的本机问答记录、课程/自定义分组、Canvas 资料检索与可选 ChatGPT 授权连接
- Windows 可执行程序打包

根据当前产品决定，Ed Discussion 和 Ed Lessons 不包含在此版本中。

## 最快启动方式

在上一级“学习助手”文件夹双击带淡黄色书本图标的 `Student Assistant.lnk`，可直接运行打包后的程序。原来的 `启动 Student Assistant.bat` 仍保留作备用：优先运行 EXE；如果尚未打包，则运行项目虚拟环境中的源码版本。

程序、窗口与快捷方式统一使用 `app/assets/student-assistant-butter-v1.ico` 的淡黄色图标。重新导出图标可运行 `.venv\Scripts\python.exe scripts\build_icon.py --replace`；打包后运行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1` 创建或更新本项目的快捷方式。图标设计提示词和生成方式见 `app/assets/icon-design.md`。

界面采用与图标协调的奶油淡黄主题：暖棕文字、蜂蜜黄按钮、奶白卡片和少量蜜桃／鼠尾草绿点缀。导航、统计卡片和课程标签使用原创手绘感 SVG 小图标，缩放不依赖 emoji 或额外下载。字体优先使用本机已有的 `Noto Sans SC`，缺少时回退到 `Microsoft YaHei UI`、`Microsoft YaHei`、`Segoe UI`；不会自动安装字体。作业、课程、公告、对话及阅读窗口共用主题配色，仍保留紧凑布局和清楚的提交／逾期状态。

打包完成后也可以直接运行：

```text
dist\StudentAssistant\StudentAssistant.exe
```

整个 `dist\StudentAssistant` 文件夹必须保留，不能只移动其中的 EXE。

## 从源码安装

环境要求：Windows 10/11、Python 3.12 或更高版本。

```powershell
cd student-assistant
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m app.main
```

运行测试：

```powershell
python -m pytest -q
python scripts\security_audit.py
```

## Canvas PAT

1. 登录学校 Canvas。
2. 打开 `Account → Settings`。
3. 在 `Approved Integrations` 区域选择 `New Access Token`。
4. 创建仅供 Student Assistant 使用的 Token。
5. 在 App 的 `Settings → Canvas Connection` 中输入并保存。

不要把真实 PAT 放进聊天、源码、截图、`.env` 或日志。

- Replace：在同一输入框输入新 PAT，再点击 `Save Token`。
- Delete：点击 `Delete Token`，凭证会从 Windows Credential Manager 删除。
- App 重启后只显示是否已保存，不会重新显示真实 PAT。

## Deadline 与提醒

- Assignments 按课程分组，每门课下分别列出未提交、状态暂不可用、已提交作业；顶部可筛选课程和提交状态。
- Deadlines 不按课程或提交状态分组：未到期项目按截止时间从近到远排列，然后是无截止时间项目，真正已逾期项目放在最后；每张卡片保留课程名称。默认显示待完成作业和测验（未提交及状态暂不可用）。选择 `Submitted` 可看已提交项目，选择 `All statuses` 可看全部；已提交项目不会仅因截止日期已过而被当作逾期。
- 点击 Dashboard 的 Due Today、Due Tomorrow、Due This Week 或 Overdue，会打开相应日期范围的 Deadlines，清除之前的课程筛选并显示待完成作业和测验。日期范围也可以在 Deadlines 顶部切回 `All deadlines`，后台刷新会保留当前范围。
- 每张作业卡片和详情都会显示提交状态；已提交作业保留截止时间，Canvas 有提交时间时也会显示。
- 截止时间显示完整年月日、24 小时制时:分、本地时区和 UTC 偏移。

- Deadline 的唯一事实来源是 Canvas 返回的 `due_at`。
- `due_at = null` 始终显示 `No deadline`，不会根据描述猜日期。
- 时间转换为当前 Windows 本地时区。
- 已提交作业默认不发送后续提醒；可在 Settings 开启 `Notify even after submitted`。
- 默认规则：7 天、3 天、1 天、6 小时以及一次 Overdue 提醒。
- 同一作业的同一提醒类型只发送一次。
- 当前版本只在 App 启动及运行期间检查，不安装 Task Scheduler 或云端后台服务。

提醒设置和 `Test Notification` 位于 `Settings → Deadline Reminders`。

## Quizzes 测验截止时间

左侧 `Quizzes` 单独列出测验，默认显示全部提交状态，跨课程按截止时间从近到远排列，真正已逾期的测验置底。每项显示课程、准确到分钟的截止时间、倒计时及 Canvas 可确认的提交状态。点击可读取测验介绍和中英对照；不会开始测验、读取题目或提交答案。

同步同时检查作业列表、Classic Quizzes 列表和 New Quizzes 列表，按 Canvas ID 合并同一计分测验，不按标题猜测。练习测验或没有出现在作业列表中的可见测验也会补入全局 Deadlines、Dashboard 和提醒。与作业关联的测验优先保留当前学生的作业 `due_at` 和提交状态；独立测验使用其 API `due_at`。`lock_at` 是关闭时间，不会被当成截止时间；没有 `due_at` 则显示 `No deadline`，排在有截止日期的未逾期项目后、逾期项目前。

部分学校/课程未启用 New Quizzes API 或学生角色无读取权限；这时保留已读取的作业/测验，并显示“测验信息可能不完整”的提示，点 `Coverage details` 可查看来源。权限不可见、未发布或 API 未返回的测验无法保证读取，App 不会绕过访问限制或把接口失败当成“没有测验”。

## 查看作业与下载资料

在 Assignments、Deadlines、Dashboard 的作业卡片或课程详情的 Assignments 标签中，单击作业即可打开完整详情。详情会从 Canvas 重新读取作业说明、当前提交状态和资料链接。

点击说明中的 Canvas 文件链接，或右侧 `Assignment materials` 中的文件，即可进入大窗口预览。点击 `Download / Save As…` 后选择保存位置；PDF、图片和常见文本/Office 文件支持 App 内预览，其他类型可直接保存。目前单个文件限制为 25 MB。文件仅在明确选择保存时写入本地，带签名的下载地址不会进入 SQLite 缓存。

## 公告

左侧 `公告` 汇总各课程已发布的可读公告，按发布时间从新到旧排列，可筛选课程。列表从原文中本机提取最多两句重点，突出作业、测验、截止时间、变更和提醒，再将标题与重点摘录自动翻译为中文。翻译在后台运行，优先处理当前可见公告；等待或翻译失败时保留原文，不阻塞列表。中文翻译及摘录仅供参考，以 Canvas 原文为准，也不会把文字中的日期当作作业截止时间。

单击公告可同时查看保留段落的 Canvas 原文和后台生成的本机离线中文翻译；翻译失败时原文仍可阅读并可重试。标题、摘要和正文翻译均使用本机离线模型，不调用云端 AI，不需要 AI Key，不向第三方发送公告内容。新增公告页的原文、摘录及译文只保留在当前进程内，不写入正文或译文文件；重启后重新读取。不能读取的课程会单独提示，刷新失败不会清空已显示内容。

公告支持本机“已读 / 未读”和重点星标：新公告默认未读，打开详情后自动标为已读，也可以手动切回未读。点击卡片右侧的空心星标可标为重点，再点一次取消。顶部的阅读状态、星标和课程筛选可组合使用；中文翻译更新不会改变已读或重点标记。

这些标记只保存在本机，不修改 Canvas 上的阅读状态；重启、刷新或清理内容缓存后仍会保留。状态表只保存课程 ID、公告 ID、已读与星标标记，不保存公告正文、译文或链接。

## 缓存与自动刷新

页面打开时会先显示本地缓存，再在后台向 Canvas 检查最新数据，避免每次进入都白屏等待。手动 Refresh 总是绕过缓存。

默认 TTL：

- Courses：24 小时
- Assignments：1 小时
- Announcements：课程详情已有的安全缓存副本为 30 分钟；新汇总页正文和译文仅保留在进程内，每次进入后台刷新

非敏感 SQLite 数据通常位于：

```text
%LOCALAPPDATA%\StudentAssistant\student_assistant.sqlite3
```

若该目录不可写，App 会安全回退到 `%TEMP%\StudentAssistant`，而不是启动闪退。Settings 中的 `Clear Cache` 只删除内容缓存，不删除聊天记录、对话分组、公告阅读/星标标记、提醒历史、设置或凭证。

## 作业介绍中英对照（本机离线）

点击任何作业，在“作业介绍 / Assignment introduction”中同时阅读 Canvas 英文原文和简体中文译文；上下阅读区可拖动分隔条调整大小。中文在后台生成，原文不会因翻译失败而隐藏。附件仍通过右侧 Assignment materials 单独预览和下载，不会自动下载或翻译附件。

翻译使用 Argos 英译中 1.9 的公开 OPUS-MT 模型，通过 CTranslate2 在本机 CPU 运行，不使用 AI Key，不向第三方发送作业正文。译文只在当前进程中缓存，重启后不会留下正文/译文文件。中文仅供参考，作业要求及截止时间以 Canvas 原文和 `due_at` 为准。Canvas 没有提供介绍时不会自行补写。

开发环境首次运行前，在安装 `requirements.txt` 后运行 `python scripts\setup_translation_model.py` 下载约 71 MB 的模型。安装脚本验证固定 SHA-256，只提取明确允许的模型文件；不加载包中的 Stanza/Pickle 文件。发布构建会把公开模型放在 EXE 同级的 `models\en_zh` 中，使用发布版不需要再次下载。模型来源及 CC-BY 4.0 作者署名保存在该目录的 README.md。

## AI Study Assistant

AI 页面支持本机问答记录、课程资料检索，以及用户主动授权后的 ChatGPT 在线回答。没有连接或没有允许发送资料时，不请求在线模型；仍可查询已经加载的截止任务，并通过自己的 Canvas 连接检索可读资料。Canvas 检索本身需要联网，但不会因此把内容发送给 OpenAI。

### 保留问答与整理分组

- 每次发送先保存问题，再请求回答；回答、来源和安全的失败提示也会保留。即使网络失败或回答未完成，已经保存的问题仍可继续查看。
- 课程加载后，分组菜单会按课程建立独立分组；也可以点击 `新建分组` 创建自己的分类。`重命名分组` 不会修改 Canvas 名称，后续同步也不会覆盖手动名称。
- 每组可以有多个 `新对话`，支持对话 `重命名`、`移动到…`。移动和重命名不会清空已有问答；重启、切换页面、清理内容缓存和退出 ChatGPT 都不会删除记录。
- 完整历史保存在本机 SQLite，并在该对话中显示。在线模型从当前对话最近最多 40 条用户/助手消息中选择合计最多 24,000 字符的完整消息，优先保留最近的有效提问，跳过放不下的超长回答；原记录不裁剪。较旧内容仍保留，只是不全部发送给模型。失败提示不进入模型上下文。
- 新功能启用前已经关闭、且旧版本从未保存的临时问答无法追溯恢复。此功能也不会导入你在 ChatGPT 或 Codex 中的其他聊天记录。

普通问题和回答按文本保存；常见 Token、Authorization、Cookie、密码、API Key 等形态在写入前脱敏。SQLite 不是加密保险箱，请不要粘贴密码、令牌或不希望留在本机的个人信息；不带任何标识的任意秘密无法保证自动识别。

### 自动查找作业要求和相关课件

可以输入类似“5891 group report 怎么写”或先选择课程分组再提问。App 会尝试按课程代码和作业名称定位目标，从 Canvas 读取作业介绍、结构化评分标准（Rubric）、关联页面和附件，并按标题及作业涉及的概念寻找相关课件。课程或作业有多个相近结果时会要求确认，不会猜选其中一个。

支持 PDF、DOCX、PPTX 和常见文本的文字提取；结果显示来源编号和 Canvas 路径。未连接在线模型时展示本机读取到的资料；连接并授权后，可把这些资料作为写作分析的依据。截止时间仍只采用 Canvas `due_at`，不从课件或历史问答推断。

`AI Assistant` 顶部可切换资料读取范围，选择会保存在本机，重启后仍生效；只影响下一次检索，不会改动已有问答，也不会自动向在线模型发送资料：

- `扩展 · 200 页`（默认）：PDF 最多 200 页、PPT 最多 200 张，单个来源最多 100,000 字符，单次合计最多 400,000 字符。
- `标准 · 50 页`：PDF 最多 50 页、PPT 最多 50 张，单个来源最多 25,000 字符，单次合计最多 100,000 字符。

两种模式都最多保留 8 个来源、遍历 8 个相关模块，单份下载资料仍限 8 MB。页数和字符数任一达到上限都会停止，并提示实际限制。列表分页、Office 解压和 PDF 解码仍有独立的安全上限，因此扩大范围不代表已读完整门课；扫描 PDF 仍需 OCR。无权限、不支持格式、解析失败或触及安全上限的资料不会作为已读依据。

本机检索预览每个来源仅展示前 12,000 字符，避免超长摘录挤满历史记录；这与提取上限不同，在线回答可使用上述范围内的已提取文字。扩大范围可能增加读取耗时和模型额度消耗；实际在线模型的上下文和账户额度限制仍适用，失败时会保留问题并明确提示，不会声称已完成分析。

最终回答在脱敏、附加读取提示后最多保留 120,000 字符；超过时会明确标注截断，避免回答已完成却无法保存。此限制不同于读取资料的 400,000 字符上限，不会因此把发送给模型的资料退回到旧读取范围。

原始下载文件和独立检索数据仅在内存中处理，不自动落盘。不过，为了保留问答，助手回答中展示或引用的资料文字（包括本机检索结果）及来源会随聊天记录保存。`Clear Cache` 不会清除这些已经成为问答的内容。

### 连接自己的 ChatGPT

1. 打开 `AI Assistant`，点击 `连接 ChatGPT`。
2. 点击 `Continue with ChatGPT`，在系统浏览器中亲自登录、选择账户/工作区并审核授权。App 不读取 Codex 的登录文件、现有 Cookie 或其他会话令牌。
3. 读取当前账户可用模型，并从列表中选择。App 不写死某个模型名称，账户实际可用模型以返回列表及请求结果为准。
4. 需要在线回答时，勾选“允许将当前问题、当前对话的近期记录及相关课程资料发送给 OpenAI”。此选项默认关闭，可随时取消；不勾选时只进行本机查询/Canvas 检索。

ChatGPT 登录和套餐使用是不同权限；账户或工作区是否符合条件、是否授权套餐使用，以及模型是否实际可用，需要用户完成授权及首次成功回答才能确认。符合条件的请求使用用户的 ChatGPT 套餐或可用额度，此连接不会自动切换为另行计费的 API Key 路径。授权不会提供 ChatGPT 的其他聊天记录。[OpenAI 官方说明](https://developers.openai.com/siwc/token-sharing-open-source)

连接使用 Authorization Code + PKCE、仅监听 `127.0.0.1` 的临时回调和经过签名/发行者/受众校验的 ID Token；登录凭证分块保存在 Windows Credential Manager，不放入 SQLite。App 不把 ID Token 放进授权 URL 或登录提示参数。可在连接窗口切换或退出账户；远端撤销未获确认时，会提示前往 ChatGPT 设置断开 App。[官方账户与会话指南](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)

当前自动化验证使用模拟授权、凭证库和网络响应；真实账户登录、套餐额度以及首次在线回答必须由用户亲自完成后验证，不能把按钮可点击或“已登录”视为已经成功调用模型。

Settings 中原有的可选 AI Key 仍只安全保存在 Credential Manager，可 `Save / Replace Key` 或 `Delete Key`；输入框不会回填明文。本机模式及上述 ChatGPT 授权路线均不使用该 Key。

## Ed 状态

根据当前产品决定，本版本不实现 Ed Discussion 或 Ed Lessons，因此没有 Ed Token 输入项，也没有未确认的 Ed API 请求。以后若重新纳入 Ed，必须先确认官方、稳定、只读 API，再单独实现和审计。

## 数据位置与日志

- Canvas PAT、可选 AI Key、ChatGPT OAuth 连接凭证：Windows Credential Manager，服务名 `StudentAssistant`
- SQLite/缓存/聊天问答与分组/提醒去重：`%LOCALAPPDATA%\StudentAssistant`
- 源码运行日志：仅当前终端，默认不写日志文件
- EXE：`dist\StudentAssistant`

App 管理的凭证不写入 SQLite、缓存或打包产物。聊天内容写入前会做常见凭证形态脱敏，但请勿依赖自动识别来保管任意秘密。

## Windows 打包

```powershell
python -m pip install -r requirements-build.txt
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

构建脚本会先运行测试和安全审计，再创建 `dist\StudentAssistant\StudentAssistant.exe`，最后扫描构建产物。

## 完全卸载

1. 在 Settings 删除 Canvas Token 和可选 AI Key，并在 AI 页连接窗口逐个退出已保存的 ChatGPT 账户；远端撤销未确认时，到 ChatGPT 设置断开此 App。
2. 关闭 App。
3. 删除 `dist\StudentAssistant` 和项目目录（若不再需要源码）。
4. 如不再需要保留问答，删除 `%LOCALAPPDATA%\StudentAssistant`；若曾发生路径回退，也删除 `%TEMP%\StudentAssistant`。此操作会一并删除本机聊天记录和分组，请先保留需要的数据。
5. 如未先在 App 中删除凭证，可打开 Windows `Credential Manager → Windows Credentials`，删除服务名为 `StudentAssistant` 的条目。

详细安全边界见 [SECURITY.md](SECURITY.md)。

## 未来升级到 Canvas OAuth2

如果未来需要多用户分发，建议从个人 PAT 迁移到 Canvas 官方 OAuth2 Authorization Code 流程：使用系统浏览器完成登录与授权，回调只接收短期授权码，Access/Refresh Token 仍只写入 Windows Credential Manager，并实现到期刷新、撤销授权、最小权限与明确的重新登录提示。不要把 Refresh Token 放入 SQLite、日志、URL 查询参数或打包文件；OAuth Client Secret 也不应硬编码进公开桌面客户端。

