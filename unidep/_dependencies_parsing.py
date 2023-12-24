"""unidep - Unified Conda and Pip requirements management.

This module provides YAML parsing for `requirements.yaml` files.
"""

from __future__ import annotations

import hashlib
import sys
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from unidep.platform_definitions import Platform, Spec, platforms_from_selector

if TYPE_CHECKING:
    if sys.version_info >= (3, 8):
        from typing import Literal
    else:  # pragma: no cover
        from typing_extensions import Literal

from unidep.utils import (
    is_pip_installable,
    parse_package_str,
    selector_from_comment,
)

try:  # pragma: no cover
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib
    HAS_TOML = True
except ImportError:  # pragma: no cover
    HAS_TOML = False


def find_requirements_files(
    base_dir: str | Path = ".",
    depth: int = 1,
    *,
    verbose: bool = False,
) -> list[Path]:
    """Scan a directory for requirements.yaml files."""
    filename_yaml = "requirements.yaml"
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
            elif child.name == filename_yaml:
                found_files.append(child)
                if verbose:
                    print(f"🔍 Found `{filename_yaml}` at `{child}`")
            elif child.name == "pyproject.toml" and any(
                line.lstrip().startswith("[tool.unidep")
                for line in child.read_text().splitlines()
                # TODO[Bas]: will fail if defining dict in  # noqa: TD004, TD003, FIX002, E501
                # pyproject.toml directly e.g., it contains:
                # `tool = {unidep = {dependencies = ...}}`
            ):
                found_files.append(child)

    _scan_dir(base_path, 0)
    return sorted(found_files)


def _extract_first_comment(
    commented_map: CommentedMap,
    index_or_key: int | str,
) -> str | None:
    """Extract the first comment from a CommentedMap."""
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


def _identifier(identifier: int, selector: str | None) -> str:
    """Return a unique identifier based on the comment."""
    platforms = None if selector is None else tuple(platforms_from_selector(selector))
    data_str = f"{identifier}-{platforms}"
    # Hash using SHA256 and take the first 8 characters for a shorter hash
    return hashlib.sha256(data_str.encode()).hexdigest()[:8]


def _parse_dependency(
    dependency: str,
    dependencies: CommentedMap,
    index_or_key: int | str,
    which: Literal["conda", "pip", "both"],
    identifier: int,
    ignore_pins: list[str],
    overwrite_pins: dict[str, str | None],
    skip_dependencies: list[str],
) -> list[Spec]:
    name, pin, selector = parse_package_str(dependency)
    if name in ignore_pins:
        pin = None
    if name in skip_dependencies:
        return []
    if name in overwrite_pins:
        pin = overwrite_pins[name]
    comment = (
        _extract_first_comment(dependencies, index_or_key)
        if isinstance(dependencies, (CommentedMap, CommentedSeq))
        else None
    )
    if comment and selector is None:
        selector = selector_from_comment(comment)
    identifier_hash = _identifier(identifier, selector)
    if which == "both":
        return [
            Spec(name, "conda", pin, identifier_hash, selector),
            Spec(name, "pip", pin, identifier_hash, selector),
        ]
    return [Spec(name, which, pin, identifier_hash, selector)]


class ParsedRequirements(NamedTuple):
    """Requirements with comments."""

    channels: list[str]
    platforms: list[Platform]
    requirements: dict[str, list[Spec]]


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


def _parse_overwrite_pins(overwrite_pins: list[str]) -> dict[str, str | None]:
    """Parse overwrite pins."""
    result = {}
    for overwrite_pin in overwrite_pins:
        pkg = parse_package_str(overwrite_pin)
        result[pkg.name] = pkg.pin
    return result


def _load(p: Path, yaml: YAML) -> dict[str, Any]:
    if p.suffix == ".toml":
        if not HAS_TOML:  # pragma: no cover
            msg = (
                "❌ No toml support found in your Python installation."
                " If you are using unidep from `pyproject.toml` and this"
                " error occurs during installation, make sure you add"
                '\n\n[build-system]\nrequires = [..., "unidep[toml]"]\n\n'
                " Otherwise, please install it with `pip install tomli`."
            )
            raise ImportError(msg)
        with p.open("rb") as f:
            return tomllib.load(f)["tool"]["unidep"]
    with p.open() as f:
        return yaml.load(f)


