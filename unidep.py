#!/usr/bin/env python3
"""requirements.yaml - Unified Conda and Pip requirements management.

This module provides a command-line tool for managing conda environment.yaml files.
"""
from __future__ import annotations

import argparse
import codecs
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import warnings
from collections import defaultdict
from copy import deepcopy
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

if TYPE_CHECKING:
    import pytest
    from setuptools import Distribution

if sys.version_info >= (3, 8):
    from typing import Literal, get_args
else:  # pragma: no cover
    from typing_extensions import Literal, get_args


__version__ = "0.23.0"
__all__ = [
    "create_conda_env_specification",
    "extract_matching_platforms",
    "filter_python_dependencies",
    "find_requirements_files",
    "get_python_dependencies",
    "parse_project_dependencies",
    "parse_yaml_requirements",
    "resolve_conflicts",
    "setuptools_finalizer",
    "write_conda_environment_file",
]

# Definitions

Platform = Literal[
    "linux-64",
    "linux-aarch64",
    "linux-ppc64le",
    "osx-64",
    "osx-arm64",
    "win-64",
]
Selector = Literal[
    "linux64",
    "aarch64",
    "ppc64le",
    "osx64",
    "arm64",
    "win64",
    "win",
    "unix",
    "linux",
    "osx",
    "macos",
]
CondaPip = Literal["conda", "pip"]

PEP508_MARKERS = {
    "linux-64": "sys_platform == 'linux' and platform_machine == 'x86_64'",
    "linux-aarch64": "sys_platform == 'linux' and platform_machine == 'aarch64'",
    "linux-ppc64le": "sys_platform == 'linux' and platform_machine == 'ppc64le'",
    "osx-64": "sys_platform == 'darwin' and platform_machine == 'x86_64'",
    "osx-arm64": "sys_platform == 'darwin' and platform_machine == 'arm64'",
    "win-64": "sys_platform == 'win32' and platform_machine == 'AMD64'",
    ("linux-64", "linux-aarch64", "linux-ppc64le"): "sys_platform == 'linux'",
    ("osx-64", "osx-arm64"): "sys_platform == 'darwin'",
    (
        "linux-64",
        "linux-aarch64",
        "linux-ppc64le",
        "osx-64",
        "osx-arm64",
    ): "sys_platform == 'linux' or sys_platform == 'darwin'",
}


# The first element of each tuple is the only unique selector
PLATFORM_SELECTOR_MAP: dict[Platform, list[Selector]] = {
    "linux-64": ["linux64", "unix", "linux"],
    "linux-aarch64": ["aarch64", "unix", "linux"],
    "linux-ppc64le": ["ppc64le", "unix", "linux"],
    # "osx64" is a selector unique to conda-build referring to
    # platforms on macOS and the Python architecture is x86-64
    "osx-64": ["osx64", "osx", "macos", "unix"],
    "osx-arm64": ["arm64", "osx", "macos", "unix"],
    "win-64": ["win64", "win"],
}
PLATFORM_SELECTOR_MAP_REVERSE: dict[Selector, set[Platform]] = {}
for _platform, _selectors in PLATFORM_SELECTOR_MAP.items():
    for _selector in _selectors:
        PLATFORM_SELECTOR_MAP_REVERSE.setdefault(_selector, set()).add(_platform)


def _simple_warning_format(
    message: Warning | str,
    category: type[Warning],  # noqa: ARG001
    filename: str,
    lineno: int,
    line: str | None = None,  # noqa: ARG001
) -> str:
    """Format warnings without code context."""
    return (
        f"---------------------\n"
        f"⚠️  *** WARNING *** ⚠️\n"
        f"{message}\n"
        f"Location: {filename}:{lineno}\n"
        f"---------------------\n"
    )


warnings.formatwarning = _simple_warning_format

# Functions for setuptools and conda


def find_requirements_files(
    base_dir: str | Path = ".",
    depth: int = 1,
    filename: str = "requirements.yaml",
    *,
    verbose: bool = False,
) -> list[Path]:
    """Scan a directory for requirements.yaml files."""
    base_path = Path(base_dir)
    found_files = []

    # Define a helper function to recursively scan directories
    def _scan_dir(path: Path, current_depth: int) -> None:
        if verbose:
            print(f"🔍 Scanning in `{path}` at depth {current_depth}")
        if current_depth > depth:
            return
        for child in path.iterdir():
            if child.is_dir():
                _scan_dir(child, current_depth + 1)
            elif child.name == filename:
                found_files.append(child)
                if verbose:
                    print(f"🔍 Found `{filename}` at `{child}`")

    _scan_dir(base_path, 0)
    return sorted(found_files)


def extract_matching_platforms(comment: str) -> list[Platform]:
    """Filter out lines from a requirements file that don't match the platform."""
    # we support a very limited set of selectors that adhere to platform only
    # refs:
    # https://docs.conda.io/projects/conda-build/en/latest/resources/define-metadata.html#preprocessing-selectors
    # https://github.com/conda/conda-lock/blob/3d2bf356e2cf3f7284407423f7032189677ba9be/conda_lock/src_parser/selectors.py

    sel_pat = re.compile(r"#\s*\[([^\[\]]+)\]")
    multiple_brackets_pat = re.compile(r"#.*\].*\[")  # Detects multiple brackets

    filtered_platforms = set()

    for line in comment.splitlines(keepends=False):
        if multiple_brackets_pat.search(line):
            msg = f"Multiple bracketed selectors found in line: '{line}'"
            raise ValueError(msg)

        m = sel_pat.search(line)
        if m:
            conds = m.group(1).split()
            for cond in conds:
                if cond not in PLATFORM_SELECTOR_MAP_REVERSE:
                    valid = list(PLATFORM_SELECTOR_MAP_REVERSE.keys())
                    msg = f"Unsupported platform specifier: '{comment}' use one of {valid}"  # noqa: E501
                    raise ValueError(msg)
                cond = cast(Selector, cond)
                for _platform in PLATFORM_SELECTOR_MAP_REVERSE[cond]:
                    filtered_platforms.add(_platform)

    return list(filtered_platforms)


def _build_pep508_environment_marker(
    platforms: list[Platform | tuple[Platform, ...]],
) -> str:
    """Generate a PEP 508 selector for a list of platforms."""
    sorted_platforms = tuple(sorted(platforms))
    if sorted_platforms in PEP508_MARKERS:
        return PEP508_MARKERS[sorted_platforms]  # type: ignore[index]
    environment_markers = [
        PEP508_MARKERS[platform]
        for platform in sorted(sorted_platforms)
        if platform in PEP508_MARKERS
    ]
    return " or ".join(environment_markers)


def _extract_first_comment(
    commented_map: CommentedMap,
    index_or_key: int | str,
) -> str | None:
    comments = commented_map.ca.items.get(index_or_key, None)
    if comments is None:
        return None
    comment_strings = next(
        c.value.split("\n")[0].rstrip().lstrip() for c in comments if c is not None
    )
    if not comment_strings:
        # empty string
        return None
    return "".join(comment_strings)


def _extract_name_and_pin(package_str: str) -> tuple[str, str | None]:
    """Splits a string into package name and version pinning."""
    # Regular expression to match package name and version pinning
    match = re.match(r"([a-zA-Z0-9_-]+)\s*(.*)", package_str)
    if match:
        package_name = match.group(1).strip()
        version_pin = match.group(2).strip()

        # Return None if version pinning is missing or empty
        if not version_pin:
            return package_name, None
        return package_name, version_pin

    msg = f"Invalid package string: '{package_str}'"
    raise ValueError(msg)


def _parse_dependency(
    dependency: str,
    dependencies: CommentedMap,
    index_or_key: int | str,
    which: Literal["conda", "pip", "both"],
) -> list[Meta]:
    comment = _extract_first_comment(dependencies, index_or_key)
    name, pin = _extract_name_and_pin(dependency)
    if which == "both":
        return [Meta(name, "conda", comment, pin), Meta(name, "pip", comment, pin)]
    return [Meta(name, which, comment, pin)]


