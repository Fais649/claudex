# Claudex

**A local, subscription-aware failover runner for Claude Code and Codex CLI.**

Claudex is for people who use more than one official coding-agent subscription
and want work to continue when a provider is rate-limited or signed out. It
runs the installed `claude` and `codex` CLIs locally; it is not an API proxy,
does not require provider API keys, and keeps credentials in the profiles those
CLIs already manage.

## What it does

- Tries provider profiles in your chosen order.
- Remembers rate-limit/login failures and waits for the reported reset time.
- Starts routine work on a fast model and can continue the same provider
  session on a stronger model after an `[[ESCALATE]]` hand-off.
- Keeps Claude account profiles separate via `CLAUDE_CONFIG_DIR`.
- Translates Codex `exec --json` events into Claude stream-json events, making
  it suitable for Claude-compatible local-agent adapters.

## Install

Claudex is currently an alpha package. Install from a checkout:

```sh
git clone https://github.com/Fais649/claudex.git
cd claudex
python3 -m pip install .
mkdir -p ~/.config/claudex
cp claudex.example.toml ~/.config/claudex/config.toml
```

You need working local installations and logins for whichever providers you
enable:

```sh
claude --version
codex --version
claudex --claudex-help
```

## Configure

The example configuration intentionally uses generic profile names. The
provider list defines both names and priority:

```toml
[router]
providers = ["claude-work", "codex", "claude-personal"]
reasoning_effort = "medium"
cooldown_seconds = 900
state_dir = "~/.local/state/claudex"
input_protocol = "stream-json"

[providers.claude-work]
kind = "claude"
config_dir = "~/.claude-work"
fast_model = "sonnet"
strong_model = "opus"

[providers.codex]
kind = "codex"
# fast_model and strong_model are optional: Claudex discovers current
# Luna/Sol models from Codex's local model cache when omitted.

[providers.claude-personal]
kind = "claude"
config_dir = "~/.claude-personal"
fast_model = "sonnet"
strong_model = "opus"
```

Set `CLAUDEX_CONFIG=/path/to/config.toml` or pass `--config /path/to/config.toml`
to use a non-default file. `CLAUDEX_STATE_DIR` overrides the configured state
directory, which is useful for containers and tests.

`input_protocol = "stream-json"` is the default for a host that talks to
Claude's non-interactive streaming protocol. Use `plain` only when the host
writes a single plain-text task to stdin.

## Use as a Claude-compatible runner

Claudex forwards normal Claude Code flags while taking ownership of model,
reasoning-effort, resume, and escalation settings:

```sh
printf '%s\n' \
  '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"Summarise this repository."}]}}' \
  | claudex -p --input-format stream-json --output-format stream-json --verbose
```

When a provider returns a recognized usage, rate-limit, or authentication
failure, Claudex records a cooldown in its state directory and tries the next
provider. A successful Claude session can be resumed with the usual
`--resume <session-id>` flag. Moving a session between Claude profiles copies
only its session transcript; crossing from Claude to Codex deliberately starts
a fresh provider session with a recovery note.

## Safety and limits

- This is an alpha project; test it with non-critical tasks first.
- It invokes official local CLIs using the accounts already authenticated on
  the host. Review each provider's terms and subscription limits.
- The state directory contains session identifiers and routing logs. Protect
  it like other local developer metadata.
- It never uploads a profile or token itself, but a tool it launches naturally
  sends its prompt and tool results to that provider.

## Development

```sh
python3 -m pip install -e '.[dev]'
pytest
python3 -m build
```

## License

MIT.
