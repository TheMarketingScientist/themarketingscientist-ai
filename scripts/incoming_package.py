"""Create and safely import incoming article packages without running Git or Quarto."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

import yaml


IMAGE_EXTENSIONS = {".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
PLACEHOLDER_PATTERN = re.compile(rb"\{\{image:([a-z0-9-]+)\}\}")
ROLE_PATTERN = re.compile(r"^(?:featured|figure-[1-9][0-9]*)$")
WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


@dataclass
class PackageReport:
    """Machine-readable result consumed by the PowerShell entry point."""

    status: str = "passed"
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    slug: str | None = None
    incoming_folder: str | None = None
    article_path: str | None = None
    image_mapping: list[dict[str, Any]] = field(default_factory=list)
    proposed_mapping: list[dict[str, Any]] = field(default_factory=list)
    mapping_approval_token: str | None = None
    package_yml_changes: str | None = None
    imported_files: list[dict[str, Any]] = field(default_factory=list)
    collisions: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        """Record a hard failure."""

        self.status = "failed"
        self.errors.append(message)

    def ambiguous(self, message: str) -> None:
        """Record a mapping decision that needs one explicit approval."""

        self.status = "ambiguous"
        self.errors.append(message)

    def as_dict(self) -> dict[str, Any]:
        """Return stable JSON fields for every command outcome."""

        return asdict(self)


@dataclass(frozen=True)
class PackageContents:
    """Validated incoming package metadata and immutable source inputs."""

    root: pathlib.Path
    title: str
    slug: str
    article: pathlib.Path
    article_bytes: bytes
    image_candidates: tuple[pathlib.Path, ...]
    explicit_images: dict[str, str | None]
    roles: tuple[str, ...]


def safe_slug(title: str) -> str:
    """Convert an article title to a portable lowercase ASCII slug."""

    if not isinstance(title, str) or not title.strip():
        raise ValueError("Article title must be a non-empty string.")
    normalized = unicodedata.normalize("NFKD", title.strip())
    ascii_title = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title).strip("-")
    slug = slug[:80].rstrip("-")
    if not slug:
        raise ValueError("Article title does not contain characters that can form a safe slug.")
    if slug in WINDOWS_RESERVED_NAMES:
        slug = f"article-{slug}"
    return slug


def sha256_bytes(value: bytes) -> str:
    """Return a lowercase SHA-256 digest for bytes."""

    return hashlib.sha256(value).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    """Return a lowercase SHA-256 digest without loading a file all at once."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def relative_display(path: pathlib.Path, repo_root: pathlib.Path) -> str:
    """Return a forward-slash repository path when possible."""

    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def package_yaml_text(title: str, slug: str) -> str:
    """Create the intentionally small starter manifest."""

    quoted_title = json.dumps(title, ensure_ascii=False)
    return (
        f"title: {quoted_title}\n"
        f"slug: {slug}\n"
        "article: article.qmd\n"
        "images:\n"
        "  featured: null\n"
        "  figure-1: null\n"
        "  figure-2: null\n"
    )


def create_package(repo_root: pathlib.Path, title: str) -> PackageReport:
    """Create one incoming package folder and manifest; never invoke Git or Quarto."""

    report = PackageReport()
    try:
        root = repo_root.resolve(strict=True)
        slug = safe_slug(title)
    except (OSError, ValueError) as error:
        report.fail(str(error))
        return report

    incoming_root = root / "incoming"
    package_root = incoming_root / slug
    report.slug = slug
    report.incoming_folder = relative_display(package_root, root)
    if package_root.exists() or package_root.is_symlink():
        report.fail(f"Incoming package already exists: {report.incoming_folder}")
        return report

    try:
        incoming_root.mkdir(parents=True, exist_ok=True)
        if incoming_root.resolve().parent != root:
            raise ValueError("The incoming directory resolves outside the repository root.")
        package_root.mkdir(exist_ok=False)
        manifest = package_root / "package.yml"
        manifest.write_text(package_yaml_text(title.strip(), slug), encoding="utf-8", newline="")
    except (OSError, ValueError) as error:
        if package_root.exists() and not any(package_root.iterdir()):
            package_root.rmdir()
        report.fail(f"Could not create incoming package: {error}")
        return report

    report.imported_files.append(
        {
            "source_path": None,
            "destination_path": relative_display(manifest, root),
            "sha256": sha256_file(manifest),
        }
    )
    return report


