"""Two-daemon routing regressions; no real Docker or Azure calls."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from advisor_deploy.operations import Cli, CliFailure


DESKTOP = "npipe:////./pipe/dockerDesktopLinuxEngine"
DEFAULT = "npipe:////./pipe/docker_engine"


@pytest.fixture
def docker_pair(tmp_path, monkeypatch):
    script = tmp_path / "docker_stub.py"
    state = tmp_path / "state.json"
    log = tmp_path / "calls.jsonl"
    original = tmp_path / "original-config"
    original.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    state.write_text(json.dumps({
        "current_context": "desktop-linux",
        "contexts": {"desktop-linux": DESKTOP, "default": DEFAULT, "remote": "tcp://unapproved.example:2376"},
        "images": {DESKTOP: {}, DEFAULT: {"advisor-local:run": "stale-image"}},
    }), encoding="utf-8")
    script.write_text(
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "state_path=Path(os.environ['DOCKER_TEST_STATE']); s=json.loads(state_path.read_text())\n"
        "args=sys.argv[1:]; host=None\n"
        "if args and args[0]=='--host': host=args[1]; args=args[2:]\n"
        "config=os.environ.get('DOCKER_CONFIG',''); private=config.endswith('docker')\n"
        "with open(os.environ['DOCKER_TEST_LOG'],'a') as log:\n"
        " log.write(json.dumps({'args':sys.argv[1:],'command':args,'config':config,"
        "'context_env':os.environ.get('DOCKER_CONTEXT'),'host_env':os.environ.get('DOCKER_HOST')})+'\\n')\n"
        "if args[:2]==['context','show']:\n"
        " print(os.environ.get('DOCKER_CONTEXT') or ('default' if private else s['current_context']));sys.exit(0)\n"
        "if args[:2]==['context','inspect']:\n"
        " if private and args[2]!='default': sys.exit(22)\n"
        " print(s['contexts'][args[2]]);sys.exit(0)\n"
        "if host is None:\n"
        " context=os.environ.get('DOCKER_CONTEXT')\n"
        " if private and context and context!='default': sys.exit(22)\n"
        " host=s['contexts'][context] if context else (os.environ.get('DOCKER_HOST') or s['contexts']['default' if private else s['current_context']])\n"
        "images=s['images'][host]\n"
        "if args[0]=='info': print('linux')\n"
        "elif args[0]=='build': images[args[args.index('--tag')+1]]='verified-image'\n"
        "elif args[:2]==['image','inspect']: print(images.get(args[2], 'verified-image' if host==s['contexts']['desktop-linux'] else 'missing-image'))\n"
        "elif args[0]=='run': s['verified_on']=host\n"
        "elif args[0]=='login':\n"
        " assert sys.stdin.read().strip()=='fixture-token'\n"
        " Path(config,'config.json').write_text('{\"auths\":{\"fixture\":{\"auth\":\"private-fixture\"}}}')\n"
        "elif args[0]=='tag':\n"
        " source=images.get(args[1], 'verified-image' if args[1]=='sha256:verified' and host==s['contexts']['desktop-linux'] else None)\n"
        " if source is None: sys.exit(23)\n"
        " images[args[2]]=source\n"
        "elif args[0]=='push': s['published']=images[args[1]];s['published_on']=host\n"
        "state_path.write_text(json.dumps(s))\n",
        encoding="utf-8",
    )
    for name in ("DOCKER_CONTEXT", "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ADVISOR_DOCKER_COMMAND", str(script))
    monkeypatch.setenv("DOCKER_CONFIG", str(original))
    monkeypatch.setenv("DOCKER_TEST_STATE", str(state))
    monkeypatch.setenv("DOCKER_TEST_LOG", str(log))
    return SimpleNamespace(repo=tmp_path, private=private, state=state, log=log, original=original)


@pytest.mark.parametrize("workflow", ["aca", "observer"])
def test_verified_image_is_published_on_the_same_nondefault_daemon(docker_pair, workflow, monkeypatch):
    case = docker_pair
    cli = Cli("Apply", case.repo, SimpleNamespace(directory=case.private))
    assert cli.run(["info", "--format", "{{.OSType}}"], "readiness", tool="docker", raw=True) == "linux"
    source = "advisor-local:run" if workflow == "aca" else "sha256:verified"
    if workflow == "aca":
        cli.run(["build", "--tag", source, "."], "build", tool="docker", mutation=True, raw=True)
    assert cli.run(["image", "inspect", source], "inspect", tool="docker", raw=True) == "verified-image"
    cli.run(["run", "--rm", source], "verify", tool="docker", mutation=True, raw=True)
    (case.private / "docker").mkdir()
    # Even process environment drift must not reselect a daemon after verification.
    monkeypatch.setenv("DOCKER_HOST", DEFAULT)
    cli.run(["login", "fixture.azurecr.io", "--username", "fixture", "--password-stdin"],
            "login", tool="docker", mutation=True, raw=True, stdin="fixture-token\n")
    destination = "fixture.azurecr.io/approved:run"
    cli.run(["tag", source, destination], "tag", tool="docker", mutation=True, raw=True)
    cli.run(["push", destination], "push", tool="docker", mutation=True, raw=True)
    state = json.loads(case.state.read_text())
    assert state["published"] == "verified-image"
    assert state["published_on"] == state["verified_on"] == DESKTOP
    calls = [json.loads(line) for line in case.log.read_text().splitlines()]
    operations = [call for call in calls if call["command"][0] != "context"]
    assert all(call["args"][:2] == ["--host", DESKTOP] for call in operations)
    assert all(call["context_env"] is None and call["host_env"] is None for call in operations)
    assert len([call for call in calls if call["command"][:2] == ["context", "inspect"]]) == 1
    assert calls[-1]["config"] == str(case.private / "docker")
    assert not (case.original / "config.json").exists()


def test_context_environment_precedes_host_and_survives_private_credentials(docker_pair, monkeypatch):
    case = docker_pair
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")
    monkeypatch.setenv("DOCKER_HOST", "tcp://ignored.example:2376")
    cli = Cli("Apply", case.repo, SimpleNamespace(directory=case.private))
    cli.run(["info"], "readiness", tool="docker", raw=True)
    (case.private / "docker").mkdir()
    cli.run(["login", "fixture", "--password-stdin"], "login", tool="docker", mutation=True, raw=True, stdin="fixture-token\n")
    assert cli.run(["image", "inspect", "sha256:verified"], "inspect", tool="docker", raw=True) == "verified-image"
    calls = [json.loads(line) for line in case.log.read_text().splitlines()]
    assert all(call["args"][:2] == ["--host", DESKTOP] for call in calls if call["command"][0] != "context")


def test_explicit_local_host_precedes_saved_context(docker_pair, monkeypatch):
    case = docker_pair
    monkeypatch.setenv("DOCKER_HOST", DEFAULT)
    cli = Cli("Apply", case.repo, SimpleNamespace(directory=case.private))
    assert cli.run(["image", "inspect", "advisor-local:run"], "inspect", tool="docker", raw=True) == "stale-image"
    calls = [json.loads(line) for line in case.log.read_text().splitlines()]
    assert not any(call["command"][0] == "context" for call in calls)
    assert calls[0]["args"][:2] == ["--host", DEFAULT]


@pytest.mark.parametrize("settings", [
    {"DOCKER_HOST": "tcp://localhost:2375"},
    {"DOCKER_CONTEXT": "remote", "DOCKER_HOST": DESKTOP},
    {"DOCKER_HOST": "npipe:////remote-server/pipe/engine"},
])
def test_remote_or_unvalidated_endpoint_never_falls_back(docker_pair, monkeypatch, settings):
    case = docker_pair
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    cli = Cli("Apply", case.repo, SimpleNamespace(directory=case.private))
    with pytest.raises(CliFailure, match="docker"):
        cli.run(["info"], "readiness", tool="docker", raw=True)
    calls = [json.loads(line) for line in case.log.read_text().splitlines()] if case.log.exists() else []
    assert all(call["command"][0] == "context" for call in calls)
