English | [简体中文](README.zh-CN.md)

# Student Assistant

A local Windows desktop companion for Canvas courses, assignments, quizzes, announcements and deadlines. Built with Python and PySide6, with read-only access through the official Canvas API.

**[Download for Windows x64](https://github.com/zzzbbb-hcc/student-assistant/releases/download/v0.1.0/StudentAssistant-v0.1.0-windows-x64.zip)** · [Latest release and checksums](https://github.com/zzzbbb-hcc/student-assistant/releases/latest)

## Quick start

1. Download `StudentAssistant-v0.1.0-windows-x64.zip` from the link above or the release's **Assets** section.
2. Extract the entire ZIP. Keep `StudentAssistant.exe`, `_internal` and `models` together inside the `StudentAssistant` folder.
3. Run `StudentAssistant\StudentAssistant.exe`.
4. Open **Settings → Canvas Connection** and save your own Canvas Personal Access Token.

The portable release runs on **Windows 10/11, 64-bit (x64)** and does not require Python. GitHub's automatically generated **Source code** archives contain source files, not the runnable application. `SHA256SUMS.txt` on the release page lets you verify the downloaded ZIP.

**Current Canvas support:** the application is configured for the **University of Sydney Canvas instance**. It is not currently configurable for other institutions. Each user needs their own Canvas account and token, and their own login and authorization for the optional ChatGPT connection.

**Interface language:** the application combines English and Simplified Chinese labels. Announcement summaries and offline translations are presented in Chinese.

## Features

- Browse Canvas courses, assignments, announcements, Pages, Modules and course materials.
- View Classic and New Quizzes on a separate page, with readable quiz deadlines included in the Dashboard and global Deadlines view.
- See exact local due times, submission status, countdowns and desktop reminders while the app is running.
- Open task details and preview or explicitly save linked files.
- Read original assignment introductions and announcements alongside offline Simplified Chinese translations.
- Keep local conversations and groups, retrieve relevant course materials, and optionally request online answers through an explicitly authorized ChatGPT connection.

Canvas access is read-only. The app does not start quizzes, read quiz questions, submit answers or modify course content. Ed Discussion and Ed Lessons are not included in this release.

## Connect Canvas

1. Sign in to your university's Canvas website.
2. Go to **Account → Settings → Approved Integrations → New Access Token**.
3. Create a token for Student Assistant.
4. Paste it into **Settings → Canvas Connection** in the app and select **Save Token**.

To replace a token, enter the new value and save it. **Delete Token** removes the saved credential from Windows Credential Manager. After a restart, the app shows whether a token is saved without displaying its value.

Never put a real token in source code, issues, chat messages, screenshots, `.env` files or logs. The download does not contain a shared Canvas token: every user must provide their own.

## Deadlines, quizzes and reminders

**Assignments** groups work by course and submission status. **Deadlines** combines assignments and quizzes across courses: dated work that is not overdue comes first, undated work follows, and genuinely overdue work appears last. Submitted work is not marked overdue just because its due date has passed. Course and submission filters remain available.

The Dashboard's **Due Today**, **Due Tomorrow**, **Due This Week** and **Overdue** cards open the corresponding Deadlines filter. Background refresh preserves that selection; choose **All deadlines** to clear it.

Deadline facts come only from Canvas `due_at`. A missing value is shown as **No deadline**; the app does not infer dates from descriptions. Times use the current Windows time zone and include the full date, 24-hour time, zone and UTC offset. Quiz `lock_at` is an availability closing time, not a due date.

Quiz synchronization checks assignments, Classic Quizzes and New Quizzes, merging linked records by Canvas IDs. Some quiz APIs may be unavailable to a student or disabled for a course. **Coverage details** explains incomplete sources while retaining the work already loaded; it does not bypass access restrictions.

Configure reminders in **Settings → Deadline Reminders**. Defaults are 7 days, 3 days, 1 day and 6 hours before a deadline, plus one overdue reminder. Each task/reminder combination is sent once. Submitted work is skipped unless **Notify even after submitted** is enabled. **Test Notification** checks the local notification backend. Reminders are checked at startup and while the app is running; there is no background service after it closes.

## Read and download materials

Click a task in Assignments, Deadlines, the Dashboard or a course's Assignments tab to load its full introduction, current submission status and material links. Linked Canvas files open in a larger preview window. Use **Download / Save As…** to choose where to save them.

PDFs, images and common text/Office formats support in-app previews; other file types can be saved. The preview/download window currently limits an individual file to 25 MB. Files are written to disk only when explicitly saved, and signed download URLs are not persisted in SQLite.

Assignment introductions have resizable English and Chinese reading panes. Translation runs in the background and retains the original text if it fails. Attachments are not automatically downloaded or translated. Canvas's original instructions and `due_at` remain authoritative.

## Announcements and offline translation

The **公告** page lists readable, published announcements across courses, newest first. It extracts up to two key sentences locally, then translates the title, summary and body into Simplified Chinese. Translation prioritizes visible items and does not block reading the original.

Original text, extracted summaries and translations on this announcement page remain in memory for the current session. Read/unread and star markers are saved locally and do not change Canvas. Those markers survive refreshes, restarts and **Clear Cache**.

Offline translation uses the public Argos English–Chinese 1.9 / OPUS-MT model through CTranslate2 on the local CPU. It needs no AI key and sends no announcement or assignment text to a translation service. Session translations are not saved as separate original/translated-text files.

The Windows release includes the model in `models\en_zh`. Source users install it with `python scripts\setup_translation_model.py`; the download is approximately 71 MB, is checked against a fixed SHA-256, and only explicitly allowed model files are extracted. The model directory's `README.md` preserves its authorship and CC-BY 4.0 attribution.

## AI Study Assistant

### Conversations and course retrieval

Questions are saved locally before an answer is requested. Answers, sources and safe failure messages are retained even if a network request fails. Use course groups or **新建分组** to organize conversations; **重命名** and **移动到…** rename or move them without deleting their contents. Renaming a group does not rename the Canvas course.

For example, ask about a fictional course such as `DEMO1001 sample report`, or select its course group first. The app searches readable assignments, rubrics, Pages, Modules and related files. Ambiguous matches require clarification. Sources include Canvas paths and reference numbers. Retrieval itself requires a Canvas connection but does not by itself send the retrieved text to OpenAI.

PDF, DOCX, PPTX and common text files support text extraction. The reading-scope selector affects the next retrieval:

| Scope | PDF pages / presentation slides | Characters per source | Characters per request |
| --- | ---: | ---: | ---: |
| Standard · 50 pages | 50 | 25,000 | 100,000 |
| Extended · 200 pages (default) | 200 | 100,000 | 400,000 |

Both scopes allow up to 8 sources, inspect up to 8 relevant modules and limit each downloaded source file to 8 MB. Parsing and decompression protections also apply. The local preview shows up to 12,000 characters per source. Scanned documents require OCR; unavailable, unsupported or truncated material is disclosed rather than treated as fully read. Increasing these limits does not change the online model's actual context or account quota.

Original downloads and separate retrieval data are processed in memory. However, excerpts and references that become part of a saved answer are retained with that conversation. **Clear Cache does not remove those saved answers or excerpts.**

### Optional ChatGPT connection

1. Open **AI Assistant → 连接 ChatGPT → Continue with ChatGPT**.
2. Complete sign-in and review authorization yourself in the system browser.
3. Select a model from the models actually available to that account.
4. To request an online answer, enable the option allowing the current question, recent conversation and selected course evidence to be sent to OpenAI. This permission is off by default and can be withdrawn.

Without that connection and permission, the app does not request online inference; local lookups and authorized Canvas retrieval remain available. The connection does not import your other ChatGPT or Codex conversations. Eligible requests use the permissions and allowance granted by your ChatGPT account; the app does not silently switch to a separately billed API-key route.

**Availability depends on your account and workspace.** Complete your own authorization and verify a first successful online answer to confirm plan eligibility and model access. A signed-in state alone does not establish working inference. See the [official integration documentation](https://developers.openai.com/siwc/token-sharing-open-source).

Authorization uses the system browser, Authorization Code + PKCE and a temporary loopback callback. Credentials are protected in Windows Credential Manager. The app does not read Codex login files, browser cookies or existing session tokens. You can switch or sign out of accounts in the connection window; if remote revocation cannot be confirmed, disconnect the app in ChatGPT settings.

Online context uses complete messages from the current conversation, up to 40 recent user/assistant messages and 24,000 characters; older local history remains intact. Failure messages are excluded from inference context. Final saved responses are limited to 120,000 characters, with an explicit notice if truncated. The optional AI-key setting remains separate, is stored only in Credential Manager and is not used by this ChatGPT authorization route.

## Privacy, storage and cache

The published application package excludes saved Canvas tokens, AI keys, OAuth credentials, personal databases, course caches, chat histories and development screenshots. Each installation creates its own local data as its user works with the app.

| Data | Storage |
| --- | --- |
| Canvas PAT, optional AI key and ChatGPT credentials | Windows Credential Manager, service `StudentAssistant` |
| Cache, settings, conversations/groups, announcement markers and reminder history | `%LOCALAPPDATA%\StudentAssistant` |
| Fallback when that directory cannot be written | `%TEMP%\StudentAssistant` |
| Source-run logs | Current terminal by default; no normal log file |

Credentials are not stored in SQLite or shipped with the application. Conversation text is ordinary local SQLite data, **not an encrypted vault**. Common credential patterns are redacted before saving, but arbitrary secrets cannot be reliably detected. Do not paste passwords, tokens or personal information you do not want retained. Conversation history is not silently trimmed to the smaller online context window, and old messages never saved by earlier versions cannot be recovered.

Pages can show cached content immediately and refresh Canvas in the background. **Refresh** bypasses the cache. Default cache lifetimes are 24 hours for courses, 1 hour for assignments and 30 minutes for the existing course-detail announcement cache; the standalone announcements page refreshes its in-memory content on entry.

**Clear Cache removes content cache only. It preserves conversations, conversation groups, announcement read/star markers, reminder history, settings and credentials.** Closing the app, changing pages, renaming/moving conversations and signing out of ChatGPT also preserve local conversations.

For implementation boundaries and safe reporting guidance, see [SECURITY.md](SECURITY.md). Reports should contain synthetic or redacted examples, never real tokens, databases or exception dumps.

## Run from source

Requirements: Windows 10/11 and Python 3.12 or later.

```powershell
git clone https://github.com/zzzbbb-hcc/student-assistant.git
cd student-assistant
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python scripts\setup_translation_model.py
python -m app.main
```

Run the existing tests and release security checks:

```powershell
python -m pytest -q
python scripts\security_audit.py
```

Automated validation uses mocked authorization, credential storage and network responses. It does not establish that a real account, ChatGPT plan or model has been authorized successfully; that requires the user's sign-in and a first complete online answer.

## Build the Windows package

```powershell
python -m pip install -r requirements-build.txt
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

The build script runs tests and a pre-build audit, builds `dist\StudentAssistant\StudentAssistant.exe`, copies the fixed public translation model and audits the resulting package. Distribute the entire `dist\StudentAssistant` directory with `_internal` and `models` intact.

An optional local shortcut can be created after building:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1
```

The application uses a cream/yellow theme with bundled artwork and vector navigation icons. It uses an installed font from `Noto Sans SC`, `Microsoft YaHei UI`, `Microsoft YaHei` or `Segoe UI`; it does not install fonts. The icon source and regeneration notes are in [app/assets/icon-design.md](app/assets/icon-design.md).

## Remove the application

1. Delete the saved Canvas token and optional AI key in Settings. Sign out of saved ChatGPT accounts; disconnect the app in ChatGPT settings if remote revocation is not confirmed.
2. Close Student Assistant and delete its extracted application folder. Source users can also delete the checkout when no longer needed.
3. To remove local conversations and settings as well, first preserve anything needed, then delete `%LOCALAPPDATA%\StudentAssistant` and, if used, `%TEMP%\StudentAssistant`.
4. If credentials were not removed in the app, remove the `StudentAssistant` entries from **Windows Credential Manager → Windows Credentials**.
