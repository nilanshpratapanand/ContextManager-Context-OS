ContextOS: more modes, files and images, and a cleaner UI.

## Install

**Windows 10/11:** download `install.bat` below and double-click it.
**macOS / Linux:** download `install.sh` below and run `bash install.sh`, or:
```bash
curl -fsSL https://raw.githubusercontent.com/nilanshpratapanand/ContextManager-Context-OS/main/install.sh | bash
```
The installer finds or installs Python, downloads ContextOS, sets up its environment, creates `.env` and runs the tests. Run it again any time to update; your API keys and chats are kept. After that, `RUN.bat` / `run.sh` pull the latest changes on every start.

## What's new

- **Pipeline mode:** `/pipeline ...` splits a request into subtasks, sends each to the free model lane that suits it, runs independent ones in parallel and merges the results.
- **Attachments:** attach, drop or paste text, code, data files and images (images need a free Gemini key; PDFs need the optional `pypdf` package).
- **Local-only mode:** `LLM_LOCAL_ONLY=1` uses only models on your own machine.
- **Sampling profiles:** precise work gets a low temperature, open-ended writing a high one.
- **Theme control:** System / Light / Dark, live OS sync, deeper dark mode, better touch targets and phone layout.
- **Auto-update:** every start installs the newest commit on `main`.
- **Fixes:** build-index save race, Windows-style path escapes.
