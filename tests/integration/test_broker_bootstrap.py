# tests/integration/test_broker_bootstrap.py
"""Настоящий RabbitMQ 4.1 в одноразовом контейнере: пароль, повтор, отказ и уборка.

Запускается отдельно с -m external. Общий RabbitMQ и пользовательские .env
не читаются; тестовый контейнер не публикует порты и не подключён к сети.
"""

import inspect
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from app.settings import ROOT

pytestmark = pytest.mark.external
PASSWORD = "synthetic-$#'quote\"  "


def docker_bridge():
    """Подменяет только Compose/проверку сети, передавая CLI-запросы настоящему брокеру."""
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    args = sys.argv[2:]
    output = ""
    if args == ["info"]:
        pass
    elif args[:2] == ["network", "inspect"]:
        output = config["container"] if "--format" in args else ""
    elif args[0] == "compose":
        if "run" in args:
            output = "\n".join(
                [
                    config["container"],
                    "contract-diff",
                    "contract-diff",
                    config["password"],
                    config.get("timeout", "10"),
                    "http://192.168.55.3:8082",
                ]
            )
        elif "up" in args:
            output = "APP_UP"
    elif args[0] in {"exec", "inspect"}:
        data = sys.stdin.buffer.read() if "-i" in args else None
        if "-i" in args and config.get("stall"):
            args[-2] = "timer:sleep(60000)."
        return subprocess.run(
            [config["docker"], *args], input=data, check=False, timeout=30
        ).returncode
    else:
        return 2
    if output:
        sys.stdout.buffer.write((output + "\n").encode())
    return 0


def test_real_rabbitmq_bootstrap_and_repeat(tmp_path):
    """Создаёт пользователя с точным паролем, повторяет запуск и отклоняет неверный пароль."""
    docker = shutil.which("docker")
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    bash = str(git_bash) if git_bash.exists() else shutil.which("bash")
    if not docker or not bash:
        pytest.skip("Нужны Docker CLI и Bash для отдельной интеграции RabbitMQ")

    def call(*args, timeout=20):
        """Выполняет Docker без секретов в командной строке и возвращает ограниченный вывод."""
        return subprocess.run(
            [docker, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )

    if call("info", "--format", "{{.ServerVersion}}").returncode:
        pytest.skip("Docker daemon недоступен")
    image = "rabbitmq:4.1-management"
    if call("image", "inspect", image).returncode:
        pytest.skip("Для интеграции сначала загрузите rabbitmq:4.1-management")
    container = "contract-diff-test-" + uuid4().hex[:12]
    start = call(
        "run",
        "--pull=never",
        "--rm",
        "-d",
        "--name",
        container,
        "--hostname",
        container,
        "--network",
        "none",
        "--cpus",
        "1",
        "--memory",
        "512m",
        "--env",
        "RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS=+S 1:1",
        image,
    )
    assert start.returncode == 0, start.stderr
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = call(
                "exec",
                "--user",
                "rabbitmq",
                container,
                "timeout",
                "5s",
                "rabbitmq-diagnostics",
                "-q",
                "check_running",
                timeout=8,
            )
            if not ready.returncode:
                break
            time.sleep(0.3)
        else:
            pytest.fail("Тестовый RabbitMQ не запустился за 60с")
        (tmp_path / "scripts").mkdir()
        shutil.copyfile(ROOT / "scripts/up.sh", tmp_path / "scripts/up.sh")
        (tmp_path / ".env.example").write_text("", encoding="utf-8")
        bridge = tmp_path / "bridge.py"
        bridge.write_text(
            "import json, sys, subprocess\nfrom pathlib import Path\n"
            + inspect.getsource(docker_bridge)
            + "\nraise SystemExit(docker_bridge())\n",
            encoding="utf-8",
        )
        config = tmp_path / "config.json"
        values = {"docker": docker, "container": container, "password": PASSWORD}
        config.write_text(json.dumps(values), encoding="utf-8")
        hook = tmp_path / "hook.sh"
        launcher = " ".join(
            shlex.quote(p.as_posix()) for p in (Path(sys.executable), bridge, config)
        )
        hook.write_text(
            f'docker() {{ {launcher} "$@"; }}\nflock() {{ return 0; }}\n',
            encoding="utf-8",
            newline="\n",
        )
        environment = dict(
            os.environ, BASH_ENV=hook.as_posix(), PYTHONIOENCODING="utf-8", MSYS2_ARG_CONV_EXCL="*"
        )

        def deploy():
            """Выполняет настоящий up.sh, включая создание и проверку пароля через RPC."""
            result = subprocess.run(
                [bash, (tmp_path / "scripts/up.sh").as_posix()],
                cwd=tmp_path,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=120,
                check=False,
            )
            assert PASSWORD not in result.stdout + result.stderr
            return result

        first = deploy()
        assert first.returncode == 0, first.stdout + first.stderr
        assert "Создание пользователя RabbitMQ" in first.stdout
        assert "APP_UP" in first.stdout
        second = deploy()
        assert second.returncode == 0, second.stdout + second.stderr
        assert "Создание пользователя RabbitMQ" not in second.stdout
        assert "Создание выделенного vhost" not in second.stdout
        values["password"] = "wrong-synthetic-password"
        config.write_text(json.dumps(values), encoding="utf-8")
        wrong = deploy()
        assert wrong.returncode != 0
        assert "APP_UP" not in wrong.stdout
        assert "wrong-synthetic-password" not in wrong.stdout + wrong.stderr
        values.update(password=PASSWORD, timeout="5", stall=True)
        config.write_text(json.dumps(values), encoding="utf-8")
        started = time.monotonic()
        hung = deploy()
        assert hung.returncode == 124, hung.stdout + hung.stderr
        assert time.monotonic() - started < 20
        assert "Превышено время ожидания" in hung.stderr
        leftovers = call(
            "exec", container, "find", "/dev/shm", "-name", "contract-diff-password.*", "-print"
        )
        assert leftovers.returncode == 0 and not leftovers.stdout.strip()
    finally:
        call("rm", "-f", container)


def test_compose_lan_publication_and_port_override(tmp_path):
    """Публикация LAN и порт приложения совпадают; process environment меняет оба конца."""
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Для проверки модели Compose нужен Docker CLI")
    for name in ("compose.yaml", ".env.example"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    environment = dict(os.environ, RABBITMQ_PASSWORD="synthetic")
    environment.pop("APP_BIND_IP", None)
    environment.pop("APP_PORT", None)
    for address, port in (("192.168.55.3", 8082), ("127.0.0.1", 18082)):
        if port != 8082:
            environment.update(APP_BIND_IP=address, APP_PORT=str(port))
        result = subprocess.run(
            [docker, "compose", "--env-file", ".env.example", "config", "--format", "json"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, "Не удалось проверить Compose"
        api = json.loads(result.stdout)["services"]["api"]
        binding = api["ports"][0]
        assert binding["host_ip"] == address
        assert int(binding["published"]) == binding["target"] == port
        assert int(api["environment"]["APP_PORT"]) == port
        assert api["environment"]["APP_HOST"] == "0.0.0.0"
