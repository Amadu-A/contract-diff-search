# tests/functional/test_deployment.py
"""Проверки однокомандного запуска с изолированным состоянием общего RabbitMQ.

Вместо Docker используется функция Bash, читающая временный Python-файл. Реальные контейнеры,
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
    tool = sys.argv[1]
    if tool != "--mock-docker":
        code = (
            int(state.get("lock_busy", False))
            if tool == "--mock-flock"
            else state.get("quality_exit", 0)
        )
        state.setdefault("tools", []).append(tool)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        if tool == "--mock-uv":
            print("QUALITY_DETAIL_FIRST\n" + "quality progress\n" * 12)
        return code
    password = sys.stdin.read() if "-i" in args else ""
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
            output = "BUILD_DETAIL_FIRST\nImage test-app Built"
        elif "run" in args:
            if "app.provisioning" in args:
                assert state["built"], "Образ должен быть собран перед чтением Settings"
                code = 1 if state.get("bootstrap_error") else 0
                output = "\n".join(state["config"]) if code == 0 else ""
            else:
                assert "tests" in args
                output = "TEST_DETAIL_FIRST\n" + "test progress\n" * 15 + "tests passed"
                code = state.get("container_tests_exit", 0)
        elif "up" in args:
            state["up_count"] += 1
        elif "ps" not in args:
            code = 1
    elif args and args[0] == "exec":
        assert args[args.index("timeout") - 1] == CONTAINER
        assert "--user" in args and args[args.index("--user") + 1] == "rabbitmq"
        if "rabbitmqctl" in args:
            ctl = args[args.index("rabbitmqctl") + 1 :]
            while ctl and ctl[0].startswith("--"):
                count = 2 if ctl[0] == "--timeout" else 1
                ctl = ctl[count:]
            command, values = ctl[0], ctl[1:]
        else:
            script = args[args.index("-c") + 1]
            assert "cat >" in script and "/dev/shm/contract-diff-password" in script
            assert "trap" in script and "timeout" in script
            expression, user = args[-2:]
            command = (
                "add_user"
                if "rabbit_auth_backend_internal:add_user" in expression
                else "authenticate_user"
            )
            assert "file:read_file(_2)" in expression
            values = [user]
        event["operation"] = command
        if command == state.get("hang_command"):
            import time

            state_path.write_text(json.dumps(state), encoding="utf-8")
            time.sleep(60)
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
    """Подключает подмену через BASH_ENV, включая временный каталог с запретом исполнения."""
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    bash = str(git_bash) if git_bash.exists() else shutil.which("bash")
    if bash is None:
        pytest.skip("Для проверки scripts/up.sh требуется Bash")
    mock = tmp_path / "mock_docker.py"
    mock.write_text(
        "import hashlib, json, os, sys\nfrom pathlib import Path\n"
        + f"USER={USER!r}\nVHOST={VHOST!r}\nCONTAINER={CONTAINER!r}\n"
        + inspect.getsource(mock_docker_main)
        + "\nraise SystemExit(mock_docker_main())\n",
        encoding="utf-8",
        newline="\n",
    )
    mock.chmod(0o600)
    hook = tmp_path / "mock_env.sh"
    launcher = shlex.quote(Path(sys.executable).as_posix()) + " " + shlex.quote(mock.as_posix())
    hook.write_text(
        "\n".join(
            f'{name}() {{ {launcher} --mock-{name} "$@"; }}'
            for name in ("docker", "flock", "uv", "git")
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    hook.chmod(0o600)
    repository = tmp_path / "repository"
    (repository / "scripts").mkdir(parents=True)
    for name in ("up.sh", "test.sh", "check-quality.sh"):
        shutil.copyfile(ROOT / "scripts" / name, repository / "scripts" / name)
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
        "config": [CONTAINER, USER, VHOST, PASSWORD, "30", "http://192.168.55.3:8082"],
        "repository": str(repository),
        "users": {},
        "vhosts": {"/": {}},
        "fail_commands": [],
        "mutations": [],
        "journal": [],
        "up_count": 0,
    }

    def run(trace=False, script="up.sh", arguments=()):
        """Выполняет Bash без использования пользовательского Docker и возвращает новый state."""
        state_path.write_text(json.dumps(state), encoding="utf-8")
        journal_start = len(state["journal"])
        environment = dict(os.environ)
        environment["BASH_ENV"] = hook.as_posix()
        environment["CONTRACT_DIFF_MOCK_STATE"] = str(state_path)
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["MSYS2_ARG_CONV_EXCL"] = "*"
        completed = subprocess.run(
            [
                bash,
                *(["-x"] if trace else []),
                (repository / "scripts" / script).as_posix(),
                *arguments,
            ],
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
        events = state["journal"][journal_start:]
        if script != "check-quality.sh":
            assert state.get("lock_busy") or events, (
                "Не сработала тестовая подмена Docker; проверка сценария недействительна"
            )
        if script == "up.sh" and not state.get("lock_busy"):
            assert events[0]["args"] == ["info"]
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
    return [event["operation"] for event in state["journal"] if "operation" in event]


def test_first_start_and_repeat_are_idempotent(deployment):
    """Создаёт недостающее один раз и сохраняет спецсимволы пароля без утечки в argv/лог."""
    state, run = deployment
    first = run()
    assert first.returncode == 0, first.stderr
    assert "BUILD_DETAIL_FIRST" in first.stdout
    assert "Создание пользователя RabbitMQ" in first.stdout
    assert "Проверка пароля RabbitMQ" in first.stdout
    assert "http://192.168.55.3:8082" in first.stdout
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
    assert command_names(state)[-1] == "authenticate_user"
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
    assert command_names(state)[-1] == command


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
    assert command_names(state)[-1] == "list_users"


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
    assert command_names(state)[-1] == "list_user_permissions"


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


def test_concurrent_deploy_is_rejected_before_docker(deployment):
    """Не запускает второй deploy, когда другой процесс удерживает блокировку."""
    state, run = deployment
    state["lock_busy"] = True
    completed = run()
    assert completed.returncode != 0
    assert state["journal"] == []
    assert "Уже выполняется другой" in completed.stderr


def test_deploy_log_is_explicit_and_stages_stay_visible(deployment):
    """Только --log скрывает подробности сборки; текущий шаг всё равно виден."""
    state, run = deployment
    completed = run(arguments=("--log",))
    assert completed.returncode == 0, completed.stderr
    assert "BUILD_DETAIL_FIRST" not in completed.stdout
    assert "Создание пользователя RabbitMQ" in completed.stdout
    log = Path(state["repository"]) / ".cache/deploy.log"
    assert "BUILD_DETAIL_FIRST" in log.read_text(encoding="utf-8")
    assert PASSWORD not in log.read_text(encoding="utf-8")


def test_rejected_deploy_keeps_log_of_running_process(deployment):
    """Второй запуск с --log не стирает журнал процесса, удерживающего блокировку."""
    state, run = deployment
    state["lock_busy"] = True
    log = Path(state["repository"]) / ".cache/deploy.log"
    log.parent.mkdir()
    log.write_text("running deploy log", encoding="utf-8")
    completed = run(arguments=("--log",))
    assert completed.returncode != 0
    assert log.read_text(encoding="utf-8") == "running deploy log"


@pytest.mark.parametrize("script, marker", [("test.sh", "TEST"), ("check-quality.sh", "QUALITY")])
def test_server_checks_show_details_unless_log_requested(deployment, script, marker):
    """По умолчанию сборка и тесты видны, а --log сохраняет подробности в отдельный файл."""
    state, run = deployment
    completed = run(script=script)
    assert completed.returncode == 0, completed.stderr
    assert f"{marker}_DETAIL_FIRST" in completed.stdout
    completed = run(script=script, arguments=("--log",))
    assert completed.returncode == 0, completed.stderr
    assert "Подробный вывод:" in completed.stdout
    assert f"{marker}_DETAIL_FIRST" not in completed.stdout
    filename = "tests-linux.log" if script == "test.sh" else "quality-linux.log"
    assert f"{marker}_DETAIL_FIRST" in (Path(state["repository"]) / ".cache" / filename).read_text()


@pytest.mark.parametrize("arguments", [(), ("--log",)])
def test_test_script_keeps_failure_code_and_output(deployment, arguments):
    """Ошибка тестового контейнера видна и сохраняет код выхода в обоих режимах."""
    state, run = deployment
    state["container_tests_exit"] = 7
    completed = run(script="test.sh", arguments=arguments)
    assert completed.returncode == 7
    assert "TEST_DETAIL_FIRST" in completed.stdout


@pytest.mark.skipif(os.name == "nt", reason="Проверка POSIX-группы процессов и GNU timeout")
def test_hung_docker_client_is_bounded(deployment):
    """Даже зависание клиента до завершения exec не оставляет up.sh ждать бесконечно."""
    import time

    state, run = deployment
    configured(state)
    state["config"][4] = "1"
    state["hang_command"] = "authenticate_user"
    started = time.monotonic()
    completed = run()
    assert completed.returncode == 124
    assert time.monotonic() - started < 25
    assert "Превышено время ожидания" in completed.stderr
    assert state["up_count"] == 0