def ordered_roles(roles: set[str]) -> tuple[str, ...]:
    """Order featured first and numbered figures numerically."""

    def key(role: str) -> tuple[int, int]:
        if role == "featured":
            return (0, 0)
        return (1, int(role.split("-", 1)[1]))

    return tuple(sorted(roles, key=key))


def _safe_direct_child(root: pathlib.Path, value: str) -> pathlib.Path | None:
    """Resolve a manifest filename only when it names a direct package child."""

    candidate = pathlib.Path(value)
    if candidate.is_absolute() or candidate.name != value or value in {".", ".."}:
        return None
    resolved = (root / candidate).resolve()
    if resolved.parent != root.resolve():
        return None
    return resolved


def load_package(
    repo_root: pathlib.Path,
    incoming_folder: pathlib.Path,
    report: PackageReport,
) -> PackageContents | None:
    """Read and validate a direct child of incoming/ without changing it."""

    root = repo_root.resolve()
    incoming_root = (root / "incoming").resolve()
    package_root = incoming_folder.resolve()
    report.incoming_folder = relative_display(package_root, root)
    if package_root.parent != incoming_root:
        report.fail("IncomingFolder must be one direct child under incoming/.")
        return None
    if not package_root.is_dir() or package_root.is_symlink():
        report.fail(f"Incoming package folder does not exist or is unsafe: {package_root}")
        return None

    manifest = package_root / "package.yml"
    if not manifest.is_file() or manifest.is_symlink():
        report.fail("Incoming package must contain a regular package.yml file.")
        return None
    try:
        metadata = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        report.fail(f"Could not parse package.yml: {error}")
        return None
    if not isinstance(metadata, dict):
        report.fail("package.yml must contain a YAML mapping.")
        return None

    title = metadata.get("title")
    slug = metadata.get("slug")
    article_name = metadata.get("article")
    image_config = metadata.get("images")
    if not isinstance(title, str) or not title.strip():
        report.fail("package.yml title must be a non-empty string.")
    if not isinstance(slug, str) or not slug.strip():
        report.fail("package.yml slug must be a non-empty string.")
    else:
        try:
            slug_is_safe = safe_slug(slug) == slug
        except ValueError:
            slug_is_safe = False
        if not slug_is_safe or package_root.name != slug:
            report.fail("package.yml slug must be safe and match the incoming folder name.")
    if not isinstance(article_name, str) or not article_name.strip():
        report.fail("package.yml article must name the package QMD file.")
    if not isinstance(image_config, dict):
        report.fail("package.yml images must be a YAML mapping.")
    if report.errors:
        return None

    qmd_files = sorted(
        (
            path
            for path in package_root.iterdir()
            if path.is_file() and not path.is_symlink() and path.suffix.casefold() == ".qmd"
        ),
        key=lambda path: path.name.casefold(),
    )
    if len(qmd_files) != 1:
        report.fail(f"Incoming package must contain exactly one QMD; found {len(qmd_files)}.")
        return None
    configured_article = _safe_direct_child(package_root, article_name)
    only_article = qmd_files[0].resolve()
    if configured_article is None:
        report.fail("package.yml article must identify the package's only QMD file.")
        return None
    if configured_article != only_article:
        if article_name == "article.qmd" and not configured_article.exists():
            report.warnings.append(
                f"Using the package's only QMD ({only_article.name}) instead of the starter "
                "article.qmd name."
            )
        else:
            report.fail("package.yml article must identify the package's only QMD file.")
            return None

    image_candidates = tuple(
        sorted(
            (
                path.resolve()
                for path in package_root.iterdir()
                if path.is_file()
                and not path.is_symlink()
                and path.suffix.casefold() in IMAGE_EXTENSIONS
            ),
            key=lambda path: (path.name.casefold(), path.name),
        )
    )
    if not image_candidates:
        report.fail("Incoming package must contain at least one supported image file.")
        return None

    try:
        article_bytes = only_article.read_bytes()
        article_bytes.decode("utf-8")
    except (OSError, UnicodeError) as error:
        report.fail(f"Incoming QMD must be readable UTF-8: {error}")
        return None

    raw_placeholders = re.findall(rb"\{\{image:([^}]*)\}\}", article_bytes)
    roles = {match.group(1).decode("ascii") for match in PLACEHOLDER_PATTERN.finditer(article_bytes)}
    if len(raw_placeholders) != len(list(PLACEHOLDER_PATTERN.finditer(article_bytes))):
        report.fail("Incoming QMD contains an invalid semantic image placeholder.")
        return None
    invalid_roles = sorted(role for role in roles if not ROLE_PATTERN.fullmatch(role))
    if invalid_roles:
        report.fail(f"Unsupported image roles: {', '.join(invalid_roles)}")
        return None
    if "featured" not in roles:
        report.fail("Incoming QMD must use {{image:featured}} for its YAML featured image.")
        return None
    figure_numbers = sorted(int(role.split("-", 1)[1]) for role in roles if role != "featured")
    if figure_numbers and figure_numbers != list(range(1, figure_numbers[-1] + 1)):
        report.fail("Figure placeholders must be sequential starting at figure-1.")
        return None

    explicit_images: dict[str, str | None] = {}
    for role, value in image_config.items():
        if not isinstance(role, str) or not ROLE_PATTERN.fullmatch(role):
            report.fail(f"package.yml contains an unsupported image role: {role!r}")
            continue
        if value is not None and not isinstance(value, str):
            report.fail(f"package.yml image assignment for {role} must be a filename or null.")
            continue
        explicit_images[role] = value.strip() if isinstance(value, str) and value.strip() else None
        if role not in roles and explicit_images[role]:
            report.warnings.append(f"Ignoring assignment for unused role {role}.")
    if report.errors:
        return None

    report.slug = slug
    return PackageContents(
        root=package_root,
        title=title.strip(),
        slug=slug,
        article=only_article,
        article_bytes=article_bytes,
        image_candidates=image_candidates,
        explicit_images=explicit_images,
        roles=ordered_roles(roles),
    )


