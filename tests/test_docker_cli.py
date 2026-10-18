import subprocess

import pytest

from gamedock.backends import DockerCliBackend
from gamedock.backends import docker_errors
from gamedock.errors import BackendError, DockerUnavailableError, ForeignContainerError
from gamedock.models import ContainerSpec, PortMapping, Status, VolumeMapping, is_reserved_env_name

OURS = "running|0|false|true\n"
STOPPED = "exited|0|false|true\n"
MISSING = (1, "", "Error response from daemon: No such container: gamedock-x")
FOREIGN = (0, "running|0|false|\n", "")


class Recorder:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        rc, out, err = self.results.pop(0)
        return subprocess.CompletedProcess(argv, rc, out, err)

    def argvs(self):
        return [c[0] for c in self.calls]


SPEC = ContainerSpec(
    "gamedock-x", "img:1", (PortMapping("udp", 1000, 2000),),
    (VolumeMapping("/h", "/c"),), {"SECRET": "hunter2"}, {"a": "b"},
)


# ------------------------------------------------------------------ command construction
def test_create_args_do_not_contain_env_values():
    args = DockerCliBackend.build_create_args(SPEC)
    assert "hunter2" not in " ".join(args)
    assert args[-2:] == ["--", "img:1"]  # '--': the image can never be read as an option
    assert "1000:2000/udp" in args and "type=bind,source=/h,target=/c" in args
    i = args.index("-e")
    assert args[i + 1] == "SECRET"


def test_create_sets_restart_policy_and_stop_timeout():
    args = DockerCliBackend.build_create_args(SPEC, stop_timeout=90)
    assert args[args.index("--restart") + 1] == "unless-stopped"
    assert args[args.index("--stop-timeout") + 1] == "90"
    assert "gamedock.managed=true" not in args  # labels come from the spec
    assert args[args.index("--label") + 1] == "a=b"


def test_mount_handles_odd_paths_without_a_shell():
    spec = ContainerSpec("gamedock-x", "img:1", (), (VolumeMapping("/data/my games, ok/x:y", "/c"),), {})
    args = DockerCliBackend.build_create_args(spec)
    mount = args[args.index("--mount") + 1]
    assert mount == 'type=bind,"source=/data/my games, ok/x:y",target=/c'  # CSV quoting protects the comma
    win = ContainerSpec("gamedock-x", "img:1", (), (VolumeMapping("C:\\games\\x", "/c"),), {})
    assert "source=C:\\games\\x" in DockerCliBackend.build_create_args(win)[-3]


def test_everything_is_an_argument_list_never_a_shell():
    evil = "x; rm -rf / #"
    r = Recorder([(0, STOPPED, ""), (0, "", ""), (0, STOPPED, ""), (0, "", "")])
    b = DockerCliBackend(runner=r)
    b.start(evil)
    b.stop(evil)
    for argv, kw in r.calls:
        assert isinstance(argv, list) and "shell" not in kw
    assert r.argvs()[1] == ["docker", "start", evil]


@pytest.mark.parametrize("name", ["PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "LD_PRELOAD", "HTTPS_PROXY",
                                  "XDG_CONFIG_HOME", "SSH_AUTH_SOCK"])
def test_reserved_env_names_are_refused(name):
    assert is_reserved_env_name(name)
    spec = ContainerSpec("gamedock-x", "img:1", (), (), {name: "x"})
    with pytest.raises(BackendError, match="reserved"):
        DockerCliBackend.build_create_args(spec)


def test_normal_env_names_allowed():
    for n in ("SERVER_NAME", "PUID", "PGID", "TZ", "PALWORLD_ADMIN_PASSWORD", "HOMEPAGE"):
        assert not is_reserved_env_name(n)


@pytest.mark.parametrize("image", ["--privileged", "a b", "img;rm", "-v/:/host", "", "a\nb"])
def test_bad_image_names_are_refused(image):
    spec = ContainerSpec("gamedock-x", image, (), (), {})
    with pytest.raises(BackendError, match="not a valid image"):
        DockerCliBackend.build_create_args(spec)
    with pytest.raises(BackendError):
        DockerCliBackend(runner=Recorder([])).pull_image(image)


