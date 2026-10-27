# tests/integration/test_worker_runtime.py
"""Реальный worker и адресный healthcheck в изолированной сети одноразового RabbitMQ."""

import json
import shutil
import subprocess
import time
from uuid import uuid4

import pytest

from app.settings import ROOT

pytestmark = pytest.mark.external


def test_worker_becomes_healthy_and_processes_background_comparison(tmp_path):
    """Проверяет pong и настоящий PDF-use-case без доступа к общему брокеру или GPU."""
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Нужен Docker CLI")

    def call(*args, timeout=25):
        """Ограничивает ожидание Docker; тестовые пароли не относятся к секретам окружения."""
        return subprocess.run(
            [docker, *args], capture_output=True, text=True, encoding="utf-8", timeout=timeout
        )

    for image in ("contract-diff-search-tests", "rabbitmq:4.1-management"):
        if call("image", "inspect", image).returncode:
            pytest.skip(f"Сначала соберите/загрузите тестовый образ {image}")
    name = "contract-diff-smoke-" + uuid4().hex[:10]
    broker, worker = name + "-rabbit", name + "-worker"
    environment = tmp_path / "test.env"
    environment.write_text(
        "RABBITMQ_HOST=127.0.0.1\nRABBITMQ_USER=guest\nRABBITMQ_PASSWORD=guest\nRABBITMQ_VHOST=/\nNATIVE_THREADS=1\n",
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    producer = tmp_path / "producer.py"
    producer.write_text(
        '"""Создаёт и ожидает синтетическое фоновое сравнение в тестовом контейнере."""\n'
        "import io, json, time\n"
        "from pathlib import Path\n"
        "from app.settings import Settings\nfrom app.bootstrap import build_service\n"
        "from tests.factories import pdf\n"
        "service=build_service(Settings())\n"
        "a=pdf(Path('/tmp/old.pdf'), ['2.2. Advance payment 70%.']).read_bytes()\n"
        "b=pdf(Path('/tmp/new.pdf'), ['2.2. Advance payment 50%.']).read_bytes()\n"
        "job=service.submit('synthetic-capability','old.pdf',io.BytesIO(a),'new.pdf',io.BytesIO(b),False)\n"
        "deadline=time.monotonic()+60\n"
        "while time.monotonic()<deadline:\n"
        " current=service.repository.get(job.id)\n"
        " if current.status in {'completed','completed_with_warnings','failed'}: break\n"
        " time.sleep(.2)\n"
        "assert current.status in {'completed','completed_with_warnings'}, current.status\n"
        "result=service.result(job.id,'synthetic-capability')\n"
        "assert any(d['old_text']=='70%' and d['new_text']=='50%' for d in result['differences'])\n"
        "print(json.dumps({'status':current.status,'differences':len(result['differences'])}))\n",
        encoding="utf-8",
    )
    try:
        started = call(
            "run",
            "-d",
            "--name",
            broker,
            "--hostname",
            broker,
            "--user",
            "999:999",
            "--network",
            "none",
            "--tmpfs",
            "/var/lib/rabbitmq:rw,uid=999,gid=999,mode=0700,size=128m",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "--env",
            "RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS=+S 1:1",
            "--env",
            "RABBITMQ_CTL_ERL_ARGS=+S 1:1 +A 1",
            "rabbitmq:4.1-management",
        )
        assert started.returncode == 0, started.stderr
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = call(
                "exec",
                broker,
                "timeout",
                "5s",
                "rabbitmq-diagnostics",
                "-q",
                "check_running",
                timeout=8,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            state = call(
                "inspect",
                broker,
                "--format",
                "{{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}}",
            )
            logs = call("logs", "--tail", "15", broker)
            pytest.fail(
                "Одноразовый брокер не запустился: "
                + state.stdout
                + logs.stdout[-1500:]
                + logs.stderr[-1500:]
            )
        mounts = []
        for source, target in (
            (ROOT / "app", "/srv/app/app"),
            (ROOT / "tests", "/srv/app/tests"),
            (producer, "/srv/app/producer.py"),
        ):
            mounts += ["--mount", f"type=bind,source={source},target={target},readonly"]
        started = call(
            "run",
            "--rm",
            "-d",
            "--name",
            worker,
            "--network",
            "container:" + broker,
            "--memory",
            "2g",
            "--cpus",
            "2",
            "--pids-limit",
            "256",
            "--tmpfs",
            "/tmp:rw,nosuid,size=512m",
            "--env-file",
            str(environment),
            "--mount",
            f"type=bind,source={data},target=/srv/app/.data",
            *mounts,
            "--health-cmd",
            "python -m app.worker_health",
            "--health-interval",
            "5s",
            "--health-timeout",
            "10s",
            "--health-start-period",
            "15s",
            "contract-diff-search-tests",
            "python",
            "-m",
            "app.worker",
        )
        assert started.returncode == 0, started.stderr
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            state = call("inspect", worker, "--format", "{{.State.Health.Status}}")
            if state.stdout.strip() == "healthy":
                break
            time.sleep(0.5)
        else:
            pytest.fail("Worker не стал healthy в изолированном runtime")
        produced = call("exec", worker, "python", "/srv/app/producer.py", timeout=90)
        assert produced.returncode == 0, produced.stderr[-2000:]
        report = json.loads(produced.stdout.strip().splitlines()[-1])
        assert report["differences"] >= 1
        assert call("exec", worker, "python", "-m", "app.worker_health").returncode == 0
        logs = call("logs", worker)
        assert "pthread_create failed" not in logs.stderr + logs.stdout
        events = [
            json.loads(line)
            for line in (logs.stdout + "\n" + logs.stderr).splitlines()
            if line.startswith("{")
        ]
        assert any(
            event.get("operation") == "comparison_process" and event.get("status") == "success"
            for event in events
        )
    finally:
        call("rm", "-f", worker, broker)