def parse_requirements(  # noqa: PLR0912
    *paths: Path,
    ignore_pins: list[str] | None = None,
    overwrite_pins: list[str] | None = None,
    skip_dependencies: list[str] | None = None,
    verbose: bool = False,
) -> ParsedRequirements:
    """Parse a list of `requirements.yaml` or `pyproject.toml` files."""
    ignore_pins = ignore_pins or []
    skip_dependencies = skip_dependencies or []
    overwrite_pins_map = _parse_overwrite_pins(overwrite_pins or [])
    requirements: dict[str, list[Spec]] = defaultdict(list)
    channels: set[str] = set()
    platforms: set[Platform] = set()
    datas = []
    seen: set[Path] = set()
    yaml = YAML(typ="rt")
    for p in paths:
        if verbose:
            print(f"📄 Parsing `{p}`")
        data = _load(p, yaml)
        datas.append(data)
        seen.add(p.resolve())

        # Handle includes
        for include in data.get("includes", []):
            include_path = _include_path(p.parent / include)
            if include_path in seen:
                continue  # Avoids circular includes
            if verbose:
                print(f"📄 Parsing include `{include}`")
            datas.append(_load(include_path, yaml))
            seen.add(include_path)

    identifier = -1
    for data in datas:
        for channel in data.get("channels", []):
            channels.add(channel)
        for _platform in data.get("platforms", []):
            platforms.add(_platform)
        if "dependencies" not in data:
            continue
        dependencies = data["dependencies"]
        for i, dep in enumerate(data["dependencies"]):
            identifier += 1
            if isinstance(dep, str):
                specs = _parse_dependency(
                    dep,
                    dependencies,
                    i,
                    "both",
                    identifier,
                    ignore_pins,
                    overwrite_pins_map,
                    skip_dependencies,
                )
                for spec in specs:
                    requirements[spec.name].append(spec)
                continue
            assert isinstance(dep, dict)
            for which in ["conda", "pip"]:
                if which in dep:
                    specs = _parse_dependency(
                        dep[which],
                        dep,
                        which,
                        which,  # type: ignore[arg-type]
                        identifier,
                        ignore_pins,
                        overwrite_pins_map,
                        skip_dependencies,
                    )
                    for spec in specs:
                        requirements[spec.name].append(spec)

    return ParsedRequirements(sorted(channels), sorted(platforms), dict(requirements))


# Alias for backwards compatibility
parse_yaml_requirements = parse_requirements


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
    data = _load(path, yaml)
    for include in data.get("includes", []):
        include_path = _include_path(path.parent / include)
        if not include_path.exists():
            msg = f"Include file `{include_path}` does not exist."
            raise FileNotFoundError(msg)
        include_base_path = str(include_path.parent)
        if include_base_path == str(base_path):
            continue
        if not check_pip_installable or (
            is_pip_installable(base_path) and is_pip_installable(include_path.parent)
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


def yaml_to_toml(yaml_path: Path) -> str:
    """Converts a `requirements.yaml` file TOML format."""
    try:
        import tomli_w
    except ImportError:  # pragma: no cover
        msg = (
            "❌ `tomli_w` is required to convert YAML to TOML."
            " Install it with `pip install tomli_w`."
        )
        raise ImportError(msg) from None
    yaml = YAML(typ="rt")
    data = _load(yaml_path, yaml)
    data.pop("name", None)
    dependencies = data.get("dependencies", [])
    for i, dep in enumerate(dependencies):
        if isinstance(dep, str):
            comment = _extract_first_comment(dependencies, i)
            if comment is not None:
                selector = selector_from_comment(comment)
                if selector is not None:
                    dependencies[i] = f"{dep}:{selector}"
            continue
        assert isinstance(dep, dict)
        for which in ["conda", "pip"]:
            if which in dep:
                comment = _extract_first_comment(dep, which)
                if comment is not None:
                    selector = selector_from_comment(comment)
                    if selector is not None:
                        dep[which] = f"{dep[which]}:{selector}"

    return tomli_w.dumps({"tool": {"unidep": data}})
