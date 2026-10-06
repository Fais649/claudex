#!/usr/bin/env python3
"""Claudex: subscription-aware Claude Code and Codex CLI failover.

Claudex accepts the Claude stream-json protocol, tries configured providers in
order, persists cooldowns/sessions locally, and translates Codex CLI events
back to the Claude event protocol for compatible agent hosts.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from zoneinfo import ZoneInfo

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - enforced by pyproject
    tomllib = None

HOME = os.path.expanduser("~")
SELF = os.path.realpath(__file__)
APP_NAME = "Claudex"


def _first_executable(*paths):
    for p in paths:
        if p and os.access(p, os.X_OK) and os.path.realpath(p) != SELF:
            return p
    return None


CLAUDE_BIN = _first_executable(os.environ.get("CLAUDE_CODE_EXECUTABLE"), shutil.which("claude"), "/usr/bin/claude")
CODEX_BIN = _first_executable(shutil.which("codex"), os.path.join(HOME, ".local/bin/codex"), "/usr/local/bin/codex")
STATE_DIR = os.environ.get("CLAUDEX_STATE_DIR") or os.path.join(HOME, ".local/state/claudex")
COOLDOWNS = os.path.join(STATE_DIR, "cooldowns.json")
SESSIONS = os.path.join(STATE_DIR, "sessions.json")
LOG = os.path.join(STATE_DIR, "auto.log")
PROVIDERS = []
EFFORT = "medium"
DEFAULT_COOLDOWN = 15 * 60
MARKER = "[[ESCALATE]]"
STREAM_INPUT = True


def _expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))


def configure(path: str | None) -> None:
    """Load a TOML configuration. Environment variables override its state dir."""
    global CLAUDE_BIN, CODEX_BIN, STATE_DIR, COOLDOWNS, SESSIONS, LOG
    global PROVIDERS, EFFORT, DEFAULT_COOLDOWN, STREAM_INPUT
    path = path or os.environ.get("CLAUDEX_CONFIG") or os.path.join(HOME, ".config/claudex/config.toml")
    if not os.path.exists(path):
        raise SystemExit(f"{APP_NAME}: configuration not found: {path}. Copy claudex.example.toml first.")
    with open(path, "rb") as f:
        data = tomllib.load(f)
    router = data.get("router", {})
    names = router.get("providers", [])
    tables = data.get("providers", {})
    if not names:
        raise SystemExit(f"{APP_NAME}: router.providers must contain at least one provider name.")
    PROVIDERS = []
    for name in names:
        spec = dict(tables.get(name, {}))
        if spec.get("kind") not in ("claude", "codex"):
            raise SystemExit(f"{APP_NAME}: providers.{name}.kind must be 'claude' or 'codex'.")
        spec["name"] = name
        if spec.get("config_dir"):
            spec["config"] = _expand(spec["config_dir"])
        PROVIDERS.append(spec)
    EFFORT = router.get("reasoning_effort", "medium")
    DEFAULT_COOLDOWN = int(router.get("cooldown_seconds", 900))
    STATE_DIR = os.environ.get("CLAUDEX_STATE_DIR") or _expand(router.get("state_dir", "~/.local/state/claudex"))
    COOLDOWNS, SESSIONS, LOG = (os.path.join(STATE_DIR, name) for name in ("cooldowns.json", "sessions.json", "claudex.log"))
    STREAM_INPUT = router.get("input_protocol", "stream-json") == "stream-json"
    CLAUDE_BIN = _expand(router.get("claude_bin", CLAUDE_BIN or "claude"))
    CODEX_BIN = _expand(router.get("codex_bin", CODEX_BIN or "codex"))

ESCALATION_PROMPT = (
    "You are running on a fast, cost-efficient model. Handle routine work yourself. "
    "If the task needs deeper reasoning than you can reliably deliver (complex architecture, "
    "subtle debugging, risky or wide-reaching changes, or you are stuck after a genuine attempt), "
    "hand it off instead of guessing: stop, and end your final message with a line of the form\n"
    f"{MARKER} <one-line reason>\n"
    "A stronger model then continues this same session with your full context. "
    "Only do this when it is really necessary."
)
TAKEOVER_PROMPT = (
    "You are now the stronger model, taking over this session from the faster model. "
    "Its hand-off reason: {reason}\n"
    f"Continue the task to completion. Do not emit {MARKER} yourself."
)
SWITCH_NOTE = (
    "Note: a previous agent run on this task was interrupted (its provider hit a usage limit). "
    "Check the current state of the workspace and the issue before continuing; "
    "some steps may already be done."
)

LIMIT_RE = re.compile(
    r"hit your .{0,20}limit|usage limit|limit reached|out of credits|insufficient.{0,10}(credit|quota)"
    r"|quota exceeded|rate.?limit|429|too many requests",
    re.I,
)
AUTH_RE = re.compile(
    r"log ?in again|not logged in|please run /login|invalid api key|authentication|unauthori[sz]ed|401"
    r"|oauth token has expired|token expired|credit balance is too low",
    re.I,
)
RESET_RE = re.compile(r"resets\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*\(([^)]+)\)", re.I)

out_lock = threading.Lock()


# ---------------------------------------------------------------- utilities

def log(msg, to_multica=True):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} [{os.getpid()}] {msg}\n")
    if to_multica:
        emit({"type": "log", "log": {"level": "info", "message": f"{APP_NAME}: {msg}"}})


def emit(obj):
    with out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save(path, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = path + f".{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def available(p):
    return load(COOLDOWNS).get(p["name"], 0) <= time.time()


def cool_down(p, text):
    until = time.time() + DEFAULT_COOLDOWN
    m = RESET_RE.search(text or "")
    if m:
        try:
            hour, minute, ampm, tzname = int(m[1]), int(m[2] or 0), (m[3] or "").lower(), m[4].strip()
            if ampm == "pm" and hour < 12:
                hour += 12
            if ampm == "am" and hour == 12:
                hour = 0
            now = dt.datetime.now(ZoneInfo(tzname))
            reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if reset <= now:
                reset += dt.timedelta(days=1)
            until = reset.timestamp()
        except Exception:
            pass
    data = load(COOLDOWNS)
    data[p["name"]] = until
    save(COOLDOWNS, data)
    log(f"{p['name']} unavailable until {dt.datetime.fromtimestamp(until).strftime('%a %H:%M')}: {(text or '').strip()[:160]}")


def remember_session(sid, p, model):
    if not sid:
        return
    data = load(SESSIONS)
    data[sid] = {"provider": p["name"], "model": model, "at": time.time()}
    if len(data) > 2000:  # keep the newest entries only
        data = dict(sorted(data.items(), key=lambda kv: kv[1].get("at", 0))[-1000:])
    save(SESSIONS, data)


def codex_models():
    """Latest listed Luna and Sol slugs from Codex's model cache."""
    best = {}
    try:
        data = load(os.path.join(HOME, ".codex/models_cache.json"))
        for m in data.get("models", []):
            mt = re.fullmatch(r"gpt-(\d+(?:\.\d+)*)-(luna|sol)", m.get("slug", ""))
            if mt and m.get("visibility", "list") == "list":
                ver = tuple(int(x) for x in mt[1].split("."))
                if ver > best.get(mt[2], ((0,), ""))[0]:
                    best[mt[2]] = (ver, m["slug"])
    except Exception:
        pass
    return best.get("luna", (0, "gpt-6-luna"))[1], best.get("sol", (0, "gpt-6.1-sol"))[1]