class Meta(NamedTuple):
    """Metadata for a dependency."""

    name: str
    which: Literal["conda", "pip"]
    comment: str | None = None
    pin: str | None = None

    def platforms(self) -> list[Platform] | None:
        """Return the platforms for this dependency."""
        if self.comment is None:
            return None
        return extract_matching_platforms(self.comment) or None

    def pprint(self) -> str:
        """Pretty print the dependency."""
        result = f"{self.name}"
        if self.pin is not None:
            result += f" {self.pin}"
        if self.comment is not None:
            result += f" {self.comment}"
        return result


class ParsedRequirements(NamedTuple):
    """Requirements with comments."""

    channels: list[str]
    platforms: list[Platform]
    requirements: dict[str, list[Meta]]


class Requirements(NamedTuple):
    """Requirements as CommentedSeq."""

    # mypy doesn't support CommentedSeq[str], so we use list[str] instead.
    channels: list[str]  # actually a CommentedSeq[str]
    conda: list[str]  # actually a CommentedSeq[str]
    pip: list[str]  # actually a CommentedSeq[str]


def _include_path(include: str) -> Path:
    """Return the path to an included file."""
    path = Path(include)
    if path.is_dir():
        path /= "requirements.yaml"
    return path.resolve()


def parse_yaml_requirements(  # noqa: PLR0912
    *paths: Path,
    verbose: bool = False,
) -> ParsedRequirements:
    """Parse a list of `requirements.yaml` files including comments."""
    requirements: dict[str, list[Meta]] = defaultdict(list)
    channels: set[str] = set()
    platforms: set[Platform] = set()
    datas = []
    seen: set[Path] = set()
    yaml = YAML(typ="rt")
    for p in paths:
        if verbose:
            print(f"📄 Parsing `{p}`")
        with p.open() as f:
            data = yaml.load(f)
            datas.append(data)
        seen.add(p.resolve())

        # Deal with includes
        for include in data.get("includes", []):
            include_path = _include_path(p.parent / include)
            if include_path in seen:
                continue  # Avoids circular includes
            if verbose:
                print(f"📄 Parsing include `{include}`")
            with include_path.open() as f:
                datas.append(yaml.load(f))
            seen.add(include_path)

    for data in datas:
        for channel in data.get("channels", []):
            channels.add(channel)
        for _platform in data.get("platforms", []):
            platforms.add(_platform)
        if "dependencies" not in data:
            continue
        dependencies = data["dependencies"]
        for i, dep in enumerate(data["dependencies"]):
            if isinstance(dep, str):
                metas = _parse_dependency(dep, dependencies, i, "both")
                for meta in metas:
                    requirements[meta.name].append(meta)
                continue
            for which in ["conda", "pip"]:
                if which in dep:
                    metas = _parse_dependency(dep[which], dep, which, which)  # type: ignore[arg-type]
                    for meta in metas:
                        requirements[meta.name].append(meta)

    return ParsedRequirements(sorted(channels), sorted(platforms), dict(requirements))


def _extract_project_dependencies(
    path: Path,
    base_path: Path,
    processed: set,
    dependencies: dict[str, set[str]],
    *,
    check_pip_installable: bool = True,
    verbose: bool = False,
) -> None:
    if path in processed:
        return
    processed.add(path)
    yaml = YAML(typ="safe")
    with path.open() as f:
        data = yaml.load(f)
        for include in data.get("includes", []):
            include_path = _include_path(path.parent / include)
            if not include_path.exists():
                msg = f"Include file `{include_path}` does not exist."
                raise FileNotFoundError(msg)
            include_base_path = str(include_path.parent)
            if include_base_path == str(base_path):
                continue
            if not check_pip_installable or (
                _is_pip_installable(base_path)
                and _is_pip_installable(include_path.parent)
            ):
                dependencies[str(base_path)].add(include_base_path)
            if verbose:
                print(f"🔗 Adding include `{include_path}`")
            _extract_project_dependencies(
                include_path,
                base_path,
                processed,
                dependencies,
                check_pip_installable=check_pip_installable,
            )


def parse_project_dependencies(
    *paths: Path,
    check_pip_installable: bool = True,
    verbose: bool = False,
) -> dict[Path, list[Path]]:
    """Extract local project dependencies from a list of `requirements.yaml` files.

    Works by scanning for `includes` in the `requirements.yaml` files.
    """
    dependencies: dict[str, set[str]] = defaultdict(set)

    for p in paths:
        if verbose:
            print(f"🔗 Analyzing dependencies in `{p}`")
        base_path = p.resolve().parent
        _extract_project_dependencies(
            path=p,
            base_path=base_path,
            processed=set(),
            dependencies=dependencies,
            check_pip_installable=check_pip_installable,
            verbose=verbose,
        )

    return {
        Path(k): sorted({Path(v) for v in v_set})
        for k, v_set in sorted(dependencies.items())
    }


# Conflict resolution functions


def _prepare_metas_for_conflict_resolution(
    requirements: dict[str, list[Meta]],
) -> dict[str, dict[Platform | None, dict[CondaPip, list[Meta]]]]:
    """Prepare and group metadata for conflict resolution.

    This function groups metadata by platform and source for each package.

    :param requirements: Dictionary mapping package names to a list of Meta objects.
    :return: Dictionary mapping package names to grouped metadata.
    """
    prepared_data = {}
    for package, meta_list in requirements.items():
        grouped_metas: dict[Platform | None, dict[CondaPip, list[Meta]]] = defaultdict(
            lambda: defaultdict(list),
        )
        for meta in meta_list:
            platforms = meta.platforms()
            if platforms is None:
                platforms = [None]  # type: ignore[list-item]
            for _platform in platforms:
                grouped_metas[_platform][meta.which].append(meta)
        # Convert defaultdicts to dicts
        prepared_data[package] = {k: dict(v) for k, v in grouped_metas.items()}
    return prepared_data


def _select_preferred_version_within_platform(
    data: dict[Platform | None, dict[CondaPip, list[Meta]]],
) -> dict[Platform | None, dict[CondaPip, Meta]]:
    reduced_data: dict[Platform | None, dict[CondaPip, Meta]] = {}
    for _platform, packages in data.items():
        reduced_data[_platform] = {}
        for which, metas in packages.items():
            if len(metas) > 1:
                # Sort metas by presence of version pin and then by the pin itself
                metas.sort(key=lambda m: (m.pin is not None, m.pin), reverse=True)
                # Keep the first Meta, which has the highest priority
                selected_meta = metas[0]
                discarded_metas = [m for m in metas[1:] if m != selected_meta]
                if discarded_metas:
                    discarded_metas_str = ", ".join(
                        f"`{m.pprint()}` ({m.which})" for m in discarded_metas
                    )
                    on_platform = _platform or "all platforms"
                    warnings.warn(
                        f"Platform Conflict Detected:\n"
                        f"On '{on_platform}', '{selected_meta.pprint()}' ({which})"
                        " is retained. The following conflicting dependencies are"
                        f" discarded: {discarded_metas_str}.",
                        stacklevel=2,
                    )
                reduced_data[_platform][which] = selected_meta
            else:
                # Flatten the list
                reduced_data[_platform][which] = metas[0]
    return reduced_data


def _resolve_conda_pip_conflicts(sources: dict[CondaPip, Meta]) -> dict[CondaPip, Meta]:
    conda_meta = sources.get("conda")
    pip_meta = sources.get("pip")
    if not conda_meta or not pip_meta:  # If either is missing, there is no conflict
        return sources

    # Compare version pins to resolve conflicts
    if conda_meta.pin and not pip_meta.pin:
        return {"conda": conda_meta}  # Prefer conda if it has a pin
    if pip_meta.pin and not conda_meta.pin:
        return {"pip": pip_meta}  # Prefer pip if it has a pin
    if conda_meta.pin == pip_meta.pin:
        return {"conda": conda_meta, "pip": pip_meta}  # Keep both if pins are identical

    # Handle conflict where both conda and pip have different pins
    warnings.warn(
        "Version Pinning Conflict:\n"
        f"Different version specifications for Conda ('{conda_meta.pin}') and Pip"
        f" ('{pip_meta.pin}'). Both versions are retained.",
        stacklevel=2,
    )
    return {"conda": conda_meta, "pip": pip_meta}


