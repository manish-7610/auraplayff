"""
Regression tests: app.main:app imports cleanly and all Pydantic schemas
resolve their annotations without PydanticUndefinedAnnotation errors.

Root cause caught by these tests:
  Pydantic v2 with `from __future__ import annotations` turns ALL annotations
  into strings that are lazily evaluated.  model_validator(mode="after") return
  types written as quoted class names (e.g. -> "PlayerRegisterRequest") fail
  during model_rebuild() because the class name cannot be resolved from outside
  the module's namespace.  The correct pattern is `-> Self` (typing.Self).

These tests run in an isolated import namespace so they always reflect the
cold-import behaviour Render/uvicorn sees on startup.
"""
from __future__ import annotations

import importlib
import os
import sys

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_min_env() -> None:
    """Set the minimum env vars needed for app.config to load without error."""
    os.environ.setdefault("APP_ENV", "testing")
    os.environ.setdefault("APP_DEBUG", "false")
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-import-test-32ch!")
    os.environ.setdefault(
        "DATABASE_URL",
        "postgresql+asyncpg://placeholder:placeholder@localhost/placeholder",
    )
    os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-key-for-import-test-32chars!")
    os.environ.setdefault("SUPER_ADMIN_AGENT_ID", "TEST-ADMIN")
    os.environ.setdefault("SUPER_ADMIN_PASSWORD", "TestAdmin@123456")
    os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:3000")


# ---------------------------------------------------------------------------
# Test 1 — app.main imports and exposes an ASGI `app` object
# ---------------------------------------------------------------------------

def test_app_main_imports_and_exposes_app() -> None:
    """
    Regression: PydanticUndefinedAnnotation: name 'PlayerRegisterRequest' is not defined.

    Verifies that `from app.main import app` succeeds and that the resulting
    object is a callable ASGI application (FastAPI instance).
    """
    _set_min_env()
    import app.main as main_module  # noqa: PLC0415
    assert hasattr(main_module, "app"), "app.main must expose a module-level `app` variable"
    asgi_app = main_module.app
    assert callable(asgi_app), "app.main.app must be callable (ASGI application)"


# ---------------------------------------------------------------------------
# Test 2 — All Pydantic schemas rebuild without PydanticUndefinedAnnotation
# ---------------------------------------------------------------------------

_SCHEMA_MODULES = [
    "app.schemas.auth",
    "app.schemas.support",
    "app.schemas.tournament",
    "app.schemas.wallet",
    "app.schemas.withdrawal",
    "app.schemas.deposit",
    "app.schemas.result",
    "app.schemas.agent",
    "app.schemas.user",
    "app.schemas.audit",
    "app.schemas.notification",
    "app.schemas.screenshot",
    "app.schemas.scheduler",
    "app.schemas.settings",
]


