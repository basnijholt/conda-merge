#!/usr/bin/env python3
"""unidep - Unified Conda and Pip requirements management.

This module provides setuptools integration for unidep.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from unidep._conflicts import resolve_conflicts
from unidep._dependencies_parsing import parse_local_dependencies, parse_requirements
from unidep.utils import (
    UnsupportedPlatformError,
    build_pep508_environment_marker,
    identify_current_platform,
    parse_folder_or_filename,
    warn,
)

if TYPE_CHECKING:
    import sys

    from setuptools import Distribution

    from unidep.platform_definitions import (
        CondaPip,
        Platform,
        Spec,
    )

    if sys.version_info >= (3, 8):
        from typing import Literal
    else:
        from typing_extensions import Literal


def filter_python_dependencies(
    resolved: dict[str, dict[Platform | None, dict[CondaPip, Spec]]],
) -> list[str]:
    """Filter out conda dependencies and return only pip dependencies.

    Examples
    --------
    >>> requirements = parse_requirements("requirements.yaml")
    >>> resolved = resolve_conflicts(
    ...     requirements.requirements, requirements.platforms
    ... )
    >>> python_deps = filter_python_dependencies(resolved)

    """
    pip_deps = []
    for platform_data in resolved.values():
        to_process: dict[Platform | None, Spec] = {}  # platform -> Spec
        for _platform, sources in platform_data.items():
            pip_spec = sources.get("pip")
            if pip_spec:
                to_process[_platform] = pip_spec
        if not to_process:
            continue

        # Check if all Spec objects are identical
        first_spec = next(iter(to_process.values()))
        if all(spec == first_spec for spec in to_process.values()):
            # Build a single combined environment marker
            dep_str = first_spec.name_with_pin(is_pip=True)
            if _platform is not None:
                selector = build_pep508_environment_marker(list(to_process.keys()))  # type: ignore[arg-type]
                dep_str = f"{dep_str}; {selector}"
            pip_deps.append(dep_str)
            continue

        for _platform, pip_spec in to_process.items():
            dep_str = pip_spec.name_with_pin(is_pip=True)
            if _platform is not None:
                selector = build_pep508_environment_marker([_platform])
                dep_str = f"{dep_str}; {selector}"
            pip_deps.append(dep_str)
    return sorted(pip_deps)


class Dependencies(NamedTuple):
    dependencies: list[str]
    extras: dict[str, list[str]]


def get_python_dependencies(
    filename: str
    | Path
    | Literal["requirements.yaml", "pyproject.toml"] = "requirements.yaml",  # noqa: PYI051
    *,
    verbose: bool = False,
    ignore_pins: list[str] | None = None,
    overwrite_pins: list[str] | None = None,
    skip_dependencies: list[str] | None = None,
    platforms: list[Platform] | None = None,
    raises_if_missing: bool = True,
    include_local_dependencies: bool = False,
) -> Dependencies:
    """Extract Python (pip) requirements from a `requirements.yaml` or `pyproject.toml` file."""  # noqa: E501
    try:
        p = parse_folder_or_filename(filename)
    except FileNotFoundError:
        if raises_if_missing:
            raise
        return Dependencies(dependencies=[], extras={})

    requirements = parse_requirements(
        p.path,
        ignore_pins=ignore_pins,
        overwrite_pins=overwrite_pins,
        skip_dependencies=skip_dependencies,
        verbose=verbose,
        extras="*",
    )
    if not platforms:
        platforms = list(requirements.platforms)
    resolved = resolve_conflicts(requirements.requirements, platforms)
    dependencies = filter_python_dependencies(resolved)
    # TODO[Bas]: This currently doesn't correctly handle  # noqa: TD004, TD003, FIX002
    # conflicts between sections in the extras and the main dependencies.
    extras = {
        section: filter_python_dependencies(resolve_conflicts(reqs, platforms))
        for section, reqs in requirements.optional_dependencies.items()
    }
    if include_local_dependencies:
        local_dependencies = parse_local_dependencies(
            p.path_with_extras,
            check_pip_installable=True,
            verbose=verbose,
            raise_if_missing=False,  # skip if local dep is not found
        )
        for paths in local_dependencies.values():
            for path in paths:
                dependencies.append(f"{path.name} @ file://{path.as_posix()}")

    return Dependencies(dependencies=dependencies, extras=extras)


def _deps(requirements_file: Path) -> Dependencies:
    try:
        platforms = [identify_current_platform()]
    except UnsupportedPlatformError:
        warn(
            "Could not identify the current platform."
            " This may result in selecting all platforms."
            " Please report this issue at"
            " https://github.com/basnijholt/unidep/issues",
        )
        # We don't know the current platform, so we can't filter out.
        # This will result in selecting all platforms. But this is better
        # than failing.
        platforms = None

    skip_local_dependencies = bool(os.getenv("UNIDEP_SKIP_LOCAL_DEPS"))
    verbose = bool(os.getenv("UNIDEP_VERBOSE"))
    return get_python_dependencies(
        requirements_file,
        platforms=platforms,
        raises_if_missing=False,
        verbose=verbose,
        include_local_dependencies=not skip_local_dependencies,
    )


def _setuptools_finalizer(dist: Distribution) -> None:  # pragma: no cover
    """Entry point called by setuptools to get the dependencies for a project."""
    # PEP 517 says that "All hooks are run with working directory set to the
    # root of the source tree".
    project_root = Path().resolve()
    try:
        requirements_file = parse_folder_or_filename(project_root).path
    except FileNotFoundError:
        return
    if requirements_file.exists() and dist.install_requires:  # type: ignore[attr-defined]
        msg = (
            "You have a `requirements.yaml` file in your project root or"
            " configured unidep in `pyproject.toml` with `[tool.unidep]`,"
            " but you are also using setuptools' `install_requires`."
            " Remove the `install_requires` line from `setup.py`."
        )
        raise RuntimeError(msg)

    deps = _deps(requirements_file)
    dist.install_requires = deps.dependencies  # type: ignore[attr-defined]

    if deps.extras:
        dist.extras_require = deps.extras  # type: ignore[attr-defined]
