"""Non-mutating tests for the rich Quarto article validator."""

from __future__ import annotations

import contextlib
import base64
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from articles import generate_articles
from scripts import article_validator as validator


def article_text(body: str, extra_yaml: str = "") -> str:
    """Build a minimal valid article without modifying repository fixtures."""

    extra = f"{extra_yaml.rstrip()}\n" if extra_yaml.strip() else ""
    return (
        "---\n"
        'title: "Test article"\n'
        "author: Andres Acosta\n"
        "date: 2026-07-15\n"
        "featured: true\n"
        "image: ../images/featured.png\n"
        'description: "Test description"\n'
        f"{extra}"
        "---\n"
        f"{body}"
    )


def quarto_config_text() -> str:
    """Return the article-only Quarto render configuration used by fixtures."""

    return (
        "project:\n"
        "  output-dir: _site\n"
        "  render:\n"
        "    - articles.qmd\n"
        '    - "articles/*.qmd"\n'
    )


class SourceInspectionTests(unittest.TestCase):
    """Exercise conservative source inspection using temporary QMD files."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary_directory.name)
        (self.root / "_quarto.yml").write_text(quarto_config_text(), encoding="utf-8")
        (self.root / "articles").mkdir()
        self.article = self.root / "articles" / "test-article.qmd"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def inspect(self, body: str, extra_yaml: str = "") -> validator.ArticleInspection:
        self.article.write_text(article_text(body, extra_yaml), encoding="utf-8")
        return validator.inspect_article(self.article)

    def test_inline_and_display_equations(self) -> None:
        inspection = self.inspect("Inline $x^2 + y^2 = z^2$.\n\n$$\nE = mc^2\n$$\n")
        self.assertTrue(inspection.math["present"])
        self.assertEqual(inspection.math["inline_count"], 1)
        self.assertEqual(inspection.math["display_count"], 1)

    def test_prices_containing_dollar_signs_are_not_math(self) -> None:
        inspection = self.inspect("Plans cost $5, $10, or $25.\n")
        self.assertFalse(inspection.math["present"])

    def test_escaped_dollars_and_inline_code_are_not_math(self) -> None:
        inspection = self.inspect(r"An escaped dollar is \$40 and code is `$x$`." + "\n")
        self.assertFalse(inspection.math["present"])

    def test_markdown_image_attributes_do_not_change_path(self) -> None:
        inspection = self.inspect(
            '![Plot](../images/plot-(final).png "Title"){width=80% fig-align="center"}\n'
        )
        self.assertEqual(len(inspection.body_images), 1)
        self.assertEqual(inspection.body_images[0]["target"], "../images/plot-(final).png")

    def test_reference_style_image_resolves_definition(self) -> None:
        inspection = self.inspect(
            "![Reference plot][figure-id]\n\n"
            '[figure-id]: <../images/reference plot.png> "Caption"\n'
        )
        self.assertEqual(len(inspection.body_images), 1)
        self.assertEqual(inspection.body_images[0]["target"], "../images/reference plot.png")
        self.assertEqual(inspection.body_images[0]["syntax"], "Markdown reference image")

    def test_quoted_and_unquoted_html_images(self) -> None:
        inspection = self.inspect(
            '<img src="../images/quoted.png" width="640">\n'
            "<img src=../images/unquoted.png height=480>\n"
        )
        self.assertEqual(
            [item["target"] for item in inspection.body_images],
            ["../images/quoted.png", "../images/unquoted.png"],
        )

    def test_python_and_r_cells_are_computational(self) -> None:
        inspection = self.inspect(
            '```{python}\nprint("hello")\n```\n\n'
            "```{r analysis, echo=FALSE}\nsummary(cars)\n```\n"
        )
        cells = [
            item for item in inspection.execution_indicators if item["kind"] == "executable-cell"
        ]
        self.assertEqual(inspection.classification, "Computational")
        self.assertEqual({item["language"] for item in cells}, {"python", "r"})

    def test_non_executable_fences_are_editorial_and_excluded(self) -> None:
        inspection = self.inspect(
            '```python\nprint("display only")\n'
            "![Example](../images/not-real.png)\n$x$\n```\n\n"
            '```{.python}\nprint("classed display block")\n```\n'
        )
        self.assertEqual(inspection.classification, "Editorial")
        self.assertEqual(inspection.body_images, [])
        self.assertFalse(inspection.math["present"])

    def test_declaration_only_jupyter_metadata_remains_editorial(self) -> None:
        inspection = self.inspect("No executable cells.\n", "jupyter: python3")
        self.assertEqual(inspection.classification, "Editorial")
        self.assertEqual(len(inspection.execution_indicators), 1)
        self.assertFalse(inspection.execution_indicators[0]["requires_execution"])

    def test_notebook_backed_metadata_is_computational(self) -> None:
        inspection = self.inspect("No fenced cells.\n", "notebook: analysis.ipynb")
        self.assertEqual(inspection.classification, "Computational")
        self.assertTrue(inspection.execution_indicators[0]["requires_execution"])

    def test_script_examples_are_excluded_from_source_discovery(self) -> None:
        inspection = self.inspect(
            '<script>const price = "$5"; const image = "![x](../images/not-real.png)";</script>\n'
        )
        self.assertEqual(inspection.body_images, [])
        self.assertFalse(inspection.math["present"])


class GeneratedHtmlTests(unittest.TestCase):
    """Exercise generated HTML parsing and complete rendered validation."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary_directory.name)
        (self.root / "_quarto.yml").write_text(quarto_config_text(), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_generated_html_images_css_links_anchors_and_math(self) -> None:
        html_path = self.root / "article.html"
        html_path.write_text(
            "<!doctype html><html><head>"
            '<link rel="stylesheet" href="article.css">'
            '<script src="https://cdn.example/mathjax/tex-chtml.js"></script>'
            "</head><body>"
            '<section id="result"><span class="math inline">x</span>'
            '<img src="images/plot.png"><a href="#result">Result</a>'
            "</section></body></html>",
            encoding="utf-8",
        )
        inspection = validator.inspect_generated_html(html_path)
        self.assertEqual(len(inspection.image_refs), 1)
        self.assertEqual(len(inspection.stylesheets), 1)
        self.assertEqual(len(inspection.links), 1)
        self.assertIn("result", inspection.anchors)
        self.assertEqual(inspection.math_signals, {"math markup", "MathJax script"})

    def test_featured_image_synchronization_and_rendered_assets(self) -> None:
        articles = self.root / "articles"
        images = self.root / "images"
        site = self.root / "_site"
        site_articles = site / "articles"
        for directory in (articles, images, site_articles):
            directory.mkdir(parents=True, exist_ok=True)

        article = articles / "test-article.qmd"
        article.write_text(
            article_text("![Body](../images/body.png)\n\nInline $x^2$.\n"),
            encoding="utf-8",
        )
        (images / "featured.png").write_bytes(b"featured-image")
        (images / "body.png").write_bytes(b"body-image")
        template = self.root / "articles.qmd"
        template.write_text("{{featured_articles}}\n", encoding="utf-8")

        (site / "article.css").write_text(".article {}\n", encoding="utf-8")
        (site / "style.css").write_text("body {}\n", encoding="utf-8")
        (site / "articles.html").write_text(
            "<!doctype html><html><body>"
            '<img src="../images/featured.png">'
            '<a href="articles/test-article.html">Test</a>'
            "</body></html>",
            encoding="utf-8",
        )
        (site_articles / "test-article.html").write_text(
            "<!doctype html><html><head>"
            '<link rel="stylesheet" href="../style.css">'
            '<link rel="stylesheet" href="../article.css">'
            '<script src="https://cdn.example/mathjax/tex-chtml.js"></script>'
            "</head><body>"
            '<section id="equation"><span class="math inline">x^2</span>'
            '<img src="../images/body.png">'
            '<a href="#equation">Equation</a></section>'
            "</body></html>",
            encoding="utf-8",
        )

        sync_report = validator.synchronize_images(self.root, article)
        report = validator.validate_rendered(
            self.root,
            article,
            validator.sha256_file(template),
        )

        self.assertEqual(sync_report.status, "passed", sync_report.errors)
        self.assertEqual(report.status, "passed", report.errors)
        self.assertEqual(report.classification, "Editorial")
        self.assertEqual(report.warnings, [])
        self.assertEqual((site / "images" / "featured.png").read_bytes(), b"featured-image")
        self.assertEqual((site / "images" / "body.png").read_bytes(), b"body-image")
        generated_paths = {item["path"] for item in report.generated_assets}
        self.assertIn("_site/articles.html", generated_paths)
        self.assertIn("_site/articles/test-article.html", generated_paths)
        self.assertIn("_site/article.css", generated_paths)
        self.assertIn("_site/images/body.png", generated_paths)

    def test_machine_readable_contract_contains_required_fields(self) -> None:
        report = validator.ValidationReport(classification="Editorial", expected_article_slug="example")
        payload = report.as_dict()
        self.assertEqual(
            set(payload),
            {
                "status",
                "classification",
                "errors",
                "warnings",
                "source_images",
                "generated_assets",
                "expected_article_slug",
                "detected_executable_indicators",
                "math",
            },
        )
        self.assertEqual(payload["status"], "passed")

    def test_image_copy_refuses_destination_outside_deploy_root(self) -> None:
        source_root = self.root / "images"
        destination_root = self.root / "_site" / "images"
        source_root.mkdir()
        destination_root.mkdir(parents=True)
        source = source_root / "source.png"
        source.write_bytes(b"source")
        outside = self.root / "outside.png"
        report = validator.ValidationReport()

        validator.copy_verified_image(
            source,
            outside,
            source_root,
            destination_root,
            report,
            "test image",
            True,
        )

        self.assertTrue(report.errors)
        self.assertFalse(outside.exists())

    def test_image_copy_detects_hash_mismatch_after_copy(self) -> None:
        source_root = self.root / "images"
        destination_root = self.root / "_site" / "images"
        source_root.mkdir()
        destination_root.mkdir(parents=True)
        source = source_root / "featured.png"
        destination = destination_root / "featured.png"
        source.write_bytes(b"expected-image")
        report = validator.ValidationReport()

        def corrupt_copy(_source: pathlib.Path, target: pathlib.Path) -> None:
            pathlib.Path(target).write_bytes(b"corrupted-image")

        with mock.patch.object(validator.shutil, "copyfile", side_effect=corrupt_copy):
            validator.copy_verified_image(
                source,
                destination,
                source_root,
                destination_root,
                report,
                "featured image",
                True,
            )

        self.assertTrue(any("SHA-256 mismatch" in error for error in report.errors))


class QuartoRenderSafetyTests(unittest.TestCase):
    """Require article-only render inputs and reject internal documentation output."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary_directory.name)
        (self.root / "_quarto.yml").write_text(quarto_config_text(), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_render_allowlist_includes_articles_and_excludes_internal_markdown(self) -> None:
        report = validator.ValidationReport()
        render_inputs = validator.read_quarto_render_inputs(self.root, report)

        self.assertEqual(render_inputs, ("articles.qmd", "articles/*.qmd"))
        self.assertEqual(report.errors, [])

        def is_rendered(path: str) -> bool:
            candidate = pathlib.PurePosixPath(path)
            return any(candidate.match(pattern) for pattern in render_inputs or ())

        self.assertTrue(is_rendered("articles.qmd"))
        self.assertTrue(is_rendered("articles/future-article.qmd"))
        self.assertFalse(is_rendered("AGENTS.md"))
        self.assertFalse(is_rendered("README.md"))
        self.assertFalse(is_rendered("scripts/notes.md"))
        self.assertFalse(is_rendered("tests/fixtures.md"))

    def test_broad_render_configuration_is_rejected(self) -> None:
        (self.root / "_quarto.yml").write_text(
            "project:\n  render:\n    - '*.qmd'\n    - '*.md'\n",
            encoding="utf-8",
        )
        report = validator.ValidationReport()

        validator.validate_quarto_render_config(self.root, report)

        self.assertTrue(any("project.render" in error for error in report.errors))

    def test_internal_markdown_deployment_artifacts_are_rejected(self) -> None:
        site_root = self.root / "_site"
        site_root.mkdir()
        (self.root / "AGENTS.md").write_text("Internal instructions\n", encoding="utf-8")
        (site_root / "AGENTS.html").write_text("<p>Internal</p>\n", encoding="utf-8")
        (site_root / "AGENTS_files").mkdir()
        report = validator.ValidationReport()

        validator.validate_no_internal_markdown_artifacts(self.root, site_root, report)

        self.assertTrue(any("AGENTS.md" in error for error in report.errors))
        self.assertTrue(any("AGENTS_files" in error for error in report.errors))


class WindowsProcessTests(unittest.TestCase):
    """Exercise Windows encoding and native-process behavior without rendering."""

    repository_root = pathlib.Path(__file__).resolve().parents[1]
    wrapper_path = repository_root / "publish_article.ps1"

    @staticmethod
    def powershell_literal(value: pathlib.Path | str) -> str:
        """Quote a value as a PowerShell single-quoted string literal."""

        return "'" + str(value).replace("'", "''") + "'"

    def invoke_python_through_wrapper(self, child_source: str) -> tuple[subprocess.CompletedProcess[bytes], dict[str, object]]:
        """Dot-source the wrapper and invoke Python through its process helper."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            child = root / "child.py"
            runner = root / "runner.ps1"
            result_path = root / "result.json"
            child.write_text(child_source, encoding="utf-8")
            runner.write_text(
                ". "
                + self.powershell_literal(self.wrapper_path)
                + " -ArticlePath 'unused.qmd'\n"
                + "$result = Invoke-NativeProcess "
                + "-FilePath "
                + self.powershell_literal(sys.executable)
                + " -Arguments @("
                + self.powershell_literal(child)
                + ") -WorkingDirectory "
                + self.powershell_literal(root)
                + " -Utf8Python\n"
                + "$payload = [ordered]@{\n"
                + "  exit_code = $result.ExitCode\n"
                + "  stdout_b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($result.StdOut))\n"
                + "  stderr_b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($result.StdErr))\n"
                + "} | ConvertTo-Json -Compress\n"
                + "[IO.File]::WriteAllText("
                + self.powershell_literal(result_path)
                + ", $payload, [Text.UTF8Encoding]::new($false))\n",
                encoding="utf-8-sig",
            )
            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(runner),
                ],
                check=False,
                capture_output=True,
            )
            result_text = result_path.read_text(encoding="utf-8") if result_path.exists() else ""
            if completed.returncode != 0 or not result_text.strip():
                self.fail(
                    "PowerShell process harness failed.\nstdout:\n"
                    + completed.stdout.decode(errors="replace")
                    + "\nstderr:\n"
                    + completed.stderr.decode(errors="replace")
                )
            payload = json.loads(result_text)
            payload["stdout"] = base64.b64decode(payload["stdout_b64"]).decode("utf-8")
            payload["stderr"] = base64.b64decode(payload["stderr_b64"]).decode("utf-8")
            return completed, payload

    def test_generator_console_falls_back_safely_under_cp1252(self) -> None:
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252", write_through=True)
        with mock.patch.object(generate_articles.sys, "stdout", stream), mock.patch.object(
            generate_articles.sys, "stderr", stream
        ):
            generate_articles.configure_console_streams()
            stream.write("status: \u2705\n")
            stream.flush()
            self.assertIn(b"\\u2705", raw.getvalue())

    def test_generator_source_status_output_is_ascii_safe(self) -> None:
        source = pathlib.Path(generate_articles.__file__).read_text(encoding="utf-8")
        source.encode("ascii")

    def test_utf8_child_environment_and_generator_stderr_with_zero_exit(self) -> None:
        completed, payload = self.invoke_python_through_wrapper(
            "import os, sys\n"
            "print(os.environ.get('PYTHONUTF8', '') + '|' + "
            "os.environ.get('PYTHONIOENCODING', '') + '|\u2713')\n"
            "print('warning \u2713', file=sys.stderr)\n"
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        self.assertEqual(payload["exit_code"], 0)
        self.assertIn("1|utf-8|\u2713", payload["stdout"])
        self.assertIn("warning \u2713", payload["stderr"])

    def test_generator_stderr_with_nonzero_exit_is_preserved(self) -> None:
        completed, payload = self.invoke_python_through_wrapper(
            "import sys\nprint('fatal diagnostic', file=sys.stderr)\nsys.exit(7)\n"
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        self.assertEqual(payload["exit_code"], 7)
        self.assertIn("fatal diagnostic", payload["stderr"])


class CommandContractTests(unittest.TestCase):
    """Verify JSON status and process exit semantics used by PowerShell."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary_directory.name)
        (self.root / "_quarto.yml").write_text(quarto_config_text(), encoding="utf-8")
        (self.root / "articles").mkdir()
        (self.root / "images").mkdir()
        (self.root / "images" / "featured.png").write_bytes(b"featured")
        self.article = self.root / "articles" / "contract-test.qmd"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_source_command(self) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = validator.main(
                [
                    "source",
                    "--repo-root",
                    str(self.root),
                    "--article",
                    str(self.article),
                ]
            )
        return exit_code, json.loads(output.getvalue())

    def test_warnings_do_not_fail_and_computational_errors_do(self) -> None:
        self.article.write_text(
            article_text("No executable cells.\n", "jupyter: python3"),
            encoding="utf-8",
        )
        exit_code, payload = self.run_source_command()
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "passed")
        self.assertTrue(payload["warnings"])

        self.article.write_text(
            article_text('```{python}\nprint("execute")\n```\n'),
            encoding="utf-8",
        )
        exit_code, payload = self.run_source_command()
        self.assertNotEqual(exit_code, 0)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["classification"], "Computational")


if __name__ == "__main__":
    unittest.main()