def resolve_conflicts(
    requirements: dict[str, list[Meta]],
) -> dict[str, dict[Platform | None, dict[CondaPip, Meta]]]:
    """Resolve conflicts in a dictionary of requirements.

    Uses the ``ParsedRequirements.requirements`` dict returned by
    `parse_yaml_requirements`.
    """
    prepared = _prepare_metas_for_conflict_resolution(requirements)

    resolved = {
        pkg: _select_preferred_version_within_platform(data)
        for pkg, data in prepared.items()
    }
    for platforms in resolved.values():
        for _platform, sources in platforms.items():
            platforms[_platform] = _resolve_conda_pip_conflicts(sources)
    return resolved


# Conda environment file generation functions


class CondaEnvironmentSpec(NamedTuple):
    """A conda environment."""

    channels: list[str]
    platforms: list[Platform]
    conda: list[str | dict[str, str]]  # actually a CommentedSeq[str | dict[str, str]]
    pip: list[str]


CondaPlatform = Literal["unix", "linux", "osx", "win"]


def _conda_sel(sel: str) -> CondaPlatform:
    """Return the allowed `sel(platform)` string."""
    _platform = sel.split("-", 1)[0]
    assert _platform in get_args(CondaPlatform), f"Invalid platform: {_platform}"
    return cast(CondaPlatform, _platform)


def _maybe_expand_none(
    platform_data: dict[Platform | None, dict[CondaPip, Meta]],
) -> None:
    if len(platform_data) > 1 and None in platform_data:
        sources = platform_data.pop(None)
        for _platform in get_args(Platform):
            if _platform not in platform_data:
                # Only add if there is not yet a specific platform
                platform_data[_platform] = sources


def _extract_conda_pip_dependencies(
    resolved_requirements: dict[str, dict[Platform | None, dict[CondaPip, Meta]]],
) -> tuple[
    dict[str, dict[Platform | None, Meta]],
    dict[str, dict[Platform | None, Meta]],
]:
    """Extract and separate conda and pip dependencies."""
    conda: dict[str, dict[Platform | None, Meta]] = {}
    pip: dict[str, dict[Platform | None, Meta]] = {}
    for pkg, platform_data in resolved_requirements.items():
        _maybe_expand_none(platform_data)
        for _platform, sources in platform_data.items():
            if "conda" in sources:
                conda.setdefault(pkg, {})[_platform] = sources["conda"]
            else:
                pip.setdefault(pkg, {})[_platform] = sources["pip"]
    return conda, pip


def _resolve_multiple_platform_conflicts(
    platform_to_meta: dict[Platform | None, Meta],
) -> None:
    """Fix conflicts for deps with platforms that map to a single Conda platform.

    In a Conda environment with dependencies across various platforms (like
    'linux-aarch64', 'linux64'), this function ensures consistency in metadata
    for each Conda platform (e.g., 'sel(linux): ...'). It maps each platform to
    a Conda platform and resolves conflicts by retaining the first `Meta` object
    per Conda platform, discarding others. This approach guarantees uniform
    metadata across different but equivalent platforms.
    """
    valid: dict[
        CondaPlatform,
        dict[Meta, list[Platform | None]],
    ] = defaultdict(lambda: defaultdict(list))
    for _platform, meta in platform_to_meta.items():
        assert _platform is not None
        conda_platform = _conda_sel(_platform)
        valid[conda_platform][meta].append(_platform)

    for conda_platform, meta_to_platforms in valid.items():
        # We cannot distinguish between e.g., linux-64 and linux-aarch64
        # (which becomes linux). So of the list[Platform] we only need to keep
        # one Platform. We can pop the rest from `platform_to_meta`. This is
        # not a problem because they share the same `Meta` object.
        for platforms in meta_to_platforms.values():
            for j, _platform in enumerate(platforms):
                if j >= 1:
                    platform_to_meta.pop(_platform)

        # Now make sure that valid[conda_platform] has only one key.
        # This means that all `Meta`s for the different Platforms that map to a
        # CondaPlatform are identical. If len > 1, we have a conflict, and we
        # select one of the `Meta`s.
        if len(meta_to_platforms) > 1:
            # We have a conflict, select the first one.
            first, *others = meta_to_platforms.keys()
            msg = (
                f"Dependency Conflict on '{conda_platform}':\n"
                f"Multiple versions detected. Retaining '{first.pprint()}' and"
                f" discarding conflicts: {', '.join(o.pprint() for o in others)}."
            )
            warnings.warn(msg, stacklevel=2)
            for other in others:
                platforms = meta_to_platforms[other]
                for _platform in platforms:
                    if _platform in platform_to_meta:  # might have been popped already
                        platform_to_meta.pop(_platform)
        # Now we have only one `Meta` left, so we can select it.


def _add_comment(commment_seq: CommentedSeq, platform: Platform) -> None:
    comment = f"# [{PLATFORM_SELECTOR_MAP[platform][0]}]"
    commment_seq.yaml_add_eol_comment(comment, len(commment_seq) - 1)


def create_conda_env_specification(  # noqa: PLR0912
    resolved_requirements: dict[str, dict[Platform | None, dict[CondaPip, Meta]]],
    channels: list[str],
    platforms: list[Platform],
    selector: Literal["sel", "comment"] = "sel",
) -> CondaEnvironmentSpec:
    """Create a conda environment specification from resolved requirements."""
    if selector not in ("sel", "comment"):  # pragma: no cover
        msg = f"Invalid selector: {selector}, must be one of ['sel', 'comment']"
        raise ValueError(msg)
    if platforms and not set(platforms).issubset(get_args(Platform)):
        msg = f"Invalid platform: {platforms}, must contain only {get_args(Platform)}"
        raise ValueError(msg)

    # Split in conda and pip dependencies and prefer conda over pip
    conda, pip = _extract_conda_pip_dependencies(resolved_requirements)

    conda_deps: list[str | dict[str, str]] = CommentedSeq()
    pip_deps: list[str] = CommentedSeq()
    for platform_to_meta in conda.values():
        if len(platform_to_meta) > 1 and selector == "sel":
            # None has been expanded already if len>1
            _resolve_multiple_platform_conflicts(platform_to_meta)
        for _platform, meta in sorted(platform_to_meta.items()):
            if _platform is not None and platforms and _platform not in platforms:
                continue
            dep_str = meta.name
            if meta.pin is not None:
                dep_str += f" {meta.pin}"
            if len(platforms) != 1 and _platform is not None:
                if selector == "sel":
                    sel = _conda_sel(_platform)
                    dep_str = {f"sel({sel})": dep_str}  # type: ignore[assignment]
                conda_deps.append(dep_str)
                if selector == "comment":
                    _add_comment(conda_deps, _platform)
            else:
                conda_deps.append(dep_str)

    for platform_to_meta in pip.values():
        meta_to_platforms: dict[Meta, list[Platform | None]] = {}
        for _platform, meta in platform_to_meta.items():
            meta_to_platforms.setdefault(meta, []).append(_platform)

        for meta, _platforms in meta_to_platforms.items():
            dep_str = meta.name
            if meta.pin is not None:
                dep_str += f" {meta.pin}"
            if _platforms != [None]:
                if selector == "sel":
                    marker = _build_pep508_environment_marker(_platforms)  # type: ignore[arg-type]
                    dep_str = f"{dep_str}; {marker}"
                    pip_deps.append(dep_str)
                else:
                    assert selector == "comment"
                    # We can only add comments with a single platform because
                    # `conda-lock` doesn't implement logic, e.g., [linux or win]
                    # should be spread into two lines, one with [linux] and the
                    # other with [win].
                    for _platform in _platforms:
                        # We're not adding a PEP508 marker here
                        _platform = cast(Platform, _platform)
                        pip_deps.append(dep_str)
                        _add_comment(pip_deps, _platform)
            else:
                pip_deps.append(dep_str)

    return CondaEnvironmentSpec(channels, platforms, conda_deps, pip_deps)


