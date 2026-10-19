# Setting it up with an AI assistant

You can have an AI assistant such as Claude or ChatGPT walk you through setup.
It explains each step, runs or gives you the commands, reads the output, and
fixes problems as they come up. This page is for you, the human. The
assistant's own instructions are in [SETUP_AGENT.md](../SETUP_AGENT.md); you
hand it that file at the start.

If you'd rather do it yourself, see [setup-manual.md](setup-manual.md). It is
also a useful reference while you work with an assistant.

---

## Which kind of assistant

**An assistant that can run commands on your computer** (Claude Code, or a
similar coding agent in your terminal or editor). This is the smoothest path.
It installs everything, runs the checks and reads the results itself, and asks
your permission before each command. You still do the parts that need you
(below).

**A chat assistant** (Claude or ChatGPT in a browser or the desktop app). It
can't touch your computer, so it gives you one command at a time, you run it in
a terminal, and you paste the output back. This works fine; it is just slower.

Either way, the result is the same: a warehouse on your machine that Claude
Desktop can query. (ChatGPT can guide the setup, but it can't query this
warehouse directly afterwards: its connectors reach MCP servers over the
internet, and this one is built to stay on your own machine or network behind a
simple token.)

## What stays your job

An assistant can't do these for you, and one following the instructions
won't try:

- **Signing in to each platform** (Shopify, Meta, Google, Amazon, TikTok, ...)
  and creating the app, key or token there. Some platforms make you wait for
  approval; Google Ads and Amazon Ads can take days, so start them early.
- **Clicking "Allow"** on sign-in screens.
- **Typing secrets into the `.env` file** yourself, in a text editor.
- **Approving anything that changes a live account or the outside world:**
  ad changes, publishing, opening the server to your network, scheduled tasks.
- **Deciding** how much history to load and which platforms to include.

## The one rule: secrets never go in the chat

Never paste an API key, access token, password, client secret or refresh token
into the conversation. Put them straight into `.env` in a text editor.

- Chat history is stored by the provider and stays readable in your account.
  A key pasted there is a key you should consider exposed.
- The assistant doesn't need the values. To check your work it runs
  `python env_check.py`, which shows only whether each key is set, empty or
  missing, never the value. That output is safe to paste.
- An assistant following SETUP_AGENT.md will never ask for a secret. **If one
  asks, say no.**
- If you paste one by accident, revoke that key in the platform's console,
  create a new one, and put the new one in `.env`.

With an assistant that runs commands, also know that it can technically read
files on your computer, including `.env`. Its instructions tell it not to print
`.env` or any secret. Read each command before you approve it, and decline
anything that would display `.env` (for example `cat .env` or `type .env`).

## How to start

**With an assistant that runs commands:**

1. Install [Git](https://git-scm.com/downloads) and
   [Python 3.11+](https://www.python.org/downloads/) if you don't have them.
   The assistant can check for you.
2. Clone the repository (or let the assistant do it), open the assistant in
   that folder, and paste the starter prompt below.

**With a chat assistant:**

1. Give it the instructions. Either upload `SETUP_AGENT.md` (download it from
   the repository), paste its contents into the chat, or, if your assistant can
   browse the web, give it this link:
   `https://raw.githubusercontent.com/Colinbashor/ecomm-mcp-server/main/SETUP_AGENT.md`
2. Paste the starter prompt below.

**Starter prompt** (edit the bracketed parts):

```text
Please set up the ecomm-mcp-server warehouse for me by following SETUP_AGENT.md
exactly, including its rules about secrets.

- My computer: [Windows / macOS / Linux]
- Platforms I want to connect: [e.g. Shopify, Meta Ads, Google Ads]
- How I'll ask questions afterwards: [Claude Desktop / not sure]
- Will this computer be on overnight for a daily refresh? [yes / no / not sure]

Start by checking what I already have installed, then go step by step and tell
me what I need to do in each platform's website.
```

## What the session looks like

1. **Questions.** Your system, which platforms, how you'll use it.
2. **Install.** Python environment and dependencies (~10 minutes).
3. **Sample data.** A trial run with made-up numbers so you can see it work,
   and Claude Desktop connected to it. The sample data is deleted before step 4.
4. **One platform at a time.** For each, the assistant tells you what to create
   on the platform's website, you fill in `.env`, it checks with `env_check.py`
   and runs a small test pull. Budget 15 minutes for an easy platform and longer
   for Google Ads, Amazon or TikTok Shop.
5. **History.** A bigger pull for each working platform, once you've decided
   how far back.
6. **Daily schedule and health checks**, so it keeps itself up to date and tells
   you when something stops.
7. **Hand-off.** A summary of what's connected, what runs when, and how to check
   it's healthy.

You can stop after any step and pick it up later. In a new session, tell the
assistant what's already done; it can confirm with `env_check.py`,
`warehouse_health.py` and `last_sync_status`.

## Think twice before approving

- Any command with `--execute` or `--yes`, or any script ending in `_mutate.py`.
  These change live ad accounts. Setup never needs them.
- `--host 0.0.0.0`, or anything else that opens the server to your network.
  Read [SHARING.md](../SHARING.md) first.
- Deleting `warehouse.db` **after** it holds real data. Some of it (inventory,
  rank and price snapshots) can never be downloaded again.
- Creating a scheduled task. Fine when you've asked for it; make sure you know
  when it runs.
- `git add`, `git commit` or `git push` that would include `.env`, or anything
  that uploads your data somewhere.

## If something goes wrong

Ask the assistant to run `python warehouse_health.py` and
`python env_check.py`, and to check the
[troubleshooting table](setup-manual.md#10-troubleshooting). Each platform's
page under `docs/` has a Notes section covering its known traps, and the
assistant's instructions tell it to read the page before guessing.
