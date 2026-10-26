# tests/functional/test_deployment.py
"""Проверки однокомандного запуска с изолированным состоянием общего RabbitMQ.

Вместо Docker используется временная исполняемая команда. Реальные контейнеры,
пользователи и секреты оператора не читаются и не изменяются.
"""

import hashlib
import inspect
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "S p'ec\"!$#&\\()=end  "
USER = "contract-diff"
VHOST = "contract-diff"
CONTAINER = "shared-rabbitmq-1"


def mock_docker_main() -> int:
    """Эмулирует только ожидаемые Docker/CLI-команды, сохраняя безопасный журнал."""
    state_path = Path(os.environ["CONTRACT_DIFF_MOCK_STATE"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    args = sys.argv[2:]
    password = sys.stdin.read().removesuffix("\n") if "-i" in args else ""
    event = {"args": args}
    if "-i" in args:
        event["stdin_sha256"] = hashlib.sha256(password.encode()).hexdigest()
        event["stdin_length"] = len(password)
    state["journal"].append(event)
    output, code = "", 0
    if args == ["info"]:
        code = 0 if state["docker"] else 1
    elif args[:2] == ["network", "inspect"]:
        code = 0 if state["network"] else 1
        if "--format" in args and code == 0:
            output = "\n".join(state["members"])
    elif args and args[0] == "inspect":
        code = 0 if args[-1] == CONTAINER else 1
        output = "true" if state["running"] and code == 0 else "false"
    elif args and args[0] == "compose":
        if "config" in args:
            code = 0 if state["compose_config"] else 1
        elif "build" in args:
            state["built"] = True
        elif "run" in args:
            assert state["built"], "Образ должен быть собран перед чтением Settings"
            code = 1 if state.get("bootstrap_error") else 0
            output = "\n".join(state["config"]) if code == 0 else ""
        elif "up" in args:
            state["up_count"] += 1
        elif "ps" not in args:
            code = 1
    elif args and args[0] == "exec":
        position = 2 if args[1] == "-i" else 1
        assert args[position] == CONTAINER, "Нельзя обращаться к чужому контейнеру"
        ctl = args[args.index("rabbitmqctl") + 1 :]
        while ctl and ctl[0].startswith("--"):
            count = 2 if ctl[0] == "--timeout" else 1
            ctl = ctl[count:]
        command, values = ctl[0], ctl[1:]
        if command in state["fail_commands"]:
            code = 1
        elif command == "list_users":
            assert values == ["--no-table-headers"]
            output = "\n".join(f"{name}\t{user['tags']}" for name, user in state["users"].items())
        elif command == "authenticate_user":
            assert values == [USER]
            code = 0 if state["users"].get(USER, {}).get("password") == password else 1
        elif command == "add_user":
            assert values == [USER]
            if USER in state["users"]:
                code = 1
            else:
                state["users"][USER] = {"password": password, "tags": "[]"}
                state["mutations"].append(command)
        elif command == "list_user_permissions":
            assert values == [USER, "--no-table-headers"]
            output = "\n".join(
                "\t".join([name, *permissions[USER]])
                for name, permissions in state["vhosts"].items()
                if USER in permissions
            )
        elif command == "list_vhosts":
            assert values == ["name", "--no-table-headers"]
            output = "\n".join(state["vhosts"])
        elif command == "add_vhost":
            assert values == [VHOST]
            if VHOST in state["vhosts"]:
                code = 1
            else:
                state["vhosts"][VHOST] = {}
                state["mutations"].append(command)
        elif command == "list_permissions":
            assert values == ["-p", VHOST, "--no-table-headers"]
            if VHOST not in state["vhosts"]:
                code = 1
            else:
                output = "\n".join(
                    "\t".join([name, *permissions])
                    for name, permissions in state["vhosts"][VHOST].items()
                )
        elif command == "set_permissions":
            assert values == ["-p", VHOST, USER, ".*", ".*", ".*"]
            state["vhosts"][VHOST][USER] = [".*", ".*", ".*"]
            state["mutations"].append(command)
        else:
            code = 1
    else:
        code = 1
    state_path.write_text(json.dumps(state), encoding="utf-8")
    if output:
        sys.stdout.buffer.write((output + "\n").encode("utf-8"))
    if code:
        print("Тестовая команда Docker завершилась отказом", file=sys.stderr)
    return code


@pytest.fixture
def deployment(tmp_path):
    """Подключает временную mock-команду в PATH и предоставляет запуск настоящего up.sh."""
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    bash = str(git_bash) if git_bash.exists() else shutil.which("bash")
    if bash is None:
        pytest.skip("Для проверки scripts/up.sh требуется Bash")
    directory = tmp_path / "mock-bin"
    directory.mkdir()
    mock = tmp_path / "mock_docker.py"
    mock.write_text(
        "import hashlib, json, os, sys\nfrom pathlib import Path\n"
        + f"USER={USER!r}\nVHOST={VHOST!r}\nCONTAINER={CONTAINER!r}\n"
        + inspect.getsource(mock_docker_main)
        + "\nraise SystemExit(mock_docker_main())\n",
        encoding="utf-8",
        newline="\n",
    )
    launcher = directory / "docker"
    launcher.write_text(
        "#!/usr/bin/env bash\nexec "
        + shlex.quote(Path(sys.executable).as_posix())
        + " "
        + shlex.quote(mock.as_posix())
        + ' --mock-docker "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    launcher.chmod(0o700)
    repository = tmp_path / "repository"
    (repository / "scripts").mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/up.sh", repository / "scripts/up.sh")
    (repository / ".env.example").write_text("RABBITMQ_PASSWORD=\n", encoding="utf-8")
    (repository / ".env").write_text("RABBITMQ_PASSWORD=mock-only\n", encoding="utf-8")
    state_path = tmp_path / "state.json"
    state = {
        "docker": True,
        "network": True,
        "members": [CONTAINER, "shared-shared-vlm-1"],
        "running": True,
        "compose_config": True,
        "built": False,
        "config": [CONTAINER, USER, VHOST, PASSWORD],
        "users": {},
        "vhosts": {"/": {}},
        "fail_commands": [],
        "mutations": [],
        "journal": [],
        "up_count": 0,
    }

    def run(trace=False):
        """Выполняет Bash без использования пользовательского Docker и возвращает новый state."""
        state_path.write_text(json.dumps(state), encoding="utf-8")
        environment = dict(os.environ)
        environment["PATH"] = str(directory) + os.pathsep + environment["PATH"]
        environment["CONTRACT_DIFF_MOCK_STATE"] = str(state_path)
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["MSYS2_ARG_CONV_EXCL"] = "*"
        completed = subprocess.run(
            [bash, *(["-x"] if trace else []), (repository / "scripts/up.sh").as_posix()],
            cwd=repository,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        state.clear()
        state.update(json.loads(state_path.read_text(encoding="utf-8")))
        assert PASSWORD.rstrip() not in completed.stdout + completed.stderr
        assert all(
            PASSWORD.rstrip() not in arg for event in state["journal"] for arg in event["args"]
        )
        return completed

    return state, run


def configured(state):
    """Заполняет корректного выделенного пользователя и его единственный vhost."""
    state["users"][USER] = {"password": PASSWORD, "tags": "[]"}
    state["vhosts"][VHOST] = {USER: [".*", ".*", ".*"]}


def command_names(state):
    """Извлекает выполненные RabbitMQ-команды из журнала без чтения секретов."""
    return [
        args[args.index("rabbitmqctl") + 4]
        for event in state["journal"]
        if "rabbitmqctl" in (args := event["args"])
    ]


def test_first_start_and_repeat_are_idempotent(deployment):
    """Создаёт недостающее один раз и сохраняет спецсимволы пароля без утечки в argv/лог."""
    state, run = deployment
    first = run()
    assert first.returncode == 0, first.stderr
    assert state["users"][USER] == {"password": PASSWORD, "tags": "[]"}
    assert state["vhosts"][VHOST][USER] == [".*", ".*", ".*"]
    assert state["mutations"] == ["add_user", "add_vhost", "set_permissions"]
    commands = command_names(state)
    assert commands.index("authenticate_user") < commands.index("add_vhost")
    password_events = [event for event in state["journal"] if "stdin_sha256" in event]
    assert password_events
    assert all(
        event["stdin_sha256"] == hashlib.sha256(PASSWORD.encode()).hexdigest()
        for event in password_events
    )
    assert all(event["stdin_length"] == len(PASSWORD) for event in password_events)
    second = run(trace=True)
    assert second.returncode == 0, second.stderr
    assert state["mutations"] == ["add_user", "add_vhost", "set_permissions"]
    assert state["up_count"] == 2


def test_existing_permissions_are_repaired_only_in_own_vhost(deployment):
    """Исправляет права приложения и сохраняет чужие vhost/учётные записи."""
    state, run = deployment
    configured(state)
    state["users"]["other-project"] = {"password": "another", "tags": "[]"}
    state["vhosts"]["other-project"] = {"other-project": [".*", ".*", ".*"]}
    state["vhosts"][VHOST][USER] = ["^nothing$", "^nothing$", "^nothing$"]
    completed = run()
    assert completed.returncode == 0, completed.stderr
    assert state["mutations"] == ["set_permissions"]
    assert state["vhosts"][VHOST][USER] == [".*", ".*", ".*"]
    assert state["vhosts"]["other-project"] == {"other-project": [".*", ".*", ".*"]}
    assert state["users"]["other-project"]["password"] == "another"


def test_wrong_password_does_not_change_user_or_start_service(deployment):
    """Не меняет пароль существующего пользователя и не запускает приложение при отказе входа."""
    state, run = deployment
    configured(state)
    state["users"][USER]["password"] = "existing-different-password"
    completed = run()
    assert completed.returncode != 0
    assert state["mutations"] == []
    assert state["up_count"] == 0
    assert state["users"][USER]["password"] == "existing-different-password"
    assert "change_password" not in command_names(state)


@pytest.mark.parametrize(
    "command",
    ["list_users", "authenticate_user", "list_user_permissions", "list_vhosts", "list_permissions"],
)
def test_rabbitmq_read_failure_is_not_treated_as_missing_record(deployment, command):
    """Отказ CLI останавливает запуск, а не провоцирует создание пользователя/vhost заново."""
    state, run = deployment
    configured(state)
    state["fail_commands"] = [command]
    completed = run()
    assert completed.returncode != 0
    assert state["mutations"] == []
    assert state["up_count"] == 0


@pytest.mark.parametrize("tags", ["[administrator]", "[management]", "[monitoring, policymaker]"])
def test_privileged_existing_user_is_rejected(deployment, tags):
    """Не присваивает проекту административного пользователя и не очищает его tags."""
    state, run = deployment
    configured(state)
    state["users"][USER]["tags"] = tags
    completed = run()
    assert completed.returncode != 0
    assert state["mutations"] == []
    assert state["up_count"] == 0
    assert state["users"][USER]["tags"] == tags


def test_user_with_permissions_in_foreign_vhost_is_rejected(deployment):
    """Отклоняет общую учётную запись, сохраняя все права других приложений."""
    state, run = deployment
    configured(state)
    state["vhosts"]["other-project"] = {USER: [".*", ".*", ".*"]}
    completed = run()
    assert completed.returncode != 0
    assert state["mutations"] == []
    assert state["up_count"] == 0
    assert state["vhosts"]["other-project"][USER] == [".*", ".*", ".*"]


@pytest.mark.parametrize(
    "failure", ["docker", "network", "running", "members", "compose_config", "bootstrap_error"]
)
def test_missing_dependencies_stop_before_broker_mutation(deployment, failure):
    """Отсутствие Docker/сети/брокера или отказ конфигурации не меняют RabbitMQ."""
    state, run = deployment
    if failure == "members":
        state["members"] = ["shared-shared-vlm-1"]
    else:
        state[failure] = failure == "bootstrap_error"
    completed = run()
    assert completed.returncode != 0
    assert state["mutations"] == []
    assert state["up_count"] == 0