def write_conda_environment_file(
    env_spec: CondaEnvironmentSpec,
    output_file: str | Path | None = "environment.yaml",
    name: str = "myenv",
    *,
    verbose: bool = False,
) -> None:
    """Generate a conda environment.yaml file or print to stdout."""
    resolved_dependencies = deepcopy(env_spec.conda)
    if env_spec.pip:
        resolved_dependencies.append({"pip": env_spec.pip})  # type: ignore[arg-type, dict-item]
    env_data = CommentedMap({"name": name})
    if env_spec.channels:
        env_data["channels"] = env_spec.channels
    if resolved_dependencies:
        env_data["dependencies"] = resolved_dependencies
    if env_spec.platforms:
        env_data["platforms"] = env_spec.platforms
    yaml = YAML(typ="rt")
    yaml.default_flow_style = False
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=2, offset=2)
    if output_file:
        if verbose:
            print(f"📝 Generating environment file at `{output_file}`")
        with open(output_file, "w") as f:  # noqa: PTH123
            yaml.dump(env_data, f)
        if verbose:
            print("📝 Environment file generated successfully.")
        _add_comment_to_file(output_file)
    else:
        yaml.dump(env_data, sys.stdout)


def _add_comment_to_file(
    filename: str | Path,
    extra_lines: list[str] | None = None,
) -> None:
    """Add a comment to the top of a file."""
    if extra_lines is None:
        extra_lines = []
    with open(filename, "r+") as f:  # noqa: PTH123
        content = f.read()
        f.seek(0, 0)
        command_line_args = " ".join(sys.argv[1:])
        txt = [
            f"# This file is created and managed by `unidep` {__version__}.",
            "# For details see https://github.com/basnijholt/unidep",
            f"# File generated with: `unidep {command_line_args}`",
            *extra_lines,
        ]
        content = "\n".join(txt) + "\n\n" + content
        f.write(content)


# Python setuptools integration functions


def filter_python_dependencies(
    resolved_requirements: dict[str, dict[Platform | None, dict[CondaPip, Meta]]],
    platforms: list[Platform] | None = None,
) -> list[str]:
    """Filter out conda dependencies and return only pip dependencies.

    Examples
    --------
    >>> requirements = parse_yaml_requirements("requirements.yaml")
    >>> resolved_requirements = resolve_conflicts(requirements.requirements)
    >>> python_dependencies = filter_python_dependencies(resolved_requirements)
    """
    pip_deps = []
    for platform_data in resolved_requirements.values():
        _maybe_expand_none(platform_data)
        to_process: dict[Platform | None, Meta] = {}  # platform -> Meta
        for _platform, sources in platform_data.items():
            if (
                _platform is not None
                and platforms is not None
                and _platform not in platforms
            ):
                continue
            pip_meta = sources.get("pip")
            if pip_meta:
                to_process[_platform] = pip_meta
        if not to_process:
            continue

        # Check if all Meta objects are identical
        first_meta = next(iter(to_process.values()))
        if all(meta == first_meta for meta in to_process.values()):
            # Build a single combined environment marker
            dep_str = first_meta.name
            if first_meta.pin is not None:
                dep_str += f" {first_meta.pin}"
            if _platform is not None:
                selector = _build_pep508_environment_marker(list(to_process.keys()))  # type: ignore[arg-type]
                dep_str = f"{dep_str}; {selector}"
            pip_deps.append(dep_str)
            continue

        for _platform, pip_meta in to_process.items():
            dep_str = pip_meta.name
            if pip_meta.pin is not None:
                dep_str += f" {pip_meta.pin}"
            if _platform is not None:
                selector = _build_pep508_environment_marker([_platform])
                dep_str = f"{dep_str}; {selector}"
            pip_deps.append(dep_str)
    return sorted(pip_deps)


def get_python_dependencies(
    filename: str | Path = "requirements.yaml",
    *,
    verbose: bool = False,
    platforms: list[Platform] | None = None,
    raises_if_missing: bool = True,
) -> list[str]:
    """Extract Python (pip) requirements from requirements.yaml file."""
    p = Path(filename)
    if not p.exists():
        if raises_if_missing:
            msg = f"File {filename} not found."
            raise FileNotFoundError(msg)
        return []

    requirements = parse_yaml_requirements(p, verbose=verbose)
    resolved_requirements = resolve_conflicts(requirements.requirements)
    return filter_python_dependencies(
        resolved_requirements,
        platforms=platforms or list(requirements.platforms),
    )


def _identify_current_platform() -> Platform:
    """Detect the current platform."""
    system = platform.system().lower()
    architecture = platform.machine().lower()

    if system == "linux":
        if architecture == "x86_64":
            return "linux-64"
        if architecture == "aarch64":
            return "linux-aarch64"
        if architecture == "ppc64le":
            return "linux-ppc64le"
        msg = "Unsupported Linux architecture"
        raise ValueError(msg)
    if system == "darwin":
        if architecture == "x86_64":
            return "osx-64"
        if architecture == "arm64":
            return "osx-arm64"
        msg = "Unsupported macOS architecture"
        raise ValueError(msg)
    if system == "windows":
        if "64" in architecture:
            return "win-64"
        msg = "Unsupported Windows architecture"
        raise ValueError(msg)
    msg = "Unsupported operating system"
    raise ValueError(msg)


def setuptools_finalizer(dist: Distribution) -> None:  # pragma: no cover
    """Entry point called by setuptools to get the dependencies for a project."""
    # PEP 517 says that "All hooks are run with working directory set to the
    # root of the source tree".
    project_root = Path().resolve()
    requirements_file = project_root / "requirements.yaml"
    if requirements_file.exists() and dist.install_requires:
        msg = (
            "You have a requirements.yaml file in your project root, "
            "but you are also using setuptools' install_requires. "
            "Please use one or the other, but not both."
        )
        raise RuntimeError(msg)
    dist.install_requires = list(
        get_python_dependencies(
            requirements_file,
            platforms=[_identify_current_platform()],
            raises_if_missing=False,
        ),
    )


# Command line interface functions


def _escape_unicode(string: str) -> str:
    return codecs.decode(string, "unicode_escape")