def models(p):
    if p["kind"] == "claude":
        return p.get("fast_model", "sonnet"), p.get("strong_model", "opus")
    fast, strong = codex_models()
    return p.get("fast_model", fast), p.get("strong_model", strong)


def failure_kind(text):
    if not text:
        return None
    if LIMIT_RE.search(text):
        return "limit"
    if AUTH_RE.search(text):
        return "auth"
    return None


def escalation_reason(text):
    for line in reversed((text or "").strip().splitlines()):
        if MARKER in line:
            return line.split(MARKER, 1)[1].strip() or "no reason given"
    return None


def prompt_text(user_line):
    try:
        content = json.loads(user_line)["message"]["content"]
        if isinstance(content, str):
            return content
        return "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
    except Exception:
        return user_line


def user_line(text):
    if not STREAM_INPUT:
        return text
    return json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})


# ------------------------------------------------------------- arg parsing

VALUE_FLAGS = {"--output-format", "--input-format", "--permission-mode", "--model", "--effort", "--resume",
               "--max-turns", "--mcp-config", "--settings", "--disallowedTools", "--append-system-prompt",
               "--append-system-prompt-file", "--add-dir", "--setting-sources"}
OWNED = {"--model", "--effort", "--resume", "--append-system-prompt", "--append-system-prompt-file"}


