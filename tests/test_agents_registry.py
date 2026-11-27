"""Agent CLI registry and install jobs (docs/agent-chat.md §2, §13), with fake CLIs and a fake npm -- no real
agent CLI, npm or installer ever runs here."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from agent_nexus import agent_cli
from agent_nexus.agents import installs as installs_mod
from agent_nexus.agents import registry
from agent_nexus.agents.installs import InstallError, Installs

FAKES = Path(__file__).parent / "fixtures" / "fake_agents"
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell shims")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """Only the test's own folders count: the machine's real /usr/local/bin (npm, claude...) must stay unseen."""
    monkeypatch.setattr(agent_cli, "candidates", lambda name, env: [])
    registry.invalidate()
    yield
    registry.invalidate()


def shim(folder: Path, name: str, script: str, *first: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    args = " ".join(f'"{a}"' for a in first)
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKES / script}" {args} "$@"\n', encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def box(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    env = {"PATH": str(bin_dir), "HOME": str(home), "FAKE_MARK": str(tmp_path / "calls.jsonl")}
    return {"home": home, "bin": bin_dir, "env": env, "tmp": tmp_path, "mark": tmp_path / "calls.jsonl"}


def calls(box) -> list[list[str]]:
    if not box["mark"].exists():
        return []
    return [json.loads(line) for line in box["mark"].read_text().splitlines()]


# -- definitions ---------------------------------------------------------------------------------------------

def test_builtin_definitions_cover_the_four_clis():
    defs = registry.definitions()
    assert set(defs) == {"claude", "codex", "dsh", "gemini"}
    assert defs["claude"].adapter == "claude-stream" and defs["codex"].adapter == "codex-exec"
    assert defs["dsh"].adapter == "acp" and defs["gemini"].adapter == "acp"
    for d in defs.values():
        assert set(d.install) == {"windows", "darwin", "linux"}
        assert d.install["linux"][:3] == ["npm", "install", "-g"]
        assert d.session_globs and d.command[0] == "{bin}"
    assert defs["dsh"].node_min == 22
    assert defs["codex"].permission_flags["readonly"] == ["--sandbox", "read-only"]
    assert defs["claude"].permission_flags["full"] == ["--dangerously-skip-permissions"]
    assert defs["claude"].install_alt and defs["codex"].install_alt and not defs["dsh"].install_alt
    assert defs["claude"].supports() == {"resume": True, "approvals": True, "stream": True}


def test_custom_overrides_add_and_hide_clis():
    cfg = {"agents": {"custom": {
        "codex": {"label": "Codex（公司版）", "permission_flags": {"full": ["--yolo"]}, "adapter": 5},
        "aider": {"label": "Aider", "binaries": ["aider"], "adapter": "text",
                  "command": ["{bin}", "--message", "{prompt}"], "install": ["pipx", "install", "aider-chat"]},
        "evil": {"label": "x", "adapter": "claude-stream"},  # custom CLIs cannot claim a built-in protocol
        "gemini": {"enabled": False},
        "Bad Name": {"label": "nope"},
        "broken": "not a dict",
    }}}
    defs = registry.definitions(cfg)
    assert "gemini" not in defs and "Bad Name" not in defs and "broken" not in defs
    assert defs["codex"].label == "Codex（公司版）" and defs["codex"].adapter == "codex-exec"
    assert defs["codex"].permission_flags["full"] == ["--yolo"]
    assert defs["codex"].permission_flags["readonly"] == ["--sandbox", "read-only"]  # the rest kept
    aider = defs["aider"]
    assert not aider.builtin and aider.binaries == ("aider",) and aider.install["linux"] == ["pipx", "install",
                                                                                            "aider-chat"]
    assert defs["evil"].adapter == "text"
    # the "agents" section alone works too
    assert "aider" in registry.definitions(cfg["agents"])


def test_command_templates():
    defs = registry.definitions()
    argv = registry.command_argv(defs["codex"], "/x/codex", permission="readonly",
                                 values={"workdir": "/w", "prompt": "hi", "system_prompt": "be brief"})
    assert argv == ["/x/codex", "exec", "--json", "--skip-git-repo-check", "-C", "/w", "--sandbox", "read-only",
                    "-c", "developer_instructions=be brief", "hi"]
    resumed = registry.command_argv(defs["claude"], "claude", permission="edits", template="resume_command",
                                    values={"resume_id": "abc"})
    assert resumed[-4:] == ["--resume", "abc", "--permission-mode", "acceptEdits"]
    assert "--append-system-prompt" not in resumed  # no system prompt, no flag
    ask = registry.command_argv(defs["claude"], "claude", permission="ask")
    assert ask[-2:] == ["--permission-prompt-tool", "{approval_tool}"]  # left for the runner


# -- locate --------------------------------------------------------------------------------------------------

def test_locate_on_path_and_not_installed(box):
    defs = registry.definitions()
    assert registry.locate(defs["gemini"], box["env"]) is None
    path = shim(box["bin"], "gemini", "registry_cli.py", "gemini")
    assert registry.locate(defs["gemini"], box["env"]) == str(path)


def test_locate_in_the_npm_global_prefix_off_path(box):
    """npm's prefix is on no PATH the service sees: ``npm config get prefix`` finds it."""
    prefix = box["tmp"] / "npm-global"
    shim(box["bin"], "npm", "registry_npm.py", "npm")
    dsh = shim(prefix / "bin", "dsh", "registry_cli.py", "dsh")
    env = {**box["env"], "FAKE_NPM_PREFIX": str(prefix)}
    assert registry.locate(registry.definitions()["dsh"], env) == str(dsh)
    # ~/.npmrc prefix= wins without running npm
    other = box["tmp"] / "other"
    dsh2 = shim(other / "bin", "dsh", "registry_cli.py", "dsh")
    (box["home"] / ".npmrc").write_text(f"prefix={other}\n")
    registry.invalidate()
    assert registry.locate(registry.definitions()["dsh"], env) == str(dsh2)