def _add_common_args(
    sub_parser: argparse.ArgumentParser,
    options: set[str],
) -> None:  # pragma: no cover
    if "directory" in options:
        sub_parser.add_argument(
            "-d",
            "--directory",
            type=Path,
            default=".",
            help="Base directory to scan for requirements.yaml file(s), by default `.`",
        )
    if "file" in options:
        sub_parser.add_argument(
            "-f",
            "--file",
            type=Path,
            default="requirements.yaml",
            help="The requirements.yaml file to parse or folder that contains"
            " that file, by default `requirements.yaml`",
        )
    if "verbose" in options:
        sub_parser.add_argument(
            "-v",
            "--verbose",
            action="store_true",
            help="Print verbose output",
        )
    if "platform" in options:
        current_platform = _identify_current_platform()
        sub_parser.add_argument(
            "--platform",
            "-p",
            type=str,
            action="append",  # Allow multiple instances of -p
            default=None,  # Default is a list with the current platform
            choices=get_args(Platform),
            help="The platform(s) to get the requirements for. "
            "Multiple platforms can be specified. "
            f"By default, the current platform (`{current_platform}`) is used.",
        )
    if "editable" in options:
        sub_parser.add_argument(
            "-e",
            "--editable",
            action="store_true",
            help="Install the project in editable mode",
        )
    if "depth" in options:
        sub_parser.add_argument(
            "--depth",
            type=int,
            default=1,
            help="Maximum depth to scan for requirements.yaml files, by default 1",
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Unified Conda and Pip requirements management.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Subcommands")

    # Subparser for the 'merge' command
    parser_merge = subparsers.add_parser(
        "merge",
        help="Merge requirements to conda installable environment.yaml",
    )
    parser_merge.add_argument(
        "-o",
        "--output",
        type=Path,
        default="environment.yaml",
        help="Output file for the conda environment, by default `environment.yaml`",
    )
    parser_merge.add_argument(
        "-n",
        "--name",
        type=str,
        default="myenv",
        help="Name of the conda environment, by default `myenv`",
    )
    parser_merge.add_argument(
        "--stdout",
        action="store_true",
        help="Output to stdout instead of a file",
    )
    parser_merge.add_argument(
        "--selector",
        type=str,
        choices=("sel", "comment"),
        default="sel",
        help="The selector to use for the environment markers, if `sel` then"
        " `- numpy # [linux]` becomes `sel(linux): numpy`, if `comment` then"
        " it remains `- numpy # [linux]`, by default `sel`",
    )
    _add_common_args(parser_merge, {"directory", "verbose", "platform", "depth"})

    # Subparser for the 'pip' and 'conda' command
    help_str = "Get the {} requirements for the current platform only."
    parser_pip = subparsers.add_parser("pip", help=help_str.format("pip"))
    parser_conda = subparsers.add_parser("conda", help=help_str.format("conda"))
    for sub_parser in [parser_pip, parser_conda]:
        _add_common_args(sub_parser, {"verbose", "platform", "file"})
        sub_parser.add_argument(
            "--separator",
            type=str,
            default=" ",
            help="The separator between the dependencies, by default ` `",
        )

    # Subparser for the 'install' command
    parser_install = subparsers.add_parser(
        "install",
        help="Install the dependencies of a single `requirements.yaml` file in the"
        " currently activated conda environment with conda, then install the remaining"
        " dependencies with pip, and finally install the current package"
        " with `pip install [-e] .`.",
    )
    # Add positional argument for the file
    parser_install.add_argument(
        "file",
        type=Path,
        help="The requirements.yaml file to parse or folder that contains that"
        " file, by default `.`",
        default=".",
    )
    parser_install.add_argument(
        "--skip-local",
        action="store_true",
        help="Skip installing local dependencies",
    )
    parser_install.add_argument(
        "--skip-pip",
        action="store_true",
        help="Skip installing pip dependencies from `requirements.yaml`",
    )
    parser_install.add_argument(
        "--skip-conda",
        action="store_true",
        help="Skip installing conda dependencies from `requirements.yaml`",
    )
    _add_common_args(parser_install, {"verbose", "editable"})
    parser_install.add_argument(
        "--conda-executable",
        type=str,
        choices=("conda", "mamba", "micromamba"),
        help="The conda executable to use",
        default=None,
    )
    parser_install.add_argument(
        "--dry-run",
        "--dry",
        action="store_true",
        help="Only print the commands that would be run",
    )

    # Subparser for the 'conda-lock' command
    parser_lock = subparsers.add_parser(
        "conda-lock",
        help="Generate a global conda-lock file of a collection of `requirements.yaml`"
        " files. Additionally, generate a conda-lock file for each separate"
        " `requirements.yaml` file based on the global lock file.",
    )
    parser_lock.add_argument(
        "--only-global",
        action="store_true",
        help="Only generate the global lock file",
    )
    parser_lock.add_argument(
        "--check-input-hash",
        action="store_true",
        help="Check existing input hashes in lockfiles before regenerating lock files."
        " This flag is directly passed to `conda-lock`.",
    )
    _add_common_args(parser_lock, {"directory", "verbose", "platform", "depth"})

    # Subparser for the 'version' command
    parser_merge = subparsers.add_parser(
        "version",
        help="Print version information of unidep.",
    )

    args = parser.parse_args()

    if args.command is None:  # pragma: no cover
        parser.print_help()
        sys.exit(1)

    if "file" in args and args.file.is_dir():
        args.file = args.file / "requirements.yaml"
    return args


def _identify_conda_executable() -> str:  # pragma: no cover
    """Identify the conda executable to use.

    This function checks for micromamba, mamba, and conda in that order.
    """
    if shutil.which("micromamba"):
        return "micromamba"
    if shutil.which("mamba"):
        return "mamba"
    if shutil.which("conda"):
        return "conda"
    msg = "Could not identify conda executable."
    raise RuntimeError(msg)


def _is_pip_installable(folder: str | Path) -> bool:  # pragma: no cover
    """Determine if the project is pip installable.

    Checks for existence of setup.py or [build-system] in pyproject.toml.
    """
    path = Path(folder)
    if (path / "setup.py").exists():
        return True

    # When toml makes it into the standard library, we can use that instead
    # For now this is good enough, except it doesn't handle the case where
    # [build-system] is inside of a multi-line literal string.
    pyproject_path = path / "pyproject.toml"
    if pyproject_path.exists():
        with pyproject_path.open("r") as file:
            for line in file:
                if line.strip().startswith("[build-system]"):
                    return True
    return False


def _format_inline_conda_package(package: str) -> str:
    name, pin = _extract_name_and_pin(package)
    if pin is None:
        return name
    return f'{name}"{pin.strip()}"'


def _pip_install_local(
    folder: str | Path,
    *,
    editable: bool,
    dry_run: bool,
) -> None:  # pragma: no cover
    if not os.path.isabs(folder):  # noqa: PTH117
        relative_prefix = ".\\" if os.name == "nt" else "./"
        folder = f"{relative_prefix}{folder}"
    pip_command = [sys.executable, "-m", "pip", "install", str(folder)]
    if editable:
        pip_command.insert(-1, "-e")
    print(f"📦 Installing project with `{' '.join(pip_command)}`\n")
    if not dry_run:
        subprocess.run(pip_command, check=True)  # noqa: S603


def _install_command(
    *,
    conda_executable: str,
    dry_run: bool,
    editable: bool,
    file: Path,
    skip_local: bool = False,
    skip_pip: bool = False,
    skip_conda: bool = False,
    verbose: bool = False,
) -> None:
    """Install the dependencies of a single `requirements.yaml` file."""
    requirements = parse_yaml_requirements(file, verbose=verbose)
    resolved_requirements = resolve_conflicts(requirements.requirements)
    env_spec = create_conda_env_specification(
        resolved_requirements,
        requirements.channels,
        platforms=[_identify_current_platform()],
    )
    if env_spec.conda and not skip_conda:
        conda_executable = conda_executable or _identify_conda_executable()
        channel_args = ["--override-channels"] if env_spec.channels else []
        for channel in env_spec.channels:
            channel_args.extend(["--channel", channel])

        conda_command = [
            conda_executable,
            "install",
            "--yes",
            *channel_args,
        ]
        # When running the command in terminal, we need to wrap the pin in quotes
        # so what we print is what the user would type (copy-paste).
        to_print = [_format_inline_conda_package(pkg) for pkg in env_spec.conda]  # type: ignore[arg-type]
        conda_command_str = " ".join((*conda_command, *to_print))
        print(f"📦 Installing conda dependencies with `{conda_command_str}`\n")  # type: ignore[arg-type]
        if not dry_run:  # pragma: no cover
            subprocess.run((*conda_command, *env_spec.conda), check=True)  # type: ignore[arg-type]  # noqa: S603
    if env_spec.pip and not skip_pip:
        pip_command = [sys.executable, "-m", "pip", "install", *env_spec.pip]
        print(f"📦 Installing pip dependencies with `{' '.join(pip_command)}`\n")
        if not dry_run:  # pragma: no cover
            subprocess.run(pip_command, check=True)  # noqa: S603

    if not skip_local:
        if _is_pip_installable(file.parent):  # pragma: no cover
            folder = file.parent
            _pip_install_local(folder, editable=editable, dry_run=dry_run)
        else:  # pragma: no cover
            print(
                f"⚠️  Project {file.parent} is not pip installable. "
                "Could not find setup.py or [build-system] in pyproject.toml.",
            )

    if not skip_local:
        # Install local dependencies (if any) included via `includes:`
        local_dependencies = parse_project_dependencies(
            file,
            check_pip_installable=True,
            verbose=verbose,
        )
        assert len(local_dependencies) <= 1
        names = {k.name: [dep.name for dep in v] for k, v in local_dependencies.items()}
        print(f"📝 Found local dependencies: {names}\n")
        for deps in sorted(local_dependencies.values()):
            for dep in sorted(deps):
                _pip_install_local(dep, editable=editable, dry_run=dry_run)

    if not dry_run:  # pragma: no cover
        print("✅ All dependencies installed successfully.")


def _merge_command(
    *,
    depth: int,
    directory: Path,
    name: str,
    output: Path,
    stdout: bool,
    selector: Literal["sel", "comment"],
    platforms: list[Platform],
    verbose: bool,
) -> None:  # pragma: no cover
    # When using stdout, suppress verbose output
    verbose = verbose and not stdout

    found_files = find_requirements_files(
        directory,
        depth,
        verbose=verbose,
    )
    if not found_files:
        print(f"❌ No requirements.yaml files found in {directory}")
        sys.exit(1)
    requirements = parse_yaml_requirements(*found_files, verbose=verbose)
    resolved_requirements = resolve_conflicts(requirements.requirements)
    env_spec = create_conda_env_specification(
        resolved_requirements,
        requirements.channels,
        requirements.platforms or platforms,
        selector=selector,
    )
    output_file = None if stdout else output
    write_conda_environment_file(env_spec, output_file, name, verbose=verbose)
    if output_file:
        found_files_str = ", ".join(f"`{f}`" for f in found_files)
        print(
            f"✅ Generated environment file at `{output_file}` from {found_files_str}",
        )


def _remove_top_comments(filename: str | Path) -> None:
    """Removes the top comments (lines starting with '#') from a file."""
    with open(filename) as file:  # noqa: PTH123
        lines = file.readlines()

    first_non_comment = next(
        (i for i, line in enumerate(lines) if not line.strip().startswith("#")),
        len(lines),
    )
    content_without_comments = lines[first_non_comment:]
    with open(filename, "w") as file:  # noqa: PTH123
        file.writelines(content_without_comments)


def _run_conda_lock(
    tmp_env: Path,
    conda_lock_output: Path,
    *,
    check_input_hash: bool = False,
) -> None:  # pragma: no cover
    if shutil.which("conda-lock") is None:
        msg = (
            "Cannot find `conda-lock`."
            " Please install it with `pip install conda-lock`, or"
            " `pipx install conda-lock`, or"
            " `conda install -c conda-forge conda-lock`."
        )
        raise RuntimeError(msg)
    if not check_input_hash and conda_lock_output.exists():
        print(f"🗑️ Removing existing `{conda_lock_output}`")
        conda_lock_output.unlink()
    cmd = [
        "conda-lock",
        "lock",
        "--file",
        str(tmp_env),
        "--lockfile",
        str(conda_lock_output),
    ]
    if check_input_hash:
        cmd.append("--check-input-hash")
    print(f"🔒 Locking dependencies with `{' '.join(cmd)}`\n")
    try:
        subprocess.run(cmd, check=True, text=True, capture_output=True)  # noqa: S603
        _remove_top_comments(conda_lock_output)
        _add_comment_to_file(
            conda_lock_output,
            extra_lines=[
                "#",
                "# This environment can be installed with",
                "# `micromamba create -f conda-lock.yml -n myenv`",
                "# This file is a `conda-lock` file generated via `unidep`.",
                "# For details see https://conda.github.io/conda-lock/",
            ],
        )
    except subprocess.CalledProcessError as e:
        print("❌ Error occurred:\n", e)
        print("Return code:", e.returncode)
        print("Output:", e.output)
        print("Error Output:", e.stderr)
        sys.exit(1)


def _conda_lock_global(
    *,
    depth: int,
    directory: str | Path,
    platform: list[Platform],
    verbose: bool,
    check_input_hash: bool,
) -> Path:
    """Generate a conda-lock file for the global dependencies."""
    directory = Path(directory)
    tmp_env = directory / "tmp.environment.yaml"
    conda_lock_output = directory / "conda-lock.yml"
    _merge_command(
        depth=depth,
        directory=directory,
        name="myenv",
        output=tmp_env,
        stdout=False,
        selector="comment",
        platforms=platform,
        verbose=verbose,
    )
    _run_conda_lock(tmp_env, conda_lock_output, check_input_hash=check_input_hash)
    print(f"✅ Global dependencies locked successfully in `{conda_lock_output}`.")
    return conda_lock_output


class LockSpec(NamedTuple):
    packages: dict[tuple[CondaPip, Platform, str], dict[str, Any]]
    dependencies: dict[tuple[CondaPip, Platform, str], set[str]]


def _parse_conda_lock_packages(
    conda_lock_packages: list[dict[str, Any]],
) -> LockSpec:
    deps: dict[CondaPip, dict[Platform, dict[str, set[str]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(set)),
    )

    def _recurse(
        package_name: str,
        resolved: dict[str, set[str]],
        dependencies: dict[str, set[str]],
    ) -> set[str]:
        if package_name in resolved:
            return resolved[package_name]
        all_deps = set(dependencies[package_name])
        for dep in dependencies[package_name]:
            all_deps.update(_recurse(dep, resolved, dependencies))
        resolved[package_name] = all_deps
        return all_deps

    for p in conda_lock_packages:
        deps[p["manager"]][p["platform"]][p["name"]].update(p["dependencies"])

    resolved: dict[CondaPip, dict[Platform, dict[str, set[str]]]] = {}
    for manager, platforms in deps.items():
        resolved_manager = resolved.setdefault(manager, {})
        for _platform, pkgs in platforms.items():
            _resolved: dict[str, set[str]] = {}
            for package in list(pkgs):
                _recurse(package, _resolved, pkgs)
            resolved_manager[_platform] = _resolved

    packages: dict[tuple[CondaPip, Platform, str], dict[str, Any]] = {}
    for p in conda_lock_packages:
        key = (p["manager"], p["platform"], p["name"])
        assert key not in packages
        packages[key] = p

    # Flatten the `dependencies` dict to same format as `packages`
    dependencies = {
        (which, platform, name): deps
        for which, platforms in resolved.items()
        for platform, pkgs in platforms.items()
        for name, deps in pkgs.items()
    }
    return LockSpec(packages, dependencies)


def _add_package_to_lock(
    *,
    name: str,
    which: CondaPip,
    platform: Platform,
    packages: dict[tuple[CondaPip, Platform, str], dict[str, Any]],
    locked: list[dict[str, Any]],
    locked_keys: set[tuple[CondaPip, Platform, str]],
) -> tuple[CondaPip, Platform, str] | None:
    key = (which, platform, name)
    if key not in packages:
        return key
    if key not in locked_keys:
        locked.append(packages[key])
        locked_keys.add(key)  # Add identifier to the set
    return None


def _add_package_with_dependencies_to_lock(
    *,
    name: str,
    which: CondaPip,
    platform: Platform,
    lock_spec: LockSpec,
    locked: list[dict[str, Any]],
    locked_keys: set[tuple[CondaPip, Platform, str]],
    missing_keys: set[tuple[CondaPip, Platform, str]],
) -> None:
    missing_key = _add_package_to_lock(
        name=name,
        which=which,
        platform=platform,
        packages=lock_spec.packages,
        locked=locked,
        locked_keys=locked_keys,
    )
    if missing_key is not None:
        missing_keys.add(missing_key)
    for dep in lock_spec.dependencies.get((which, platform, name), set()):
        if dep.startswith("__"):
            continue  # Skip meta packages
        missing_key = _add_package_to_lock(
            name=dep,
            which=which,
            platform=platform,
            packages=lock_spec.packages,
            locked=locked,
            locked_keys=locked_keys,
        )
        if missing_key is not None:
            missing_keys.add(missing_key)


def _handle_missing_keys(
    lock_spec: LockSpec,
    locked_keys: set[tuple[CondaPip, Platform, str]],
    missing_keys: set[tuple[CondaPip, Platform, str]],
    locked: list[dict[str, Any]],
) -> None:
    add_pkg = partial(
        _add_package_with_dependencies_to_lock,
        lock_spec=lock_spec,
        locked=locked,
        locked_keys=locked_keys,
        missing_keys=missing_keys,
    )

    # Do not re-add packages that with pip that are
    # already added with conda
    for which, _platform, name in locked_keys:
        if which == "conda":
            key = ("pip", _platform, name)
            missing_keys.discard(key)  # type: ignore[arg-type]

    # Add missing pip packages using conda (if possible)
    for which, _platform, name in list(missing_keys):
        if which == "pip":
            missing_keys.discard((which, _platform, name))
            add_pkg(name=name, which="conda", platform=_platform)
            if ("conda", _platform, name) in missing_keys:
                # If the package wasn't added, restore the missing key
                missing_keys.discard(("conda", _platform, name))
                missing_keys.add(("pip", _platform, name))

    if not missing_keys:
        return

    # Finally there might be some pip packages that are missing
    # because in the lock file they are installed with conda, however,
    # on Conda the name might be different than on PyPI. For example,
    # `msgpack` (pip) and `msgpack-python` (conda).
    options = {
        (which, platform, name): pkg
        for which, platform, name in missing_keys
        for (_which, _platform, _name), pkg in lock_spec.packages.items()
        if which == "pip"
        and _which == "conda"
        and platform == _platform
        and name in _name
    }
    for (which, _platform, name), pkg in options.items():
        names = _download_and_get_package_names(pkg)
        if names is None:
            continue
        if name in names:
            add_pkg(name=pkg["name"], which=pkg["manager"], platform=pkg["platform"])
            missing_keys.discard((which, _platform, name))
    if missing_keys:
        print(f"❌ Missing keys {missing_keys}")


def _conda_lock_subpackage(
    *,
    file: Path,
    lock_spec: LockSpec,
    channels: list[str],
    platforms: list[Platform],
    yaml: YAML | None,  # Passing this to preserve order!
) -> Path:
    requirements = parse_yaml_requirements(file)
    locked: list[dict[str, Any]] = []
    locked_keys: set[tuple[CondaPip, Platform, str]] = set()
    missing_keys: set[tuple[CondaPip, Platform, str]] = set()

    add_pkg = partial(
        _add_package_with_dependencies_to_lock,
        lock_spec=lock_spec,
        locked=locked,
        locked_keys=locked_keys,
        missing_keys=missing_keys,
    )

    for name, metas in requirements.requirements.items():
        if name.startswith("__"):
            continue  # Skip meta packages
        for meta in metas:
            _platforms = meta.platforms()
            if _platforms is None:
                _platforms = platforms
            else:
                _platforms = [p for p in _platforms if p in platforms]

            for _platform in _platforms:
                if _platform not in platforms:
                    continue
                add_pkg(name=name, which=meta.which, platform=_platform)
    _handle_missing_keys(
        lock_spec=lock_spec,
        locked_keys=locked_keys,
        missing_keys=missing_keys,
        locked=locked,
    )

    locked = sorted(locked, key=lambda p: (p["manager"], p["name"], p["platform"]))

    if yaml is None:  # pragma: no cover
        # When passing the same YAML instance that is used to load the file,
        # we preserve the order of the keys.
        yaml = YAML(typ="rt")
    yaml.default_flow_style = False
    yaml.width = 4096
    yaml.representer.ignore_aliases = lambda *_: True  # Disable anchors
    conda_lock_output = file.parent / "conda-lock.yml"
    metadata = {
        "content_hash": {p: "unidep-is-awesome" for p in platforms},
        "channels": [{"url": c, "used_env_vars": []} for c in channels],
        "platforms": platforms,
        "sources": [str(file)],
    }
    with conda_lock_output as fp:
        yaml.dump({"version": 1, "metadata": metadata, "package": locked}, fp)
    _add_comment_to_file(
        conda_lock_output,
        extra_lines=[
            "#",
            "# This environment can be installed with",
            "# `micromamba create -f conda-lock.yml -n myenv`",
            "# This file is a `conda-lock` file generated via `unidep`.",
            "# For details see https://conda.github.io/conda-lock/",
        ],
    )
    return conda_lock_output


def _download_and_get_package_names(
    package: dict[str, Any],
    component: Literal["info", "pkg"] | None = None,
) -> list[str] | None:
    try:
        import conda_package_handling.api
    except ImportError:  # pragma: no cover
        print(
            "❌ Could not import `conda-package-handling` module."
            " Please install it with `pip install conda-package-handling`.",
        )
        sys.exit(1)
    url = package["url"]
    if package["manager"] != "conda":  # pragma: no cover
        return None
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        file_path = temp_path / Path(url).name
        urllib.request.urlretrieve(url, str(file_path))  # noqa: S310
        conda_package_handling.api.extract(
            str(file_path),
            dest_dir=str(temp_path),
            components=component,
        )

        if (temp_path / "site-packages").exists():
            site_packages_path = temp_path / "site-packages"
        elif (temp_path / "lib").exists():
            lib_path = temp_path / "lib"
            python_dirs = [
                d
                for d in lib_path.iterdir()
                if d.is_dir() and d.name.startswith("python")
            ]
            if not python_dirs:
                return None
            site_packages_path = python_dirs[0] / "site-packages"
        else:
            return None

        if not site_packages_path.exists():
            return None

        return [
            d.name
            for d in site_packages_path.iterdir()
            if d.is_dir() and not d.name.endswith((".dist-info", ".egg-info"))
        ]


def _conda_lock_subpackages(
    directory: str | Path,
    depth: int,
    conda_lock_file: str | Path,
) -> list[Path]:
    directory = Path(directory)
    conda_lock_file = Path(conda_lock_file)
    with YAML(typ="rt") as yaml, conda_lock_file.open() as fp:
        data = yaml.load(fp)
    channels = [c["url"] for c in data["metadata"]["channels"]]
    platforms = data["metadata"]["platforms"]
    lock_spec = _parse_conda_lock_packages(data["package"])

    lock_files: list[Path] = []
    # Assumes that different platforms have the same versions
    found_files = find_requirements_files(directory, depth)
    for file in found_files:
        if file.parent == directory:
            # This is a `requirements.yaml` file in the root directory
            # for e.g., common packages, so skip it.
            continue
        sublock_file = _conda_lock_subpackage(
            file=file,
            lock_spec=lock_spec,
            channels=channels,
            platforms=platforms,
            yaml=yaml,
        )
        print(f"📝 Generated lock file for `{file}`: `{sublock_file}`")
        lock_files.append(sublock_file)
    return lock_files


def _conda_lock_command(
    *,
    depth: int,
    directory: Path,
    platform: list[Platform],
    verbose: bool,
    only_global: bool,
    check_input_hash: bool,
) -> None:
    """Generate a conda-lock file a collection of requirements.yaml files."""
    conda_lock_output = _conda_lock_global(
        depth=depth,
        directory=directory,
        platform=platform,
        verbose=verbose,
        check_input_hash=check_input_hash,
    )
    if only_global:
        return
    sub_lock_files = _conda_lock_subpackages(
        directory=directory,
        depth=depth,
        conda_lock_file=conda_lock_output,
    )
    mismatches = _check_consistent_lock_files(
        global_lock_file=conda_lock_output,
        sub_lock_files=sub_lock_files,
    )
    if not mismatches:
        print("✅ Analyzed all lock files and found no inconsistencies.")
    elif len(mismatches) > 1:  # pragma: no cover
        print("❌ Complete table of package version mismatches:")
        _mismatch_report(mismatches, raises=False)


class Mismatch(NamedTuple):
    name: str
    version: str
    version_global: str
    platform: Platform
    lock_file: Path
    which: CondaPip


def _check_consistent_lock_files(
    global_lock_file: Path,
    sub_lock_files: list[Path],
) -> list[Mismatch]:
    yaml = YAML(typ="safe")
    with global_lock_file.open() as fp:
        global_data = yaml.load(fp)

    global_packages: dict[str, dict[Platform, dict[CondaPip, str]]] = defaultdict(
        lambda: defaultdict(dict),
    )
    for p in global_data["package"]:
        global_packages[p["name"]][p["platform"]][p["manager"]] = p["version"]

    mismatched_packages = []
    for lock_file in sub_lock_files:
        with lock_file.open() as fp:
            data = yaml.load(fp)

        for p in data["package"]:
            name = p["name"]
            platform = p["platform"]
            version = p["version"]
            which = p["manager"]
            if global_packages.get(name, {}).get(platform, {}).get(which) == version:
                continue

            global_version = global_packages[name][platform][which]
            if global_version != version:
                mismatched_packages.append(
                    Mismatch(
                        name=name,
                        version=version,
                        version_global=global_version,
                        platform=platform,
                        lock_file=lock_file,
                        which=which,
                    ),
                )
    return mismatched_packages


def _format_table_row(
    row: list[str],
    widths: list[int],
    seperator: str = " | ",
) -> str:  # pragma: no cover
    """Format a row of the table with specified column widths."""
    return seperator.join(f"{cell:<{widths[i]}}" for i, cell in enumerate(row))


def _mismatch_report(
    mismatched_packages: list[Mismatch],
    *,
    raises: bool = False,
) -> None:  # pragma: no cover
    if not mismatched_packages:
        return

    headers = [
        "Subpackage",
        "Manager",
        "Package",
        "Version (Sub)",
        "Version (Global)",
        "Platform",
    ]

    def _to_seq(m: Mismatch) -> list[str]:
        return [
            m.lock_file.parent.name,
            m.which,
            m.name,
            m.version,
            m.version_global,
            str(m.platform),
        ]

    column_widths = [len(header) for header in headers]
    for m in mismatched_packages:
        attrs = _to_seq(m)
        for i, attr in enumerate(attrs):
            column_widths[i] = max(column_widths[i], len(attr))

    # Create the table rows
    separator_line = [w * "-" for w in column_widths]
    table_rows = [
        _format_table_row(separator_line, column_widths, seperator="-+-"),
        _format_table_row(headers, column_widths),
        _format_table_row(["-" * width for width in column_widths], column_widths),
    ]
    for m in mismatched_packages:
        row = _to_seq(m)
        table_rows.append(_format_table_row(row, column_widths))
    table_rows.append(_format_table_row(separator_line, column_widths, seperator="-+-"))

    table = "\n".join(table_rows)

    full_error_message = (
        "Version mismatches found between global and subpackage lock files:\n"
        + table
        + "\n\n‼️ You might want to pin some versions stricter"
        " in your `requirements.yaml` files."
    )

    if raises:
        raise RuntimeError(full_error_message)
    warnings.warn(full_error_message, stacklevel=2)


def _check_conda_prefix() -> None:  # pragma: no cover
    """Check if sys.executable is in the $CONDA_PREFIX."""
    if "CONDA_PREFIX" not in os.environ:
        return
    conda_prefix = os.environ["CONDA_PREFIX"]
    if sys.executable.startswith(str(conda_prefix)):
        return
    msg = (
        "UniDep should be run from the current Conda environment for correct"
        " operation. However, it's currently running with the Python interpreter"
        f" at `{sys.executable}`, which is not in the active Conda environment"
        f" (`{conda_prefix}`). Please install and run UniDep in the current"
        " Conda environment to avoid any issues."
    )
    warnings.warn(msg, stacklevel=2)
    sys.exit(1)


def main() -> None:
    """Main entry point for the command-line tool."""
    args = _parse_args()
    if "file" in args and not args.file.exists():  # pragma: no cover
        print(f"❌ File {args.file} not found.")
        sys.exit(1)

    if "platform" in args and args.platform is None:  # pragma: no cover
        args.platform = [_identify_current_platform()]

    if args.command == "merge":  # pragma: no cover
        _merge_command(
            depth=args.depth,
            directory=args.directory,
            name=args.name,
            output=args.output,
            stdout=args.stdout,
            selector=args.selector,
            platforms=args.platform,
            verbose=args.verbose,
        )
    elif args.command == "pip":  # pragma: no cover
        pip_dependencies = list(
            get_python_dependencies(
                args.file,
                platforms=[args.platform],
                verbose=args.verbose,
            ),
        )
        print(_escape_unicode(args.separator).join(pip_dependencies))
    elif args.command == "conda":  # pragma: no cover
        requirements = parse_yaml_requirements(args.file, verbose=args.verbose)
        resolved_requirements = resolve_conflicts(requirements.requirements)
        env_spec = create_conda_env_specification(
            resolved_requirements,
            requirements.channels,
            platforms=[args.platform],
        )
        print(_escape_unicode(args.separator).join(env_spec.conda))  # type: ignore[arg-type]
    elif args.command == "install":
        _check_conda_prefix()
        _install_command(
            conda_executable=args.conda_executable,
            dry_run=args.dry_run,
            editable=args.editable,
            file=args.file,
            skip_local=args.skip_local,
            skip_pip=args.skip_pip,
            skip_conda=args.skip_conda,
            verbose=args.verbose,
        )
    elif args.command == "conda-lock":  # pragma: no cover
        _conda_lock_command(
            depth=args.depth,
            directory=args.directory,
            platform=args.platform,
            verbose=args.verbose,
            only_global=args.only_global,
            check_input_hash=args.check_input_hash,
        )
    elif args.command == "version":  # pragma: no cover
        txt = (
            f"unidep version: {__version__}",
            f"unidep location: {__file__}",
            f"Python version: {sys.version}",
            f"Python executable: {sys.executable}",
        )
        print("\n".join(txt))


# pytest plugin (in beta and undocumented ATM)


def pytest_addoption(parser: pytest.Parser) -> None:  # pragma: no cover
    parser.addoption(
        "--run-affected",
        action="store_true",
        default=False,
        help="Run only tests from affected packages",
    )
    parser.addoption(
        "--branch",
        action="store",
        default="origin/main",
        help="Branch to compare with for finding affected tests",
    )
    parser.addoption(
        "--repo-root",
        action="store",
        default=".",
        type=Path,
        help="Root of the repository",
    )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:  # pragma: no cover
    if not config.getoption("--run-affected"):
        return
    try:
        from git import Repo
    except ImportError:
        print(
            "🛑 You need to install `gitpython` to use the `--run-affected` option."
            "run `pip install gitpython` to install it.",
        )
        sys.exit(1)

    compare_branch = config.getoption("--branch")
    repo_root = Path(config.getoption("--repo-root")).absolute()
    repo = Repo(repo_root)
    files = find_requirements_files(repo_root)
    dependencies = parse_project_dependencies(*files)
    diffs = repo.head.commit.diff(compare_branch)
    changed_files = [Path(diff.a_path) for diff in diffs]
    affected_packages = _affected_packages(repo_root, changed_files, dependencies)
    affected_tests = {
        item
        for item in items
        if any(item.nodeid.startswith(str(pkg)) for pkg in affected_packages)
    }
    items[:] = list(affected_tests)


def _file_in_folder(file: Path, folder: Path) -> bool:  # pragma: no cover
    file = file.absolute()
    folder = folder.absolute()
    common = os.path.commonpath([folder, file])
    return os.path.commonpath([folder]) == common


def _affected_packages(
    repo_root: Path,
    changed_files: list[Path],
    dependencies: dict[Path, list[Path]],
    *,
    verbose: bool = False,
) -> set[Path]:  # pragma: no cover
    affected_packages = set()
    for file in changed_files:
        for package, deps in dependencies.items():
            if _file_in_folder(repo_root / file, package):
                if verbose:
                    print(f"File {file} affects package {package}")
                affected_packages.add(package)
                affected_packages.update(deps)
    return {pkg.relative_to(repo_root) for pkg in affected_packages}


if __name__ == "__main__":
    main()