def parse_args(argv):
    passthrough, owned, i = [], {}, 0
    while i < len(argv):
        a = argv[i]
        key, val = (a.split("=", 1) + [None])[:2] if a.startswith("--") and "=" in a else (a, None)
        if key in VALUE_FLAGS and val is None and i + 1 < len(argv):
            val, i = argv[i + 1], i + 1
        if key in OWNED:
            owned[key] = val
        elif val is not None and key in VALUE_FLAGS:
            passthrough += [key, val]
        else:
            passthrough.append(a)
        i += 1
    return passthrough, owned


# ------------------------------------------------------------ stdin pump

class Stdin:
    """Reads Multica's stdin once and forwards later lines to the active child."""

    def __init__(self):
        self.child = None
        self.lock = threading.Lock()
        if STREAM_INPUT:
            self.first = sys.stdin.readline()
            threading.Thread(target=self._pump, daemon=True).start()
        else:
            self.first = sys.stdin.read()

    def _pump(self):
        for line in sys.stdin:
            with self.lock:
                c = self.child
            if c and c.stdin and not c.stdin.closed:
                try:
                    c.stdin.write(line)
                    c.stdin.flush()
                except Exception:
                    pass

    def attach(self, child):
        with self.lock:
            self.child = child


# ---------------------------------------------------------------- legs

class LegResult:
    def __init__(self):
        self.session = None
        self.result = None      # final Claude-format result event
        self.failure = None     # "limit" / "auth" / None
        self.text = ""          # final message text
        self.worked = False     # any tool use happened


def run_claude(p, model, stdin, first_line, passthrough, resume, append_prompt):
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    env.pop("ANTHROPIC_API_KEY", None)
    env["CLAUDE_CONFIG_DIR"] = p["config"]
    # Paperclip invokes Claude with a plain `-p` command. Always request the
    # structured event stream ourselves so that limit errors can be detected
    # and routed to the next provider rather than returned to the agent.
    output_flags = []
    if "--output-format" not in passthrough:
        output_flags = ["--output-format", "stream-json", "--verbose"]
    cmd = [CLAUDE_BIN, *passthrough, *output_flags, "--model", model, "--effort", EFFORT]
    if append_prompt:
        cmd += ["--append-system-prompt", append_prompt]
    if resume:
        cmd += ["--resume", resume]
    child = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=env, text=True, bufsize=1)
    stdin.attach(child)
    if STREAM_INPUT:
        child.stdin.write(first_line if first_line.endswith("\n") else first_line + "\n")
        child.stdin.flush()
    else:
        child.stdin.write(first_line)
        child.stdin.close()
    r = LegResult()
    # Hold events back until the model really answers, so a provider that
    # fails straight away (limit, expired login) leaves no trace in Multica.
    held, committed = [], False
    for line in child.stdout:
        try:
            ev = json.loads(line)
        except Exception:
            continue
        t = ev.get("type")
        if ev.get("session_id"):
            r.session = ev["session_id"]
        if t == "result":
            r.result, r.text = ev, ev.get("result") or ""
            if ev.get("is_error"):
                r.failure = failure_kind(r.text)
            try:
                child.stdin.close()
            except Exception:
                pass
            continue
        if t == "assistant":
            msg = ev.get("message") or {}
            if msg.get("model") != "<synthetic>" and not ev.get("error"):
                committed = True
            for b in msg.get("content") or []:
                if b.get("type") == "tool_use":
                    r.worked = True
        if committed or t == "control_request":
            for h in held:
                emit(h)
            held = []
            emit(ev)
        else:
            held.append(ev)
    child.wait()
    stderr = child.stderr.read() if child.stderr else ""
    stdin.attach(None)
    if not r.failure:
        for h in held:
            emit(h)
    if r.result is None:
        r.result = {"type": "result", "subtype": "error_during_execution", "is_error": True,
                    "result": stderr.strip() or f"claude exited with code {child.returncode} without a result",
                    "session_id": r.session}
        r.failure = failure_kind(r.result["result"])
    return r


