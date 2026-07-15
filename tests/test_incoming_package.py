"""Non-destructive tests for incoming article package creation and import."""

from __future__ import annotations

import contextlib
import io
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

from scripts import incoming_package as incoming


def qmd_bytes(*roles: str, newline: bytes = b"\n") -> bytes:
    """Build a UTF-8 article using semantic image placeholders."""

    lines = [
        b"---",
        b'title: "Incoming article"',
        b"author: Andres Acosta",
        b"date: 2026-07-15",
        b"featured: true",
        b'image: "{{image:featured}}"',
        b'description: "Incoming package test"',
        b"---",
        b"Body bytes stay exactly here.",
    ]
    for role in roles:
        number = role.split("-", 1)[1]
        lines.append(
            f"![Figure {number}]({{{{image:{role}}}}}){{width=80% fig-align=\"center\"}}".encode()
        )
    return newline.join(lines) + newline


class IncomingPackageTests(unittest.TestCase):
    """Exercise package behavior entirely inside temporary repositories."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary_directory.name)
        (self.root / "articles").mkdir()
        (self.root / "images").mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def create(self, title: str = "Pricing vs CAC") -> pathlib.Path:
        report = incoming.create_package(self.root, title)
        self.assertEqual(report.status, "passed", report.errors)
        return self.root / str(report.incoming_folder)

    @staticmethod
    def write_image(package: pathlib.Path, name: str, value: bytes | None = None) -> pathlib.Path:
        path = package / name
        path.write_bytes(value if value is not None else f"image:{name}".encode("utf-8"))
        return path

    @staticmethod
    def assign(package: pathlib.Path, **assignments: str | None) -> None:
        manifest = package / "package.yml"
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        data["images"].update(assignments)
        manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    def test_package_creation_writes_starter_manifest(self) -> None:
        package = self.create()
        self.assertTrue(package.is_dir())
        self.assertEqual(
            (package / "package.yml").read_text(encoding="utf-8"),
            'title: "Pricing vs CAC"\n'
            "slug: pricing-vs-cac\n"
            "article: article.qmd\n"
            "images:\n"
            "  featured: null\n"
            "  figure-1: null\n"
            "  figure-2: null\n",
        )
        self.assertEqual(list(package.iterdir()), [package / "package.yml"])

    def test_safe_slug_generation_is_portable(self) -> None:
        self.assertEqual(incoming.safe_slug("  Pricing vs. CAC!  "), "pricing-vs-cac")
        self.assertEqual(incoming.safe_slug("Métricas & Crecimiento"), "metricas-crecimiento")
        self.assertEqual(incoming.safe_slug("CON"), "article-con")
        with self.assertRaises(ValueError):
            incoming.safe_slug("漢字")

    def test_existing_folder_collision_does_not_overwrite(self) -> None:
        package = self.create()
        manifest = package / "package.yml"
        original = manifest.read_bytes()
        second = incoming.create_package(self.root, "Pricing vs CAC")
        self.assertEqual(second.status, "failed")
        self.assertIn("already exists", second.errors[0])
        self.assertEqual(manifest.read_bytes(), original)

    def test_arbitrary_downloaded_filename_is_accepted(self) -> None:
        package = self.create()
        (package / "article.qmd").write_bytes(qmd_bytes())
        source = self.write_image(package, "DALL·E download (final) 9273.PNG")
        report = incoming.import_package(self.root, package)
        self.assertEqual(report.status, "passed", report.errors)
        destination = self.root / "images" / "pricing-vs-cac-thumbnail.png"
        self.assertEqual(destination.read_bytes(), source.read_bytes())

    def test_only_qmd_is_detected_without_manual_renaming(self) -> None:
        package = self.create()
        (package / "Pricing versus CAC final.qmd").write_bytes(qmd_bytes())
        self.write_image(package, "hero.png")
        report = incoming.import_package(self.root, package)
        self.assertEqual(report.status, "passed", report.errors)
        self.assertTrue((self.root / "articles" / "pricing-vs-cac.qmd").is_file())
        self.assertTrue(any("only QMD" in warning for warning in report.warnings))

    def test_multiple_qmd_files_are_rejected_before_import(self) -> None:
        package = self.create()
        (package / "article.qmd").write_bytes(qmd_bytes())
        (package / "second.qmd").write_bytes(qmd_bytes())
        self.write_image(package, "hero.png")
        report = incoming.import_package(self.root, package)
        self.assertEqual(report.status, "failed")
        self.assertIn("exactly one QMD", report.errors[0])
        self.assertEqual(list((self.root / "articles").iterdir()), [])

    def test_featured_image_only_maps_automatically(self) -> None:
        package = self.create()
        (package / "article.qmd").write_bytes(qmd_bytes())
        self.write_image(package, "only-image.jpg")
        report = incoming.import_package(self.root, package)
        self.assertEqual(report.status, "passed", report.errors)
        self.assertEqual(report.image_mapping[0]["role"], "featured")
        self.assertEqual(report.image_mapping[0]["evidence"], "only remaining image")

    def test_multiple_figures_use_hints_then_deterministic_order(self) -> None:
        package = self.create()
        (package / "article.qmd").write_bytes(qmd_bytes("figure-1", "figure-2"))
        self.write_image(package, "hero final.png")
        first = self.write_image(package, "download-a.png")
        second = self.write_image(package, "download-z.png")
        report = incoming.import_package(self.root, package)
        self.assertEqual(report.status, "passed", report.errors)
        mapped = {record["role"]: record for record in report.image_mapping}
        self.assertEqual(mapped["featured"]["source"], "hero final.png")
        self.assertEqual(mapped["figure-1"]["source"], first.name)
        self.assertEqual(mapped["figure-2"]["source"], second.name)

    def test_explicit_package_yml_mapping_has_priority(self) -> None:
        package = self.create()
        (package / "article.qmd").write_bytes(qmd_bytes("figure-1"))
        self.write_image(package, "random-a.png")
        self.write_image(package, "random-b.jpg")
        self.assign(package, featured="random-b.jpg", **{"figure-1": "random-a.png"})
        report = incoming.import_package(self.root, package)
        self.assertEqual(report.status, "passed", report.errors)
        mapped = {record["role"]: record for record in report.image_mapping}
        self.assertEqual(mapped["featured"]["source"], "random-b.jpg")
        self.assertEqual(mapped["figure-1"]["source"], "random-a.png")
        self.assertTrue(all(record["evidence"] == "package.yml" for record in mapped.values()))

    def test_filename_role_hints_resolve_mapping(self) -> None:
        package = self.create()
        (package / "article.qmd").write_bytes(qmd_bytes("figure-1"))
        self.write_image(package, "article THUMBNAIL.png")
        self.write_image(package, "diagram-1.jpeg")
        report = incoming.import_package(self.root, package)
        self.assertEqual(report.status, "passed", report.errors)
        mapped = {record["role"]: record["source"] for record in report.image_mapping}
        self.assertEqual(mapped, {"featured": "article THUMBNAIL.png", "figure-1": "diagram-1.jpeg"})

    def test_dimensions_can_isolate_featured_image(self) -> None:
        package = self.create()
        (package / "article.qmd").write_bytes(qmd_bytes("figure-1"))
        wide = self.write_image(package, "alpha.png")
        square = self.write_image(package, "beta.png")
        dimensions = {wide.name: (1600, 700), square.name: (800, 800)}
        with mock.patch.object(
            incoming, "read_image_dimensions", side_effect=lambda path: dimensions[path.name]
        ):
            report = incoming.import_package(self.root, package)
        self.assertEqual(report.status, "passed", report.errors)
        mapped = {record["role"]: record for record in report.image_mapping}
        self.assertEqual(mapped["featured"]["source"], "alpha.png")
        self.assertIn("landscape dimensions", mapped["featured"]["evidence"])

    def test_ambiguous_mapping_stops_once_without_importing(self) -> None:
        package = self.create()
        (package / "article.qmd").write_bytes(qmd_bytes("figure-1"))
        self.write_image(package, "alpha.png")
        self.write_image(package, "beta.png")
        report = incoming.import_package(self.root, package)
        self.assertEqual(report.status, "ambiguous")
        self.assertEqual(len(report.proposed_mapping), 2)
        self.assertIn("images:", str(report.package_yml_changes))
        self.assertEqual(list((self.root / "articles").iterdir()), [])
        self.assertEqual(list((self.root / "images").iterdir()), [])

    def test_approved_ambiguous_mapping_imports_single_proposal(self) -> None:
        package = self.create()
        (package / "article.qmd").write_bytes(qmd_bytes("figure-1"))
        self.write_image(package, "alpha.png")
        self.write_image(package, "beta.png")
        proposal = incoming.import_package(self.root, package)
        report = incoming.import_package(
            self.root, package, approve_mapping=proposal.mapping_approval_token
        )
        self.assertEqual(report.status, "passed", report.errors)
        self.assertEqual(len(report.image_mapping), 2)

    def test_changed_package_invalidates_mapping_approval(self) -> None:
        package = self.create()
        (package / "article.qmd").write_bytes(qmd_bytes("figure-1"))
        self.write_image(package, "alpha.png")
        changed = self.write_image(package, "beta.png")
        proposal = incoming.import_package(self.root, package)
        changed.write_bytes(b"changed after review")
        report = incoming.import_package(
            self.root, package, approve_mapping=proposal.mapping_approval_token
        )
        self.assertEqual(report.status, "failed")
        self.assertIn("approval token", report.errors[0])
        self.assertEqual(list((self.root / "articles").iterdir()), [])

    def test_destination_collisions_require_explicit_approval(self) -> None:
        package = self.create()
        (package / "article.qmd").write_bytes(qmd_bytes())
        self.write_image(package, "source.png", b"new-image")
        article_destination = self.root / "articles" / "pricing-vs-cac.qmd"
        image_destination = self.root / "images" / "pricing-vs-cac-thumbnail.png"
        article_destination.write_bytes(b"old-article")
        image_destination.write_bytes(b"old-image")

        refused = incoming.import_package(self.root, package)
        self.assertEqual(refused.status, "failed")
        self.assertEqual(article_destination.read_bytes(), b"old-article")
        self.assertEqual(image_destination.read_bytes(), b"old-image")

        approved = incoming.import_package(self.root, package, approve_collisions=True)
        self.assertEqual(approved.status, "passed", approved.errors)
        self.assertNotEqual(article_destination.read_bytes(), b"old-article")
        self.assertEqual(image_destination.read_bytes(), b"new-image")

    def test_placeholder_replacement_and_other_bytes_are_exact(self) -> None:
        package = self.create()
        original = qmd_bytes("figure-1", newline=b"\r\n")
        (package / "article.qmd").write_bytes(original)
        self.write_image(package, "hero.png")
        self.write_image(package, "figure-1.png")
        report = incoming.import_package(self.root, package)
        self.assertEqual(report.status, "passed", report.errors)
        expected = original.replace(
            b"{{image:featured}}", b"../images/pricing-vs-cac-thumbnail.png"
        ).replace(b"{{image:figure-1}}", b"../images/pricing-vs-cac-figure-1.png")
        imported = (self.root / "articles" / "pricing-vs-cac.qmd").read_bytes()
        self.assertEqual(imported, expected)
        self.assertIn(b"\r\n", imported)

    def test_imported_image_sha256_is_verified_and_reported(self) -> None:
        package = self.create()
        (package / "article.qmd").write_bytes(qmd_bytes())
        source = self.write_image(package, "image.webp", b"verified-image-bytes")
        report = incoming.import_package(self.root, package)
        self.assertEqual(report.status, "passed", report.errors)
        image_record = next(record for record in report.imported_files if record["source_path"].endswith(".webp"))
        destination = self.root / image_record["destination_path"]
        self.assertEqual(image_record["sha256"], incoming.sha256_file(source))
        self.assertEqual(image_record["sha256"], incoming.sha256_file(destination))

    def test_incoming_package_is_preserved_byte_for_byte(self) -> None:
        package = self.create()
        (package / "article.qmd").write_bytes(qmd_bytes())
        self.write_image(package, "input.png", b"incoming-source")
        before = {path.name: path.read_bytes() for path in package.iterdir()}
        report = incoming.import_package(self.root, package)
        self.assertEqual(report.status, "passed", report.errors)
        after = {path.name: path.read_bytes() for path in package.iterdir()}
        self.assertEqual(after, before)
        self.assertTrue(package.is_dir())

    def test_create_mode_has_no_git_or_render_activity(self) -> None:
        with (
            mock.patch.object(subprocess, "run") as run,
            mock.patch.object(subprocess, "Popen") as popen,
            mock.patch.object(subprocess, "check_call") as check_call,
        ):
            report = incoming.create_package(self.root, "No side effects")
        self.assertEqual(report.status, "passed", report.errors)
        run.assert_not_called()
        popen.assert_not_called()
        check_call.assert_not_called()

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell is required")
    def test_powershell_create_mode_does_not_invoke_git_or_quarto(self) -> None:
        fixture = self.root / "wrapper-fixture"
        command_bin = fixture / "command-bin"
        command_bin.mkdir(parents=True)
        repository_root = pathlib.Path(__file__).resolve().parents[1]
        for command in ("git", "quarto"):
            (command_bin / f"{command}.cmd").write_text(
                f'@echo called>"%~dp0{command}-called"\r\n@exit /b 91\r\n',
                encoding="ascii",
            )
        runner = fixture / "runner.ps1"
        literal = lambda value: "'" + str(value).replace("'", "''") + "'"
        runner.write_text(
            ". "
            + literal(repository_root / "publish_article.ps1")
            + " -ArticlePath 'unused.qmd'\n"
            + "function Initialize-LocalContext {\n"
            + "  $script:RepoRoot = "
            + literal(fixture)
            + "\n  $script:PythonPath = "
            + literal(pathlib.Path(sys.executable))
            + "\n  $script:IncomingToolPath = "
            + literal(repository_root / "scripts" / "incoming_package.py")
            + "\n}\n"
            + "$CreateIncomingPackage = 'Wrapper package'\n"
            + "$OpenFolder = $false\n"
            + "Invoke-CreateIncomingPackage\n",
            encoding="utf-8-sig",
        )
        environment = os.environ.copy()
        environment["PATH"] = str(command_bin) + os.pathsep + environment["PATH"]
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(runner),
            ],
            cwd=repository_root,
            env=environment,
            check=False,
            capture_output=True,
        )
        self.assertEqual(
            completed.returncode,
            0,
            "stdout:\n"
            + completed.stdout.decode(errors="replace")
            + "\nstderr:\n"
            + completed.stderr.decode(errors="replace"),
        )
        self.assertFalse((command_bin / "git-called").exists())
        self.assertFalse((command_bin / "quarto-called").exists())
        self.assertTrue((fixture / "incoming" / "wrapper-package" / "package.yml").is_file())

    def test_cli_uses_exit_code_two_for_one_mapping_approval(self) -> None:
        package = self.create()
        (package / "article.qmd").write_bytes(qmd_bytes("figure-1"))
        self.write_image(package, "alpha.png")
        self.write_image(package, "beta.png")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = incoming.main(
                [
                    "import",
                    "--repo-root",
                    str(self.root),
                    "--incoming-folder",
                    str(package),
                ]
            )
        self.assertEqual(exit_code, 2)
        self.assertIn('"status": "ambiguous"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