def filename_hint_score(role: str, path: pathlib.Path, only_figure: bool) -> int:
    """Score conservative filename evidence for one semantic role."""

    stem = unicodedata.normalize("NFKD", path.stem).encode("ascii", "ignore").decode("ascii")
    stem = stem.casefold()
    tokens = set(filter(None, re.split(r"[^a-z0-9]+", stem)))
    if role == "featured":
        if tokens.intersection({"featured", "hero", "thumbnail", "thumb", "cover", "banner"}):
            return 100
        return 0

    number = int(role.split("-", 1)[1])
    numbered = re.compile(rf"(?:^|[^a-z0-9])(?:figure|fig|chart|diagram)[-_ ]*0*{number}(?:[^0-9]|$)")
    if numbered.search(stem):
        return 100
    if only_figure and tokens.intersection({"figure", "fig", "chart", "diagram", "plot", "graph"}):
        return 40
    return 0


def read_image_dimensions(path: pathlib.Path) -> tuple[int, int] | None:
    """Read common raster dimensions when available; mapping does not depend on Pillow."""

    try:
        from PIL import Image  # type: ignore[import-not-found]

        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except (ImportError, OSError, ValueError):
        pass

    try:
        data = path.read_bytes()
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if data[:3] in {b"GIF", b"gif"} and len(data) >= 10:
            return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    except OSError:
        return None
    return None