def test_locate_desktop_app_and_explicit_path(box):
    app = box["home"] / ".local" / "share" / "deepseek-harness" / "resources" / "runtime" / "cli" / "bin"
    dsh = shim(app, "dsh", "registry_cli.py", "dsh")
    if registry.os_key() == "linux":
        assert registry.locate(registry.definitions()["dsh"], box["env"]) == str(dsh)
    custom = shim(box["tmp"] / "opt", "my-agent", "registry_cli.py", "mine")
    defs = registry.definitions({"agents": {"custom": {"mine": {"label": "Mine", "path": str(custom)}}}})
    assert registry.locate(defs["mine"], box["env"]) == str(custom)



def test_locate_claude_bundled_with_the_desktop_app_newest_version(box, monkeypatch):
    # macOS, as seen on a real Mac: the Claude desktop app keeps versioned Claude Code builds
    monkeypatch.setattr(registry, "os_key", lambda platform=None: "darwin")
    base = box["home"] / "Library" / "Application Support" / "Claude" / "claude-code"
    old = shim(base / "2.1.40" / "aa" / "claude.app" / "Contents" / "MacOS", "claude", "registry_cli.py", "claude")
    new = shim(base / "2.1.331" / "bb" / "claude.app" / "Contents" / "MacOS", "claude", "registry_cli.py", "claude")
    assert old.exists()
    assert registry.locate(registry.definitions()["claude"], box["env"]) == str(new)

def test_expand_templates():
    env = {"HOME": "/h"}
    assert registry.expand("{DSH_HOME:.dsh}/.credentials.yaml", env) == os.path.join("/h", ".dsh") + "/.credentials.yaml"
    assert registry.expand("{DSH_HOME:.dsh}/x", {**env, "DSH_HOME": "/d"}) == "/d/x"
    assert registry.expand(".claude/a", env) == os.path.join("/h", ".claude/a")
    assert registry.expand("{LOCALAPPDATA}/a", env) is None


# -- probe ---------------------------------------------------------------------------------------------------

