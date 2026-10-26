# tests/architecture/test_boundaries.py
"""Автоматические границы Clean Architecture и обязательная русская документация."""

import ast

from app.settings import ROOT, Settings


def test_domain_and_application_import_boundaries():
    """Домен не зависит от адаптеров; application не импортирует HTTP, SQL и очередь."""
    forbidden = {
        "fastapi",
        "starlette",
        "sqlalchemy",
        "celery",
        "httpx",
        "pypdf",
        "pypdfium2",
        "pdfplumber",
        "lxml",
        "numpy",
        "scipy",
    }
    for layer in ("domain", "application"):
        for path in (ROOT / "app" / layer).glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = (
                    [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else [n.name for n in node.names]
                    if isinstance(node, ast.Import)
                    else []
                )
                assert all(
                    m.split(".")[0] not in forbidden
                    and not m.startswith("app.infrastructure")
                    and not m.startswith("app.web")
                    for m in modules
                ), str(path)


def test_sources_document_public_contracts_in_russian():
    """У каждого Python-модуля и функции есть документация; относительный путь указан первым."""
    for path in (ROOT / "app").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert source.splitlines()[0] == "# " + path.relative_to(ROOT).as_posix()
        tree = ast.parse(source)
        assert ast.get_docstring(tree), str(path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node)
                assert doc and any("А" <= ch <= "я" for ch in doc), (path, node.name)


def test_settings_catalog_and_logical_vlm_contract():
    """Baseline содержит все поддерживаемые поля, без физической модели и обязательного API-key."""
    from dotenv import dotenv_values

    names = {key.lower() for key in dotenv_values(ROOT / ".env.example")}
    assert names == set(Settings.model_fields)
    settings = Settings(_env_file=None)
    assert settings.shared_vlm_model == "shared-vlm"
    assert not any("api_key" in name or "gpu" in name or "model_id" in name for name in names)


def test_frontend_safety_and_modularity():
    """Шаблоны не выполняют inline-JS, пользовательские данные не попадают в innerHTML."""
    for path in (ROOT / "app/web/templates").rglob("*.html"):
        source = path.read_text(encoding="utf-8")
        assert "onclick=" not in source and "<style>" not in source
    for path in (ROOT / "app/web/static/js").rglob("*.js"):
        assert ".innerHTML" not in path.read_text(encoding="utf-8")
    style = (ROOT / "app/web/static/css/style.css").read_text(encoding="utf-8")
    assert style.count("@import") >= 6