@pytest.mark.parametrize("module_path", _SCHEMA_MODULES)
def test_schema_model_rebuild(module_path: str) -> None:
    """
    Each schema module must be importable and every BaseModel subclass within
    it must survive model_rebuild(raise_errors=True).

    This catches PydanticUndefinedAnnotation errors that only surface when
    Pydantic resolves forward references — e.g. quoted class names in
    model_validator(mode='after') return types that should use `Self` instead.
    """
    _set_min_env()
    from pydantic import BaseModel  # noqa: PLC0415

    mod = importlib.import_module(module_path)
    errors: list[str] = []

    for attr_name in dir(mod):
        obj = getattr(mod, attr_name)
        try:
            if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel:
                obj.model_rebuild(raise_errors=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{module_path}.{attr_name}: {type(exc).__name__}: {exc}")

    assert not errors, (
        "model_rebuild() failed for the following schemas — "
        "check for quoted self-referential return types that should use `Self`:\n"
        + "\n".join(errors)
    )


# ---------------------------------------------------------------------------
# Test 3 — Inline schemas in routers also rebuild cleanly
# ---------------------------------------------------------------------------

_ROUTER_MODULES_WITH_INLINE_SCHEMAS = [
    "app.routers.recovery",   # RecoveryRequestCreate / RecoveryRequestResponse
]


@pytest.mark.parametrize("module_path", _ROUTER_MODULES_WITH_INLINE_SCHEMAS)
def test_router_inline_schema_rebuild(module_path: str) -> None:
    """
    Some routers define Pydantic models inline rather than in app/schemas/.
    These must also survive model_rebuild().
    """
    _set_min_env()
    from pydantic import BaseModel  # noqa: PLC0415

    mod = importlib.import_module(module_path)
    errors: list[str] = []

    for attr_name in dir(mod):
        obj = getattr(mod, attr_name)
        try:
            if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel:
                obj.model_rebuild(raise_errors=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{module_path}.{attr_name}: {type(exc).__name__}: {exc}")

    assert not errors, (
        "model_rebuild() failed for inline router schemas:\n" + "\n".join(errors)
    )


# ---------------------------------------------------------------------------
# Test 4 — uvicorn can resolve `app.main:app` as a string reference
# ---------------------------------------------------------------------------

def test_uvicorn_app_string_reference() -> None:
    """
    Mimics what uvicorn does internally when given `app.main:app` as the
    application string.  Verifies the module path and attribute name are both
    valid and that the attribute is a non-None callable.
    """
    _set_min_env()
    module_path, attr = "app.main", "app"
    mod = importlib.import_module(module_path)
    assert hasattr(mod, attr), f"Module `{module_path}` has no attribute `{attr}`"
    obj = getattr(mod, attr)
    assert obj is not None, f"`{module_path}:{attr}` is None — app creation failed"
    assert callable(obj), f"`{module_path}:{attr}` is not callable"


# ---------------------------------------------------------------------------
# Test 5 — Pydantic version is Python 3.14-compatible (>= 2.12.0)
# ---------------------------------------------------------------------------

def test_pydantic_version_supports_python_314() -> None:
    """
    Regression: pydantic==2.8.2 is incompatible with Python 3.14.

    On Python 3.14, inspect.signature() (used by pydantic < 2.12.0 via
    _decorators._is_classmethod_from_sig) calls annotationlib which eagerly
    evaluates the return-type annotation of a model_validator while the class
    body is still being defined.  If the return type references the class by
    name (e.g. -> "PlayerRegisterRequest"), Python raises NameError because
    the class isn't in scope yet; pydantic wraps this as
    PydanticUndefinedAnnotation.

    pydantic 2.12.0 added Python 3.14 support and switched to
    signature_no_eval, which never evaluates annotations.  Any version < 2.12
    on Python 3.14 is broken by design.
    """
    import sys
    from packaging.version import Version
    import pydantic

    pydantic_ver = Version(pydantic.__version__)
    python_ver = sys.version_info

    if python_ver >= (3, 14):
        assert pydantic_ver >= Version("2.12.0"), (
            f"pydantic {pydantic.__version__} is incompatible with Python "
            f"{python_ver.major}.{python_ver.minor}. "
            "Upgrade to pydantic>=2.12.0 in requirements.txt."
        )


# ---------------------------------------------------------------------------
# Test 6 — model_validator(mode="after") class-definition-time safety
# ---------------------------------------------------------------------------

def test_model_validator_after_no_class_annotation_at_definition_time() -> None:
    """
    Verifies that defining a BaseModel with model_validator(mode='after') and
    a Self return type does NOT raise any exception at class-definition time.

    This specifically guards against the pydantic < 2.12 / Python 3.14
    regression where inspect.signature() was called during class body
    execution and tried to evaluate a string annotation referencing the class
    being defined (which didn't exist yet).
    """
    _set_min_env()
    from pydantic import BaseModel, model_validator
    from typing import Self

    # Must not raise at class-definition time or at model_rebuild time
    class _GuardModel(BaseModel):
        x: int = 1

        @model_validator(mode="after")
        def _check(self) -> Self:
            return self

    _GuardModel.model_rebuild(raise_errors=True)
    instance = _GuardModel(x=42)
    assert instance.x == 42