def test_probe_claude_logged_in_and_cached(box):
    shim(box["bin"], "claude", "registry_cli.py", "claude")
    d = registry.definitions()["claude"]
    env = {**box["env"], "FAKE_VERSION": "2.1.4", "FAKE_LOGGED_IN": "1"}
    result = registry.probe(d, env)
    assert result["installed"] and result["version"] == "2.1.4"
    assert result["login"] == "ok" and "claude.ai" in result["login_detail"]
    assert ["claude", "auth", "status"] in calls(box)
    count = len(calls(box))
    assert registry.probe(d, env) == result and len(calls(box)) == count  # cached
    registry.probe(d, env, fresh=True)
    assert len(calls(box)) > count


def test_probe_claude_logged_out_and_old_versions(box):
    shim(box["bin"], "claude", "registry_cli.py", "claude")
    d = registry.definitions()["claude"]
    out = registry.probe(d, {**box["env"], "FAKE_VERSION": "2.1.4", "FAKE_LOGGED_IN": "0"})
    assert out["login"] == "missing"
    # an API key counts even when auth status says no
    out = registry.probe(d, {**box["env"], "FAKE_VERSION": "2.1.4", "FAKE_LOGGED_IN": "0", "ANTHROPIC_API_KEY": "k"},
                         fresh=True)
    assert out["login"] == "ok"
    # too old for `auth status`: never asked (it would be taken as a prompt); the credentials file decides
    box["mark"].unlink()
    (box["home"] / ".claude").mkdir()
    (box["home"] / ".claude" / ".credentials.json").write_text("{}")
    out = registry.probe(d, {**box["env"], "FAKE_VERSION": "1.0.80"}, fresh=True)
    assert out["login"] == "ok" and out["version"] == "1.0.80"
    assert not any(call[1:3] == ["auth", "status"] for call in calls(box))


def test_probe_codex(box):
    shim(box["bin"], "codex", "registry_cli.py", "codex")
    d = registry.definitions()["codex"]
    out = registry.probe(d, {**box["env"], "FAKE_LOGGED_IN": "1"})
    assert out["login"] == "ok" and out["version"] == "1.2.3"  # stderr "Logged in using ChatGPT", exit 0
    assert registry.probe(d, {**box["env"], "FAKE_LOGGED_IN": "0"}, fresh=True)["login"] == "missing"
    (box["home"] / ".codex").mkdir()
    (box["home"] / ".codex" / "auth.json").write_text("{}")
    box["mark"].unlink()
    assert registry.probe(d, {**box["env"], "FAKE_LOGGED_IN": "0"}, fresh=True)["login"] == "ok"
    assert not any(call[1:3] == ["login", "status"] for call in calls(box))


def test_probe_dsh_and_gemini_by_keys_and_files(box):
    shim(box["bin"], "dsh", "registry_cli.py", "dsh")
    shim(box["bin"], "gemini", "registry_cli.py", "gemini")
    defs = registry.definitions()
    assert registry.probe(defs["dsh"], box["env"])["login"] == "unknown"
    assert registry.probe(defs["dsh"], {**box["env"], "DEEPSEEK_API_KEY": "sk"})["login"] == "ok"
    dsh_home = box["tmp"] / "dshhome"
    dsh_home.mkdir()
    (dsh_home / ".credentials.yaml").write_text("x")
    assert registry.probe(defs["dsh"], {**box["env"], "DSH_HOME": str(dsh_home)})["login"] == "ok"
    assert registry.probe(defs["gemini"], box["env"])["login"] == "missing"
    (box["home"] / ".gemini").mkdir()
    (box["home"] / ".gemini" / "oauth_creds.json").write_text("{}")
    assert registry.probe(defs["gemini"], box["env"], fresh=True)["login"] == "ok"