def assign_images(
    contents: PackageContents,
    report: PackageReport,
    *,
    approve_mapping: str | None,
) -> dict[str, dict[str, Any]] | None:
    """Resolve roles using explicit, filename, dimension, then deterministic evidence."""

    candidates_by_name = {path.name.casefold(): path for path in contents.image_candidates}
    mapping: dict[str, dict[str, Any]] = {}
    used: set[pathlib.Path] = set()

    for role in contents.roles:
        explicit = contents.explicit_images.get(role)
        if not explicit:
            continue
        source = _safe_direct_child(contents.root, explicit)
        if source is None or source.name.casefold() not in candidates_by_name:
            report.fail(f"Explicit image for {role} is not a package image: {explicit}")
            return None
        source = candidates_by_name[source.name.casefold()]
        mapping[role] = {"role": role, "source": source, "evidence": "package.yml"}
        used.add(source)

    while True:
        unresolved = [role for role in contents.roles if role not in mapping]
        available = [path for path in contents.image_candidates if path not in used]
        proposals: dict[str, pathlib.Path] = {}
        for role in unresolved:
            scores = [
                (filename_hint_score(role, path, len([r for r in unresolved if r != "featured"]) == 1), path)
                for path in available
            ]
            best_score = max((score for score, _ in scores), default=0)
            best = [path for score, path in scores if score == best_score and score > 0]
            if len(best) == 1:
                proposals[role] = best[0]
        claimed = {path for path in proposals.values() if list(proposals.values()).count(path) == 1}
        accepted = [(role, path) for role, path in proposals.items() if path in claimed]
        if not accepted:
            break
        for role, path in accepted:
            mapping[role] = {"role": role, "source": path, "evidence": "filename hint"}
            used.add(path)

    if "featured" not in mapping:
        available = [path for path in contents.image_candidates if path not in used]
        ratios: list[tuple[float, pathlib.Path, tuple[int, int]]] = []
        for path in available:
            dimensions = read_image_dimensions(path)
            if dimensions and dimensions[0] > 0 and dimensions[1] > 0:
                ratios.append((dimensions[0] / dimensions[1], path, dimensions))
        ratios.sort(key=lambda item: item[0], reverse=True)
        if ratios and ratios[0][0] >= 1.4:
            next_ratio = ratios[1][0] if len(ratios) > 1 else 0.0
            if ratios[0][0] - next_ratio >= 0.35:
                ratio, path, dimensions = ratios[0]
                mapping["featured"] = {
                    "role": "featured",
                    "source": path,
                    "evidence": f"unique landscape dimensions {dimensions[0]}x{dimensions[1]}",
                }
                used.add(path)

    unresolved = [role for role in contents.roles if role not in mapping]
    available = [path for path in contents.image_candidates if path not in used]
    if len(unresolved) == 1 and len(available) == 1:
        role = unresolved[0]
        mapping[role] = {"role": role, "source": available[0], "evidence": "only remaining image"}
        used.add(available[0])
    elif unresolved and all(role.startswith("figure-") for role in unresolved) and len(unresolved) == len(available):
        for role, path in zip(unresolved, available, strict=True):
            mapping[role] = {"role": role, "source": path, "evidence": "deterministic file order"}
            used.add(path)

    unresolved = [role for role in contents.roles if role not in mapping]
    available = [path for path in contents.image_candidates if path not in used]
    if unresolved:
        if len(available) < len(unresolved):
            report.fail(
                f"Package has {len(available)} unassigned image(s) for {len(unresolved)} unresolved role(s)."
            )
            return None
        proposal = dict(mapping)
        for role, path in zip(unresolved, available, strict=False):
            proposal[role] = {"role": role, "source": path, "evidence": "proposed deterministic order"}
        report.proposed_mapping = mapping_records(proposal, contents.slug)
        report.mapping_approval_token = mapping_approval_token(contents, proposal)
        report.package_yml_changes = image_assignment_yaml(proposal)
        if approve_mapping is None:
            report.ambiguous(
                "Image mapping is ambiguous. Review the single proposed mapping and approve it "
                "explicitly or record the assignments in package.yml."
            )
            return None
        if approve_mapping != report.mapping_approval_token:
            report.fail(
                "The image-mapping approval token does not match the current QMD and image files. "
                "Run the package again and review the updated consolidated table."
            )
            return None
        mapping = proposal
        for record in mapping.values():
            if record["evidence"] == "proposed deterministic order":
                record["evidence"] = "explicit -ApproveImageMapping"
        used.update(record["source"] for record in mapping.values())

    unused = [path.name for path in contents.image_candidates if path not in used]
    if unused:
        report.warnings.append(f"Unused package image(s): {', '.join(unused)}")
    return mapping