def test_good_image_names():
    for image in ("busybox", "lloesche/valheim-server:latest", "registry.example:5000/a/b:1.2",
                  "ghcr.io/x/y@sha256:" + "a" * 64):
        DockerCliBackend.build_create_args(ContainerSpec("gamedock-x", image, (), (), {}))


def test_create_passes_env_via_child_environment_and_waits_for_pulls():
    r = Recorder([MISSING, (0, "id", "")])
    DockerCliBackend(runner=r).create(SPEC)
    argv, kw = r.calls[1]
    assert kw["env"]["SECRET"] == "hunter2" and "hunter2" not in " ".join(argv)
    assert kw["timeout"] >= 3600  # 'docker create' downloads a missing image; 90 s would cut it off


def test_create_is_a_noop_for_our_existing_container():
    r = Recorder([(0, STOPPED, "")])
    DockerCliBackend(runner=r).create(SPEC)
    assert len(r.calls) == 1


def test_queries_use_container_inspect_and_short_timeouts():
    r = Recorder([(0, OURS, "")])
    DockerCliBackend(runner=r).get_state("x")
    assert r.calls[0][0][:3] == ["docker", "container", "inspect"]  # plain 'inspect' would also match images
    assert r.calls[0][1]["timeout"] <= 30


def test_stop_uses_configured_grace_period():
    r = Recorder([(0, OURS, ""), (0, "", "")])
    DockerCliBackend(runner=r, stop_timeout=120).stop("x")
    argv, kw = r.calls[1]
    assert argv == ["docker", "stop", "-t", "120", "x"] and kw["timeout"] >= 150


def test_remove_never_force_kills_a_running_container():
    r = Recorder([(0, OURS, ""), (0, "", "")])
    DockerCliBackend(runner=r).remove("x")
    assert r.argvs()[1] == ["docker", "rm", "x"]
    r = Recorder([(0, "paused|0|false|true\n", ""), (0, "", "")])
    DockerCliBackend(runner=r).remove("x")
    assert r.argvs()[1] == ["docker", "rm", "-f", "x"]
    r = Recorder([MISSING])
    DockerCliBackend(runner=r).remove("x")  # nothing to do
    assert len(r.calls) == 1


def test_start_unpauses_a_paused_container():
    r = Recorder([(0, "paused|0|false|true\n", ""), (0, "", "")])
    DockerCliBackend(runner=r).start("x")
    assert r.argvs()[1] == ["docker", "unpause", "x"]


# ------------------------------------------------------------------ containers GameDock did not create
def test_foreign_container_is_reported_not_touched():
    b = lambda r: DockerCliBackend(runner=r)
    st = b(Recorder([FOREIGN])).get_state("gamedock-x")
    assert st.status == Status.ERROR and "not created by GameDock" in st.detail
    for action in (lambda be: be.create(SPEC), lambda be: be.start("x"), lambda be: be.stop("x"),
                   lambda be: be.remove("x")):
        r = Recorder([FOREIGN])
        with pytest.raises(ForeignContainerError, match="will not touch"):
            action(b(r))
        assert len(r.calls) == 1  # only the look-up; no start/stop/rm/create was issued


def test_start_missing_container_is_friendly():
    with pytest.raises(BackendError, match="does not exist"):
        DockerCliBackend(runner=Recorder([MISSING])).start("x")


# ------------------------------------------------------------------ state
def test_state_mapping_and_details():
    r = Recorder([(0, "running|0|false|true\n", ""), (0, "exited|0|false|true\n", ""), MISSING,
                  (0, "exited|137|false|true\n", ""), (0, "exited|137|true|true\n", ""),
                  (0, "restarting|1|false|true\n", ""), (0, "dead|0|false|true\n", "")])
    b = DockerCliBackend(runner=r)
    assert b.get_state("x").status == Status.RUNNING
    s = b.get_state("x")
    assert s.status == Status.STOPPED and s.detail == "exited"
    assert b.get_state("x").status == Status.NOT_CREATED
    crashed = b.get_state("x")
    assert crashed.status == Status.STOPPED and "exit code 137" in crashed.detail
    assert "ran out of memory" in b.get_state("x").detail
    loop = b.get_state("x")
    assert loop.status == Status.RUNNING and "keeps stopping" in loop.detail
    assert b.get_state("x").status == Status.ERROR


