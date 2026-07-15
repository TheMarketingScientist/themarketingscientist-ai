"""Non-mutating tests for the rich Quarto article validator."""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import tempfile
import unittest

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


class SourceInspectionTests(unittest.TestCase):
    """Exercise conservative source inspection using temporary QMD files."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary_directory.name)
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

    def test_rendered_validation_syncs_and_verifies_local_assets(self) -> None:
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

        report = validator.validate_rendered(
            self.root,
            article,
            validator.sha256_file(template),
            sync_images=True,
        )

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


class CommandContractTests(unittest.TestCase):
    """Verify JSON status and process exit semantics used by PowerShell."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary_directory.name)
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