def run_codex(p, model, prompt, resume):
    cfg = ["-c", f"model_reasoning_effort={EFFORT}",
           "-c", "shell_environment_policy.inherit=all",
           "-c", "shell_environment_policy.ignore_default_excludes=true"]
    common = ["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "-m", model, *cfg]
    cmd = [CODEX_BIN, "exec", "resume", resume, *common, "-"] if resume else [CODEX_BIN, "exec", *common, "-"]
    child = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, bufsize=1)
    child.stdin.write(prompt)
    child.stdin.close()
    r, usage, error, started = LegResult(), None, None, time.time()
    announced = False

    def announce():
        # Deferred until the first real item, so a provider that fails
        # straight away leaves no trace in Multica.
        nonlocal announced
        if not announced:
            announced = True
            emit({"type": "system", "subtype": "init", "session_id": r.session, "model": model})

    def assistant(blocks):
        announce()
        emit({"type": "assistant", "session_id": r.session,
              "message": {"id": "msg_" + uuid.uuid4().hex, "role": "assistant", "model": model, "content": blocks}})

    def tool_result(tid, content, is_error=False):
        announce()
        emit({"type": "user", "session_id": r.session, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": is_error}]}})

    for line in child.stdout:
        try:
            ev = json.loads(line)
        except Exception:
            continue
        t, item = ev.get("type"), ev.get("item") or {}
        it, iid = item.get("type"), "cx_" + str(item.get("id", uuid.uuid4().hex))
        if t == "thread.started":
            r.session = ev.get("thread_id")
        elif t == "item.started" and it == "command_execution":
            r.worked = True
            assistant([{"type": "tool_use", "id": iid, "name": "Bash", "input": {"command": item.get("command", "")}}])
        elif t == "item.completed":
            if it == "agent_message":
                r.text = item.get("text", "")
                assistant([{"type": "text", "text": r.text}])
            elif it == "reasoning" and item.get("text"):
                assistant([{"type": "thinking", "thinking": item["text"]}])
            elif it == "command_execution":
                tool_result(iid, item.get("aggregated_output", ""), item.get("exit_code") not in (0, None))
            elif it == "file_change":
                r.worked = True
                changes = item.get("changes") or []
                assistant([{"type": "tool_use", "id": iid, "name": "Edit", "input": {"changes": changes}}])
                tool_result(iid, "\n".join(f"{c.get('kind')} {c.get('path')}" for c in changes))
            elif it in ("mcp_tool_call", "web_search"):
                r.worked = True
                name = item.get("tool") or item.get("query") or it
                assistant([{"type": "tool_use", "id": iid, "name": str(name), "input": item.get("arguments") or {}}])
                tool_result(iid, json.dumps(item.get("result") or item.get("error") or ""))
            elif it == "error":
                error = item.get("message")
        elif t == "turn.completed":
            usage = ev.get("usage") or {}
        elif t in ("turn.failed", "error"):
            error = (ev.get("error") or {}).get("message") if t == "turn.failed" else ev.get("message")
    child.wait()
    stderr = child.stderr.read() if child.stderr else ""

    u = {"input_tokens": (usage or {}).get("input_tokens", 0), "output_tokens": (usage or {}).get("output_tokens", 0),
         "cache_read_input_tokens": (usage or {}).get("cached_input_tokens", 0), "cache_creation_input_tokens": 0}
    failed = usage is None
    if failed:
        error = error or stderr.strip() or f"codex exited with code {child.returncode}"
        r.failure = failure_kind(error)
    r.result = {"type": "result", "subtype": "error_during_execution" if failed else "success", "is_error": failed,
                "result": error if failed else r.text, "session_id": r.session, "model": model,
                "duration_ms": int((time.time() - started) * 1000), "usage": u,
                "modelUsage": {model: {"inputTokens": u["input_tokens"], "outputTokens": u["output_tokens"],
                                       "cacheReadInputTokens": u["cache_read_input_tokens"],
                                       "cacheCreationInputTokens": 0}}}
    return r


def copy_claude_session(sid, src_cfg, dst_cfg):
    """Make a Claude session resumable from another account's config dir."""
    for path in glob.glob(os.path.join(src_cfg, "projects", "*", f"{sid}.jsonl")):
        rel = os.path.relpath(path, src_cfg)
        dst = os.path.join(dst_cfg, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(path, dst)
        return True
    return False


# ------------------------------------------------------------------ main

def main():
    argv = sys.argv[1:]
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config")
    parser.add_argument("--claudex-help", action="store_true")
    own, argv = parser.parse_known_args(argv)
    if own.claudex_help:
        print("Claudex routes Claude Code and Codex CLI subscriptions.\n"
              "Usage: claudex [--config PATH] -p [Claude Code flags]\n"
              "Set CLAUDEX_CONFIG or copy claudex.example.toml to ~/.config/claudex/config.toml.")
        return 0
    configure(own.config)
    if "-p" not in argv and "--print" not in argv:
        # Version probes and other non-task invocations: plain Claude Code.
        env = dict(os.environ, CLAUDE_CONFIG_DIR=PROVIDERS[0]["config"])
        os.execve(CLAUDE_BIN, [CLAUDE_BIN, *argv], env)

    passthrough, owned = parse_args(argv)
    extra_prompt = owned.get("--append-system-prompt")
    base_prompt = ESCALATION_PROMPT + ("\n\n" + extra_prompt if extra_prompt else "")
    stdin = Stdin()
    original = stdin.first
    task_text = prompt_text(original)

    resume = owned.get("--resume")
    prev = load(SESSIONS).get(resume) if resume else None
    prev_provider = next((p for p in PROVIDERS if prev and p["name"] == prev["provider"]), None)
    interrupted = None  # (provider, session) of a leg that hit a limit mid-run

    for p in PROVIDERS:
        if not available(p):
            continue
        fast, strong = models(p)
        model, sid, note = fast, None, None
        # Continue an earlier session where possible.
        src = interrupted or ((prev_provider, resume) if prev_provider else None)
        if src:
            sp, ssid = src
            if sp is p:
                sid = ssid
            elif sp["kind"] == "claude" and p["kind"] == "claude" and copy_claude_session(ssid, sp["config"], p["config"]):
                sid = ssid
            else:
                note = SWITCH_NOTE
            if sid and prev and sp is prev_provider and not interrupted:
                model = prev.get("model", fast)
        log(f"using {p['name']} ({model}{', resuming' if sid else ''})")

        if p["kind"] == "claude":
            first = original
            if interrupted and sid:
                first = user_line("Your previous turn was cut off by a usage limit on another account. Continue the task.")
            elif note:
                first = user_line(note + "\n\n" + task_text)
            r = run_claude(p, model, stdin, first, passthrough, sid, base_prompt)
        else:
            brief = ""
            if os.path.exists("CLAUDE.md") and not sid:
                with open("CLAUDE.md") as f:
                    brief = "Task brief (CLAUDE.md):\n" + f.read() + "\n\n"
            text = task_text if not (interrupted and sid) else "Continue the task."
            prefix = "" if sid else ESCALATION_PROMPT + "\n\n"
            r = run_codex(p, model, prefix + brief + ((note + "\n\n") if note else "") + text, sid)

        if r.failure:
            cool_down(p, r.result.get("result"))
            if r.session and r.worked:
                interrupted = (p, r.session)
            continue

        remember_session(r.session, p, model)
        reason = escalation_reason(r.text) if model == fast else None
        if reason and not r.result.get("is_error"):
            log(f"escalating {p['name']} from {fast} to {strong}: {reason}")
            takeover = TAKEOVER_PROMPT.format(reason=reason)
            if p["kind"] == "claude":
                r2 = run_claude(p, strong, stdin, user_line(takeover), passthrough, r.session, base_prompt)
            else:
                r2 = run_codex(p, strong, takeover, r.session)
            if r2.failure:
                cool_down(p, r2.result.get("result"))
                log("strong model unavailable; returning the fast model's result")
            else:
                r = r2
                remember_session(r.session, p, strong)
        emit(r.result)
        return 0

    log("no provider available")
    emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
          "result": f"{APP_NAME}: every configured provider is at its usage limit or signed out. "
                    f"See {LOG}.",
          "session_id": resume})
    return 1


if __name__ == "__main__":
    sys.exit(main())