def test_probe_not_installed_and_slow_version(box):
    d = registry.definitions()["gemini"]
    assert registry.probe(d, box["env"]) == {"installed": False, "path": None, "version": None, "login": "unknown",
                                             "login_detail": "未安装"}
    shim(box["bin"], "gemini", "registry_cli.py", "gemini")
    started = time.monotonic()
    out = registry.probe(d, {**box["env"], "FAKE_SLOW": "5", "GEMINI_API_KEY": "k"}, timeout=0.5, fresh=True)
    assert time.monotonic() - started < 4
    assert out["installed"] and out["version"] is None and out["version_error"] == "版本检测超时"
    assert out["login"] == "ok"


def test_probe_all_and_info_shape(box):
    shim(box["bin"], "codex", "registry_cli.py", "codex")
    defs = registry.definitions()
    results = registry.probe_all(defs, {**box["env"], "FAKE_LOGGED_IN": "1"})
    assert set(results) == set(defs) and results["codex"]["installed"] and not results["claude"]["installed"]
    view = registry.info(defs["codex"], results["codex"], None)
    for key in ("kind", "label", "installed", "path", "version", "login", "install", "supports"):
        assert key in view
    assert view["supports"] == {"resume": True, "approvals": False, "stream": True}


# -- installs ------------------------------------------------------------------------------------------------