def destination_name(slug: str, role: str, source: pathlib.Path) -> str:
    """Return the safe article-specific filename for one role."""

    suffix = source.suffix.casefold()
    if role == "featured":
        return f"{slug}-thumbnail{suffix}"
    return f"{slug}-{role}{suffix}"


def mapping_records(mapping: dict[str, dict[str, Any]], slug: str) -> list[dict[str, Any]]:
    """Convert internal Paths to stable display records."""

    records = []
    for role in ordered_roles(set(mapping)):
        record = mapping[role]
        records.append(
            {
                "role": role,
                "source": record["source"].name,
                "destination": destination_name(slug, role, record["source"]),
                "evidence": record["evidence"],
            }
        )
    return records


def image_assignment_yaml(mapping: dict[str, dict[str, Any]]) -> str:
    """Return an exact package.yml images block that approves a proposal."""

    lines = ["images:"]
    for role in ordered_roles(set(mapping)):
        filename = json.dumps(mapping[role]["source"].name, ensure_ascii=False)
        lines.append(f"  {role}: {filename}")
    return "\n".join(lines)


def mapping_approval_token(
    contents: PackageContents,
    mapping: dict[str, dict[str, Any]],
) -> str:
    """Bind one mapping approval to the current article and proposed image bytes."""

    payload = {
        "slug": contents.slug,
        "article_sha256": sha256_bytes(contents.article_bytes),
        "images": [
            {
                "role": role,
                "filename": mapping[role]["source"].name,
                "sha256": sha256_file(mapping[role]["source"]),
            }
            for role in ordered_roles(set(mapping))
        ],
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(serialized.encode("utf-8"))


def transformed_article_bytes(
    article_bytes: bytes,
    mapping: dict[str, dict[str, Any]],
    slug: str,
) -> bytes:
    """Replace only exact semantic placeholder bytes."""

    transformed = article_bytes
    for role in ordered_roles(set(mapping)):
        placeholder = f"{{{{image:{role}}}}}".encode("ascii")
        replacement = f"../images/{destination_name(slug, role, mapping[role]['source'])}".encode("utf-8")
        transformed = transformed.replace(placeholder, replacement)
    if b"{{image:" in transformed:
        raise ValueError("One or more semantic image placeholders remained after replacement.")
    return transformed


def _write_temp_bytes(destination: pathlib.Path, value: bytes) -> pathlib.Path:
    """Write verified staging bytes beside a destination for atomic replacement."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".incoming-", suffix=".tmp", dir=destination.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def install_files_atomically(payloads: dict[pathlib.Path, bytes]) -> None:
    """Stage, hash-check, install, and roll back a set of destination files."""

    temporary_files: dict[pathlib.Path, pathlib.Path] = {}
    originals: dict[pathlib.Path, bytes | None] = {}
    installed: list[pathlib.Path] = []
    try:
        for destination, value in payloads.items():
            temporary = _write_temp_bytes(destination, value)
            if sha256_file(temporary) != sha256_bytes(value):
                raise OSError(f"SHA-256 mismatch while staging {destination}")
            temporary_files[destination] = temporary
            originals[destination] = destination.read_bytes() if destination.exists() else None
        for destination, temporary in temporary_files.items():
            os.replace(temporary, destination)
            installed.append(destination)
        for destination, value in payloads.items():
            if sha256_file(destination) != sha256_bytes(value):
                raise OSError(f"SHA-256 mismatch after importing {destination}")
    except Exception:
        for destination in reversed(installed):
            original = originals[destination]
            if original is None:
                destination.unlink(missing_ok=True)
            else:
                rollback = _write_temp_bytes(destination, original)
                os.replace(rollback, destination)
        raise
    finally:
        for temporary in temporary_files.values():
            temporary.unlink(missing_ok=True)


def import_package(
    repo_root: pathlib.Path,
    incoming_folder: pathlib.Path,
    *,
    approve_mapping: str | None = None,
    approve_collisions: bool = False,
) -> PackageReport:
    """Map and import one package without invoking validation, rendering, or Git."""

    report = PackageReport()
    root = repo_root.resolve()
    contents = load_package(root, incoming_folder, report)
    if contents is None:
        return report
    mapping = assign_images(contents, report, approve_mapping=approve_mapping)
    if mapping is None:
        return report

    report.image_mapping = mapping_records(mapping, contents.slug)
    articles_root = (root / "articles").resolve()
    images_root = (root / "images").resolve()
    if articles_root.parent != root or images_root.parent != root:
        report.fail("Article or image destination resolves outside the repository root.")
        return report
    article_destination = articles_root / f"{contents.slug}.qmd"
    image_destinations = {
        role: images_root / destination_name(contents.slug, role, record["source"])
        for role, record in mapping.items()
    }
    all_destinations = [article_destination, *image_destinations.values()]
    unsafe_links = [destination for destination in all_destinations if destination.is_symlink()]
    if unsafe_links:
        report.fail(f"Refusing symbolic-link destination(s): {', '.join(map(str, unsafe_links))}")
        return report
    collisions = [destination for destination in all_destinations if destination.exists()]
    report.collisions = [relative_display(path, root) for path in collisions]
    if collisions and not approve_collisions:
        report.fail(
            "Destination collision(s) require explicit -ApproveDestinationCollisions: "
            + ", ".join(report.collisions)
        )
        return report

    try:
        article_value = transformed_article_bytes(contents.article_bytes, mapping, contents.slug)
    except ValueError as error:
        report.fail(str(error))
        return report

    incoming_hashes = {
        path: sha256_file(path)
        for path in (contents.root / "package.yml", contents.article, *contents.image_candidates)
    }
    payloads = {article_destination: article_value}
    for role, destination in image_destinations.items():
        payloads[destination] = mapping[role]["source"].read_bytes()

    try:
        install_files_atomically(payloads)
        for source, original_hash in incoming_hashes.items():
            if not source.is_file() or sha256_file(source) != original_hash:
                raise OSError(f"Incoming package changed unexpectedly: {source}")
    except OSError as error:
        report.fail(f"Import failed: {error}")
        return report

    report.article_path = str(article_destination)
    report.imported_files.append(
        {
            "source_path": relative_display(contents.article, root),
            "destination_path": relative_display(article_destination, root),
            "sha256": sha256_file(article_destination),
        }
    )
    for role in contents.roles:
        source = mapping[role]["source"]
        destination = image_destinations[role]
        source_hash = sha256_file(source)
        destination_hash = sha256_file(destination)
        if source_hash != destination_hash:
            report.fail(f"SHA-256 mismatch for imported image: {destination}")
            return report
        report.imported_files.append(
            {
                "source_path": relative_display(source, root),
                "destination_path": relative_display(destination, root),
                "sha256": destination_hash,
            }
        )
    return report


def build_argument_parser() -> argparse.ArgumentParser:
    """Create the command-line interface used by publish_article.ps1."""

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--repo-root", required=True, type=pathlib.Path)
    create.add_argument("--title", required=True)
    importer = subparsers.add_parser("import")
    importer.add_argument("--repo-root", required=True, type=pathlib.Path)
    importer.add_argument("--incoming-folder", required=True, type=pathlib.Path)
    importer.add_argument("--approve-mapping")
    importer.add_argument("--approve-collisions", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Emit one JSON report; 0 passes, 2 needs mapping approval, other nonzero fails."""

    arguments = build_argument_parser().parse_args(argv)
    try:
        if arguments.command == "create":
            report = create_package(arguments.repo_root, arguments.title)
        else:
            report = import_package(
                arguments.repo_root,
                arguments.incoming_folder,
                approve_mapping=arguments.approve_mapping,
                approve_collisions=arguments.approve_collisions,
            )
    except Exception as error:  # Preserve JSON even for unexpected path or I/O failures.
        report = PackageReport(status="failed")
        report.errors.append(f"Unexpected incoming-package failure: {type(error).__name__}: {error}")
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    if report.status == "passed":
        return 0
    if report.status == "ambiguous":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
