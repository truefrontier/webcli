# webcli

webcli lets an AI agent use websites you're already signed in to, as you, from your own computer. The agent runs simple commands like `webcli amazon orders --since 30d` and gets back clean data (JSON) instead of clicking through web pages.

You'd use it when you want an agent to watch, gather, or report on things that only live behind a login, and there's no official API (a way for programs to talk to the site) you can use.

The same package also installs two related commands: `llmcli`, for chat AIs like Gemini and ChatGPT, and `chromecli`, for Chrome tabs you choose to share with an agent.

## How it works

1. **You sign in once.** `webcli identity login google:default` opens a normal Chrome window. You sign in the way you always do. webcli saves that sign-in (your *session*: the cookies that keep you logged in) in a Chrome profile folder on your computer, under `~/.webcli/`. webcli calls each saved sign-in an *identity*.
2. **The session stays on your machine.** There's no webcli server and no webcli account. The profile folder is locked to your user, kept out of Time Machine backups, Spotlight, and sync tools, and the cookies stay protected by the macOS Keychain.
3. **Agents run plain commands.** After sign-in, every command opens Chrome *hidden* (headless: no window, nothing on your screen), asks the site for the data the same way the site's own page does, and prints the result. When an agent runs it (output piped), the result is JSON. When you run it in a terminal, it's a readable table.

Each site has an *adapter*: a small piece of code that knows that one site's pages and requests. Adding a site means writing a new adapter. The existing ones live in [`webcli/sites/`](https://github.com/truefrontier/webcli/tree/main/webcli/sites).

webcli drives Chrome with Playwright, a browser-automation library. You don't need to know Playwright to use it.

## Quick start

webcli is built for macOS, and it needs Google Chrome.

```sh
pipx install truefrontier-webcli          # or: pip install truefrontier-webcli
                                          # or: brew install truefrontier/tap/webcli

webcli identity login google:default      # you do this, once: a Chrome window opens, sign in
webcli keep search picnic                 # your Google Keep notes that mention "picnic"
webcli doctor                             # health check: Chrome, sign-ins, folder security
webcli agent-help --format json           # every command, flag, and exit code, for an agent to read
```

Some ideas for a watchdog or project-manager agent, all read-only:

```sh
webcli amazon orders --since 30d --details    # recent orders, items, totals
webcli gsc pages example.com --json           # Search Console: indexed and not-indexed pages, and why
webcli linkedin feed --since 48h --limit 5    # the newest posts in your LinkedIn feed
webcli feed pull                              # several feeds pulled into one file
```

An agent can run these on a schedule, compare today's results with yesterday's, and tell you when something changes.

Only a person should run sign-ins. They open a visible window, and some ask for a password, a code, or a QR scan from your phone. No agent does that part.

## Supported sites

**`webcli`** (data sites):

- `aa`: Artificial Analysis leaderboards for AI models (needs their free API key; no Chrome).
- `walkie`: Walkie.app meeting transcripts and dictations, read from the app's local database (no Chrome, no network).
- `keep`: your Google Keep notes. Read-only.
- `photos`: your Google Photos, including downloads of the original files. Read-only.
- `youtube`: your watch history, subscriptions, uploads, and home feed. Read-only, except `playlist create`.
- `recorder`: your Pixel Recorder recordings, transcripts, and audio. Read-only.
- `contacts`: your Google Contacts. Read-only, except adding or removing a label.
- `gmail`: your Gmail filters. List them, or create or delete one.
- `notebooklm`: your NotebookLM notebooks, sources, and notes. Ask questions with citations, create notebooks, add sources, generate overviews and reports, and download them.
- `gsc`: Google Search Console: your sites, page indexing, URL inspection, performance, and sitemaps. Read-only, except submitting a sitemap and requesting indexing.
- `gnews`: your Google News "For you" feed. Read-only.
- `perplexity`: Perplexity Discover's news. Read-only.
- `linkedin`: your messages, activity, feed, post search, and Boost stats. Can send one DM or publish one post.
- `facebook`: your friends list. Read-only on Facebook.
- `loom`: your Loom library and official transcripts. Read-only.
- `messages`: your phone's texts, through Google Messages for web. Can send a text.
- `whatsapp`: your WhatsApp chats, through WhatsApp Web (unofficial). Can send a text to an existing chat.
- `amazon`: your orders, card charges and refunds, package tracking, and wish lists. Read-only.
- `feed`: pulls YouTube, Google News, Perplexity, and LinkedIn into one file, on a schedule if you like.

**`llmcli`** (chat AIs): `gemini`, `chatgpt`, `muse` (Meta's Muse), and `notebooklm`. Send a prompt, get the answer back. These send your prompt to that company's AI service, so they aren't local AI.

**`chromecli`**: reads, and with your OK acts in, the tabs you share from your everyday Chrome, through a small Chrome extension.

## Safety

- **Read-only by default.** Most commands only read. The few that change something (send a message, publish a post, create a filter, and so on) do nothing unless you add `--yes` (or its twin, `--confirm`). Without it, they show you what they *would* do and stop.
- **A guard blocks everything else.** Each site has a list of the exact requests its commands may make. The guard blocks any request that isn't on the list. That's what keeps read commands reading. (One catch: opening a WhatsApp chat may send read receipts.)
- **Sends check who they're going to.** A LinkedIn post or DM refuses unless the signed-in account is the author you set. A WhatsApp send needs the exact chat name. A text to a new conversation needs `--new-ok` and every recipient spelled out.
- **Logs stay local and redacted.** Errors and activity go to `~/.webcli/logs/`, with private details stripped out. chromecli logs every action it takes, and `gsc` logs its writes.
- **Your sign-ins don't leave the machine.** The only network traffic goes to the sites themselves. Two exceptions, both opt-in: `webcli feed push` sends your feed file to a URL you give it, and chromecli's optional Jev feature sends to api.typesafe.ai only if you give it a TypeSafe key.

## Learn more

- **Full reference** (every identity, sign-in details, every example, exit codes, writes, privacy details, chromecli, and developing webcli): [docs/reference.md](https://github.com/truefrontier/webcli/blob/main/docs/reference.md)
- **Per-site docs:**
  - Google sites (Keep, Photos, YouTube, Recorder, Contacts, Gmail): [docs/google-data-sites.md](https://github.com/truefrontier/webcli/blob/main/docs/google-data-sites.md)
  - LinkedIn, Facebook, Loom: [docs/social-data-sites.md](https://github.com/truefrontier/webcli/blob/main/docs/social-data-sites.md)
  - NotebookLM: [docs/notebooklm-site.md](https://github.com/truefrontier/webcli/blob/main/docs/notebooklm-site.md)
  - Search Console: [docs/gsc-site.md](https://github.com/truefrontier/webcli/blob/main/docs/gsc-site.md)
  - Amazon: [docs/amazon-site.md](https://github.com/truefrontier/webcli/blob/main/docs/amazon-site.md)
  - Google Messages: [docs/messages-site.md](https://github.com/truefrontier/webcli/blob/main/docs/messages-site.md)
  - WhatsApp: [docs/whatsapp-site.md](https://github.com/truefrontier/webcli/blob/main/docs/whatsapp-site.md)
  - Feeds (Perplexity, Google News, YouTube, `webcli feed`): [docs/feed.md](https://github.com/truefrontier/webcli/blob/main/docs/feed.md)
  - chromecli: [docs/chromecli.md](https://github.com/truefrontier/webcli/blob/main/docs/chromecli.md)
- **What's new:** [CHANGELOG.md](https://github.com/truefrontier/webcli/blob/main/CHANGELOG.md)

MIT license.