# ------------------------------------------------------------------ errors in plain language
def test_missing_docker_binary():
    def runner(*a, **k):
        raise FileNotFoundError

    with pytest.raises(DockerUnavailableError, match="not found") as e:
        DockerCliBackend(runner=runner).start("x")
    assert e.value.kind == "not_installed" and "Install Docker" in str(e.value)


def test_daemon_down_and_permission_messages():
    down = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"
    perm = ("permission denied while trying to connect to the Docker daemon socket at "
            "unix:///var/run/docker.sock: Get ...: dial unix /var/run/docker.sock: connect: permission denied")
    for text, kind, hint in ((down, "daemon_down", "systemctl start docker"), (perm, "permission", "usermod -aG docker")):
        with pytest.raises(DockerUnavailableError) as e:
            DockerCliBackend(runner=Recorder([(1, "", text)])).get_state("x")
        assert e.value.kind == kind and hint in str(e.value)
        assert "unix:///" not in str(e.value)  # no raw jargon


@pytest.mark.parametrize("stderr,expect", [
    ("Error response from daemon: driver failed programming external connectivity: Bind for 0.0.0.0:8211 failed: "
     "port is already allocated", "Host port 8211 is already in use"),
    ("write /var/lib/docker/x: no space left on device", "disk is full"),
    ("pull access denied for foo, repository does not exist or may require 'docker login'", "could not find that image"),
    ("toomanyrequests: You have reached your pull rate limit", "limiting downloads"),
    ("no matching manifest for linux/arm64/v8 in the manifest list entries", "CPU type"),
    ("Conflict. The container name \"/gamedock-x\" is already in use by container abc", "already exists"),
    ("cannot remove a running container", "Stop it first"),
    ("something odd", "docker start failed: something odd"),
])
def test_friendly_errors(stderr, expect):
    assert expect in str(docker_errors.friendly("start", stderr))


def test_timeout_message():
    def runner(*a, **k):
        raise subprocess.TimeoutExpired("docker", 1)

    with pytest.raises(BackendError, match="did not finish in time"):
        DockerCliBackend(runner=runner).start("x")


# ------------------------------------------------------------------ images
def test_pull_and_image_ids():
    r = Recorder([(0, "pulled", ""), (0, "sha256:abc\n", ""), (1, "", "No such image"), (0, "sha256:def\n", ""),
                  (1, "", "No such object")])
    b = DockerCliBackend(runner=r)
    b.pull_image("img:1")
    assert r.calls[0][0] == ["docker", "pull", "--", "img:1"] and r.calls[0][1]["timeout"] >= 600
    assert b.get_image_id("img:1") == "sha256:abc"
    assert b.get_image_id("img:2") is None
    assert b.get_container_image_id("c") == "sha256:def"
    assert b.get_container_image_id("gone") is None


# ------------------------------------------------------------------ diagnose
def test_diagnose_ok():
    r = Recorder([(0, "Docker version 27.1.1, build abc\n", ""), (0, "27.1.1\n", "")])
    d = DockerCliBackend(runner=r).diagnose()
    assert d.reachable and d.kind == "ok" and d.server_version == "27.1.1" and "27.1.1" in d.client_version


def test_diagnose_daemon_down_and_permission():
    down = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"
    d = DockerCliBackend(runner=Recorder([(0, "Docker version 27\n", ""), (1, "", down)])).diagnose()
    assert not d.reachable and d.installed and d.kind == "daemon_down" and "systemctl start docker" in d.message
    perm = "permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock"
    d = DockerCliBackend(runner=Recorder([(0, "Docker version 27\n", ""), (1, "", perm)])).diagnose()
    assert d.kind == "permission" and "'docker' group" in d.message


def test_diagnose_not_installed():
    def runner(*a, **k):
        raise FileNotFoundError

    d = DockerCliBackend(docker_bin="definitely-not-docker-xyz").diagnose()
    assert not d.installed and d.kind == "not_installed" and "Install Docker" in d.message
    d = DockerCliBackend(runner=runner).diagnose()
    assert d.kind == "not_installed"