def wait_done(jobs: Installs, job_id: str, timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = jobs.get(job_id, 0)
        if job["state"] not in ("queued", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError("install job did not finish")


@pytest.fixture
def npm_box(box):
    shim(box["bin"], "npm", "registry_npm.py", "npm")
    shim(box["bin"], "node", "registry_npm.py", "node")
    prefix = box["tmp"] / "prefix"
    prefix.mkdir()
    box["prefix"] = prefix
    box["env"] = {**box["env"], "FAKE_NPM_PREFIX": str(prefix), "FAKE_LOGGED_IN": "1"}
    return box


def test_npm_install_runs_reprobes_and_reports_progress(npm_box):
    finished = []
    jobs = Installs(on_done=lambda kind, job: finished.append((kind, job["state"])))
    env = {**npm_box["env"], "FAKE_NPM_DELAY": "0.03"}
    job_id = jobs.start("codex", env, None)
    assert jobs.start("codex", env, None) == job_id  # one install per CLI at a time
    seen, cursor, lines = [], 0, []
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = jobs.get(job_id, cursor)
        seen.append(job["progress"])
        lines += job["log"]
        cursor = job["next"]
        if job["state"] not in ("queued", "running"):
            break
        time.sleep(0.02)
    assert job["state"] == "done", (job["error"], job["log"])
    assert job["progress"] == 100 and job["method"] == "npm"
    assert seen == sorted(seen) and any(0 < p < 100 for p in seen)
    assert any("http fetch" in line for line in lines) and any("added 12 packages" in line for line in lines)
    assert not any("\x1b" in line for line in lines)  # colours stripped
    assert any(line.startswith("> node scripts") for line in lines)  # \r splits lines
    assert job["result"]["installed"] and job["result"]["path"] == str(npm_box["prefix"] / "bin" / "codex")
    assert job["result"]["login"] == "ok"
    assert finished == [("codex", "done")]
    current = jobs.current("codex")
    assert current["state"] == "done" and current["progress"] == 100 and current["log_tail"]
    # the re-probe is what agent_info sees next
    assert registry.probe(registry.definitions()["codex"], env)["installed"]


def test_npm_argv_is_quiet_but_chatty_enough(npm_box):
    plan = Installs().plan("claude", npm_box["env"])
    assert plan["method"] == "npm" and plan["argv"][0] == str(npm_box["bin"] / "npm")
    assert plan["argv"][1:] == ["install", "-g", "@anthropic-ai/claude-code", "--no-fund", "--no-audit",
                                "--loglevel=http"]


def test_no_npm_is_a_clear_error_with_the_alternative(box):
    jobs = Installs()
    with pytest.raises(InstallError) as caught:
        jobs.start("dsh", box["env"], None)
    assert caught.value.code == "npm_missing"
    assert "没有找到 npm" in caught.value.message and "Node.js 22" in caught.value.message
    assert "nodejs.org" in caught.value.message and "桌面版" in caught.value.view()["hint"]
    with pytest.raises(InstallError) as caught:
        jobs.plan("claude", box["env"], method="npm")
    assert caught.value.code == "npm_missing" and "install.sh" in caught.value.message
    with pytest.raises(InstallError) as caught:
        jobs.plan("nope", box["env"])
    assert caught.value.code == "unknown_kind"


def test_no_npm_falls_back_to_the_official_installer(box):
    """auto: a person who wants Claude Code should not have to install Node.js first."""
    jobs = Installs(platform="linux")
    with pytest.raises(InstallError) as caught:  # no bash either: nothing can run
        jobs.plan("claude", box["env"])
    assert caught.value.code == "tool_missing"
    shim(box["bin"], "bash", "registry_npm.py", "bash")  # never run: only planned
    shim(box["bin"], "curl", "registry_npm.py", "curl")
    plan = jobs.plan("claude", box["env"])
    assert plan["method"] == "official" and plan["argv"][0] == str(box["bin"] / "bash")
    assert plan["argv"][-1] == "curl -fsSL https://claude.ai/install.sh | bash"
    assert "没有找到 npm" in plan["note"]


def test_node_too_old(npm_box):
    jobs = Installs()
    job = wait_done(jobs, jobs.start("dsh", {**npm_box["env"], "FAKE_NODE_VERSION": "v20.11.1"}, None))
    assert job["state"] == "failed" and job["code"] == "node_too_old"
    assert "Node.js 22" in job["error"] and "v20.11.1" in job["error"]
    assert not (npm_box["prefix"] / "bin" / "dsh").exists()


def test_node_too_old_switches_to_the_official_installer(npm_box, tmp_path):
    """The fake 'bash' plays the official installer: it drops a claude shim where agent_cli would look."""
    installer = tmp_path / "fake_official.sh"
    target = npm_box["prefix"] / "bin"
    installer.write_text(
        "#!/bin/sh\necho 'Downloading Claude Code 40%'\necho 'Downloading Claude Code 100%'\n"
        f"/bin/mkdir -p '{target}'\n"
        f"printf '#!/bin/sh\\nexec \"{sys.executable}\" \"{FAKES / 'registry_cli.py'}\" claude \"$@\"\\n' > '{target}/claude'\n"
        f"/bin/chmod +x '{target}/claude'\necho 'Installation complete'\n")
    installer.chmod(0o755)
    (npm_box["bin"] / "bash").symlink_to(installer)
    shim(npm_box["bin"], "curl", "registry_npm.py", "curl")
    jobs = Installs(platform="linux")
    env = {**npm_box["env"], "FAKE_NODE_VERSION": "v14.0.0", "FAKE_VERSION": "2.1.9"}
    job = wait_done(jobs, jobs.start("claude", env, None))
    assert job["state"] == "done", (job["error"], job["log"])
    assert job["method"] == "official" and job["result"]["version"] == "2.1.9"
    assert any("Node.js 版本太低" in line for line in job["log"])


@pytest.mark.parametrize("failure,code", [("eacces", "permission_denied"), ("network", "network"),
                                          ("exit", "exit_code")])
def test_install_failures_are_explained(npm_box, failure, code):
    jobs = Installs()
    job = wait_done(jobs, jobs.start("gemini", {**npm_box["env"], "FAKE_NPM_FAIL": failure}, None))
    assert job["state"] == "failed" and job["code"] == code
    assert job["error"].startswith("安装失败") and job["progress"] < 100
    assert job["error"] in job["log"]


def test_success_without_the_binary_is_a_failure(npm_box):
    jobs = Installs()
    job = wait_done(jobs, jobs.start("gemini", {**npm_box["env"], "FAKE_NPM_NO_SHIM": "1"}, None))
    assert job["state"] == "failed" and job["code"] == "not_found_after_install" and "PATH" in job["error"]


def test_cancel_and_timeout(npm_box):
    jobs = Installs()
    job_id = jobs.start("gemini", {**npm_box["env"], "FAKE_NPM_DELAY": "0.5"}, None)
    deadline = time.monotonic() + 10
    while not jobs.get(job_id, 0)["log"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert jobs.cancel(job_id)
    job = wait_done(jobs, job_id)
    assert job["state"] == "cancelled" and job["phase_label"] == "已取消"
    assert not jobs.cancel(job_id)

    slow = Installs(timeout_s=0.6)
    job = wait_done(slow, slow.start("gemini", {**npm_box["env"], "FAKE_NPM_DELAY": "0.5"}, None))
    assert job["state"] == "failed" and job["code"] == "timeout"


def test_log_paging_and_cap(npm_box, monkeypatch):
    monkeypatch.setattr(Installs, "MAX_LOG", 5)
    jobs = Installs()
    job = wait_done(jobs, jobs.start("gemini", npm_box["env"], None))
    assert job["state"] == "done"
    assert len(job["log"]) == 5 and job["next"] > 5 and job["dropped"]
    again = jobs.get(job["job_id"], job["next"])
    assert again["log"] == [] and again["next"] == job["next"]
    assert jobs.get("ij_missing", 0) is None


def test_progress_estimates():
    jobs = Installs()
    job = {"state": "running", "phase": "prepare", "progress": 0.0, "phase_at": time.time(), "fetches": 0,
           "log": [], "log_base": 0}
    jobs._estimate(job, "Downloading 50%")
    assert job["phase"] == "download" and 30 <= job["progress"] <= 40
    jobs._estimate(job, "> pkg postinstall")
    assert job["phase"] == "install" and job["progress"] >= 64
    jobs._estimate(job, "Downloading 10%")  # never back to an earlier phase
    assert job["phase"] == "install"
    jobs._estimate(job, "added 3 packages in 2s")
    assert job["progress"] == 88
    job["phase_at"] -= 3600  # a long quiet phase creeps toward its end but never reaches 100
    assert 88 <= jobs._progress(job) < 90



def test_progress_official_script_header_is_not_the_install_phase():
    # the official Codex PowerShell installer, as seen on a real Windows machine
    jobs = Installs()
    job = {"state": "running", "phase": "prepare", "progress": 0.0, "phase_at": time.time(), "fetches": 0,
           "log": [], "log_base": 0}
    jobs._estimate(job, "==> Installing Codex CLI")
    assert job["phase"] == "prepare"
    jobs._estimate(job, "==> Downloading Codex CLI")
    assert job["phase"] == "download" and job["progress"] < 60
    jobs._estimate(job, "Codex CLI 0.172.0 installed successfully.")
    assert job["phase"] == "install" and job["progress"] == 88

def test_writable_by_another_user(tmp_path):
    folder = tmp_path / "prefix"
    folder.mkdir()
    folder.chmod(0o755)
    owner = folder.stat().st_uid
    assert installs_mod._writable_by(str(folder), {"user": owner, "group": -1, "extra_groups": []})
    assert not installs_mod._writable_by(str(folder), {"user": owner + 4242, "group": owner + 4242,
                                                       "extra_groups": []})
    folder.chmod(0o777)
    assert installs_mod._writable_by(str(folder), {"user": owner + 4242, "group": -1, "extra_groups": []})


@pytest.mark.parametrize("config,remote,full", [
    ({}, True, True),
    ({"terminal": {"enabled": False}}, False, False),
    ({"terminal": {"enabled": False}, "agents": {"remote": True}}, True, False),
    ({"terminal": {"enabled": False}, "agents": {"remote": True, "allow_full_remote": True}}, True, True),
    ({"agents": {"enabled": False}}, False, True),
])
def test_remote_agents_follow_the_terminal_setting(tmp_path, config, remote, full):
    import json as _json

    from agent_nexus import registry as core_registry
    (tmp_path / core_registry.CONFIG_NAME).write_text(_json.dumps(config), encoding="utf-8")
    cfg = core_registry.load_manager_config(tmp_path)
    assert (cfg.agents_remote, cfg.agents_allow_full_remote) == (remote, full)
