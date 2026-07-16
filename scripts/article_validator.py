#!/usr/bin/env python3
"""Validate Quarto article sources and their generated deployment artifacts.

Source inspection is deliberately conservative. Quarto rendering remains the
authoritative parser for rich Markdown and math syntax. This module never
rewrites article QMD content. Its explicit ``sync-images`` mode copies
already-validated local images into ``_site/images`` before the separate,
read-only rendered validation.
"""

from __future__ import annotations

import argparse
import datetime as datetime_module
import hashlib
import html
import json
import os
import pathlib
import re
import shutil
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import unquote, urljoin, urlsplit

import yaml


REQUIRED_METADATA = ("title", "date", "author", "featured", "image", "description")
STRING_METADATA = ("title", "author", "image", "description")
INTENDED_QUARTO_RENDER_INPUTS = ("articles.qmd", "articles/*.qmd")
ARTICLE_CTA_DESCRIPTION = (
    "I advise executives on measurement strategy, marketing economics, and Marketing Science "
    "product and vendor decisions."
)
ARTICLE_CTA_BUTTON_LABEL = "Schedule a call"
ARTICLE_CTA_URL = "https://calendly.com/andres-themarketingscientist/some-context"
ARTICLE_CTA_OPT_OUT_FIELD = "article-cta"
ARTICLE_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.qmd$")
FRONT_MATTER_PATTERN = re.compile(
    r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|\Z)",
    re.DOTALL,
)
FENCE_PATTERN = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
REFERENCE_DEFINITION_PATTERN = re.compile(r"^[ \t]{0,3}\[([^\]]+)\]:[ \t]*(.*)$")
HTML_CODE_PATTERN = re.compile(
    r"<(pre|code|script|style)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
EXECUTABLE_LANGUAGES = {
    "bash",
    "csharp",
    "dotnet",
    "fsharp",
    "javascript",
    "julia",
    "matlab",
    "nodejs",
    "ojs",
    "powershell",
    "pwsh",
    "python",
    "r",
    "sas",
    "scala",
    "sh",
    "sql",
    "stata",
    "typescript",
    "zsh",
}


@dataclass
class ValidationReport:
    """Machine-readable result shared by every command."""

    classification: str = "Unknown"
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_images: list[dict[str, Any]] = field(default_factory=list)
    generated_assets: list[dict[str, str]] = field(default_factory=list)
    expected_article_slug: str = ""
    detected_executable_indicators: list[dict[str, Any]] = field(default_factory=list)
    math: dict[str, Any] = field(
        default_factory=lambda: {"present": False, "inline_count": 0, "display_count": 0}
    )

    @property
    def status(self) -> str:
        return "failed" if self.errors else "passed"

    def add_error(self, message: str) -> None:
        if message not in self.errors:
            self.errors.append(message)

    def add_warning(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def add_source_image(self, image: dict[str, Any]) -> None:
        key = (image.get("source_path"), image.get("role"), image.get("reference"))
        existing = {
            (item.get("source_path"), item.get("role"), item.get("reference"))
            for item in self.source_images
        }
        if key not in existing:
            self.source_images.append(image)

    def add_generated_asset(self, path: pathlib.Path, kind: str, repo_root: pathlib.Path) -> None:
        item = {"path": repo_relative(path, repo_root), "kind": kind}
        if item not in self.generated_assets:
            self.generated_assets.append(item)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "classification": self.classification,
            "errors": self.errors,
            "warnings": self.warnings,
            "source_images": self.source_images,
            "generated_assets": self.generated_assets,
            "expected_article_slug": self.expected_article_slug,
            "detected_executable_indicators": self.detected_executable_indicators,
            "math": self.math,
        }


@dataclass
class ArticleInspection:
    """Parsed source facts without editorial transformations."""

    metadata: dict[str, Any]
    body_images: list[dict[str, Any]]
    math: dict[str, Any]
    execution_indicators: list[dict[str, Any]]
    classification: str
    structural_errors: list[str]
    cta: "CtaInspection"


@dataclass
class HtmlElement:
    """Small HTML tree node used for structural CTA checks."""

    tag: str
    attrs: dict[str, str]
    line: int
    children: list[Any] = field(default_factory=list)
    closed: bool = False
    parent: "HtmlElement | None" = field(default=None, repr=False)


@dataclass
class CtaInspection:
    """Article CTA elements discovered in source or rendered HTML."""

    root: HtmlElement
    blocks: list[HtmlElement]


@dataclass
class HtmlInspection:
    """Local references and structural signals extracted from generated HTML."""

    image_refs: list[dict[str, Any]]
    stylesheets: list[dict[str, Any]]
    links: list[dict[str, Any]]
    anchors: set[str]
    math_signals: set[str]
    cta: CtaInspection


def repo_relative(path: pathlib.Path, repo_root: pathlib.Path) -> str:
    """Return a normalized repository-relative path."""

    return path.resolve().relative_to(repo_root.resolve()).as_posix()


def sha256_file(path: pathlib.Path) -> str:
    """Hash a file without loading the complete asset into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def blank_like(value: str) -> str:
    """Replace content with spaces while retaining line boundaries."""

    return "".join("\n" if character == "\n" else " " for character in value)


def line_number(value: str, offset: int) -> int:
    return value.count("\n", 0, offset) + 1


def is_escaped(value: str, offset: int) -> bool:
    backslashes = 0
    offset -= 1
    while offset >= 0 and value[offset] == "\\":
        backslashes += 1
        offset -= 1
    return backslashes % 2 == 1


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML front matter and return metadata plus untouched body text."""

    match = FRONT_MATTER_PATTERN.match(text)
    if not match:
        raise ValueError("Missing or invalid YAML front matter delimiters")
    try:
        metadata = yaml.safe_load(match.group(1))
    except yaml.YAMLError as error:
        raise ValueError(f"Invalid YAML front matter: {error}") from error
    if not isinstance(metadata, dict):
        raise ValueError("YAML front matter must be a mapping")
    return metadata, text[match.end() :]


def executable_language(info: str) -> str | None:
    """Return the engine name for a Quarto executable fence, if present."""

    stripped = info.strip()
    if not stripped.startswith("{") or "}" not in stripped:
        return None
    inner = stripped[1 : stripped.index("}")].strip()
    if not inner or inner.startswith("."):
        return None
    language = inner.split(",", 1)[0].split(None, 1)[0].lower()
    return language if language in EXECUTABLE_LANGUAGES else None


def closes_fence(candidate: str, character: str, minimum_length: int) -> bool:
    """Check a fence closer without constructing a dynamic regular expression."""

    stripped = candidate.lstrip(" \t")
    if len(candidate) - len(stripped) > 3 or not stripped.startswith(character):
        return False
    count = 0
    while count < len(stripped) and stripped[count] == character:
        count += 1
    return count >= minimum_length and not stripped[count:].strip()


def mask_fenced_code(body: str) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Mask fenced code and classify executable Quarto cells."""

    masked: list[str] = []
    indicators: list[dict[str, Any]] = []
    errors: list[str] = []
    opened: dict[str, Any] | None = None

    for number, line in enumerate(body.splitlines(keepends=True), start=1):
        candidate = line.rstrip("\r\n")
        if opened is not None:
            masked.append(blank_like(line))
            if closes_fence(candidate, opened["character"], opened["length"]):
                opened = None
            continue

        match = FENCE_PATTERN.match(candidate)
        if not match:
            masked.append(line)
            continue

        fence = match.group("fence")
        info = match.group("info").strip()
        opened = {"character": fence[0], "length": len(fence), "line": number}
        masked.append(blank_like(line))

        language = executable_language(info)
        if language:
            indicators.append(
                {
                    "kind": "executable-cell",
                    "line": number,
                    "language": language,
                    "detail": f"Executable Quarto fenced cell {{{language}}} at body line {number}",
                    "requires_execution": True,
                }
            )

    if opened is not None:
        errors.append(f"Unclosed fenced code block beginning at body line {opened['line']}")
    return "".join(masked), indicators, errors


def mask_inline_code(value: str) -> str:
    """Mask matched Pandoc backtick spans without rejecting unmatched literals."""

    result = list(value)
    offset = 0
    while offset < len(value):
        if value[offset] != "`" or is_escaped(value, offset):
            offset += 1
            continue

        run_end = offset + 1
        while run_end < len(value) and value[run_end] == "`":
            run_end += 1
        delimiter = value[offset:run_end]
        closing = value.find(delimiter, run_end)
        while closing >= 0:
            before_tick = closing > 0 and value[closing - 1] == "`"
            after = closing + len(delimiter)
            after_tick = after < len(value) and value[after] == "`"
            if not before_tick and not after_tick:
                break
            closing = value.find(delimiter, closing + 1)
        if closing < 0:
            offset = run_end
            continue

        for index in range(offset, closing + len(delimiter)):
            if result[index] != "\n":
                result[index] = " "
        offset = closing + len(delimiter)
    return "".join(result)


def mask_code_regions(body: str) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Exclude code examples from conservative image and math discovery."""

    without_fences, indicators, errors = mask_fenced_code(body)
    without_inline_code = mask_inline_code(without_fences)
    safe_body = HTML_CODE_PATTERN.sub(lambda match: blank_like(match.group(0)), without_inline_code)
    return safe_body, indicators, errors


def source_html_for_validation(body: str) -> str:
    """Expose raw HTML while masking display-only fenced and inline code examples."""

    result: list[str] = []
    opened: dict[str, Any] | None = None
    preserve_contents = False
    for line in body.splitlines(keepends=True):
        candidate = line.rstrip("\r\n")
        if opened is not None:
            if closes_fence(candidate, opened["character"], opened["length"]):
                result.append(blank_like(line))
                opened = None
                preserve_contents = False
            else:
                result.append(line if preserve_contents else blank_like(line))
            continue

        match = FENCE_PATTERN.match(candidate)
        if not match:
            result.append(line)
            continue

        fence = match.group("fence")
        info = match.group("info").strip().casefold()
        opened = {"character": fence[0], "length": len(fence)}
        preserve_contents = info == "{=html}"
        result.append(blank_like(line))

    without_inline_code = mask_inline_code("".join(result))
    return HTML_CODE_PATTERN.sub(lambda match: blank_like(match.group(0)), without_inline_code)


VOID_HTML_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class HtmlTreeParser(HTMLParser):
    """Build a minimal non-executing HTML tree for CTA structure and placement checks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HtmlElement("#document", {}, 1, closed=True)
        self.stack = [self.root]

    def add_element(
        self, tag: str, attrs: list[tuple[str, str | None]], *, closed: bool
    ) -> HtmlElement:
        attributes = {name.casefold(): value or "" for name, value in attrs if name}
        element = HtmlElement(
            tag=tag.casefold(),
            attrs=attributes,
            line=self.getpos()[0],
            closed=closed,
            parent=self.stack[-1],
        )
        self.stack[-1].children.append(element)
        return element

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.casefold()
        element = self.add_element(lowered, attrs, closed=lowered in VOID_HTML_ELEMENTS)
        if lowered not in VOID_HTML_ELEMENTS:
            self.stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.add_element(tag, attrs, closed=True)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        for offset in range(len(self.stack) - 1, 0, -1):
            if self.stack[offset].tag == lowered:
                self.stack[offset].closed = True
                del self.stack[offset:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def element_classes(element: HtmlElement) -> set[str]:
    return set(element.attrs.get("class", "").split())


def find_elements_with_class(root: HtmlElement, class_name: str) -> list[HtmlElement]:
    matches: list[HtmlElement] = []
    for child in root.children:
        if not isinstance(child, HtmlElement):
            continue
        if class_name in element_classes(child):
            matches.append(child)
        matches.extend(find_elements_with_class(child, class_name))
    return matches


def significant_children(element: HtmlElement) -> list[Any]:
    return [
        child
        for child in element.children
        if isinstance(child, HtmlElement) or (isinstance(child, str) and child.strip())
    ]


def element_text(element: HtmlElement) -> str:
    parts: list[str] = []
    for child in element.children:
        if isinstance(child, str):
            parts.append(child)
        else:
            parts.append(element_text(child))
    return "".join(parts).strip()


def normalized_element_text(element: HtmlElement) -> str:
    return " ".join(element_text(element).split())


def inspect_cta_html(value: str) -> CtaInspection:
    """Find standardized CTA blocks without executing or rewriting HTML."""

    parser = HtmlTreeParser()
    parser.feed(value)
    return CtaInspection(
        root=parser.root,
        blocks=find_elements_with_class(parser.root, "article-cta"),
    )


def next_significant_sibling(element: HtmlElement) -> HtmlElement | str | None:
    if element.parent is None:
        return None
    found = False
    for sibling in element.parent.children:
        if not found:
            found = sibling is element
            continue
        if isinstance(sibling, HtmlElement) or (isinstance(sibling, str) and sibling.strip()):
            return sibling
    return None


def cta_opted_out(metadata: dict[str, Any]) -> bool:
    return metadata.get(ARTICLE_CTA_OPT_OUT_FIELD) is False


def validate_article_cta(
    metadata: dict[str, Any],
    inspection: CtaInspection,
    report: ValidationReport,
    context: str,
) -> None:
    """Validate the single standardized CTA and its position before the LinkedIn footer."""

    if cta_opted_out(metadata):
        if inspection.blocks:
            report.add_error(
                f"{context} opts out with '{ARTICLE_CTA_OPT_OUT_FIELD}: false' but still "
                "contains an article CTA block"
            )
        return

    if len(inspection.blocks) != 1:
        report.add_error(
            f"{context} must contain exactly one '<section class=\"article-cta\">' block; "
            f"found {len(inspection.blocks)}"
        )
        return

    block = inspection.blocks[0]
    if (
        block.tag != "section"
        or element_classes(block) != {"article-cta"}
        or set(block.attrs) != {"class"}
        or not block.closed
    ):
        report.add_error(
            f"{context} article CTA must be a closed '<section class=\"article-cta\">' element"
        )

    children = significant_children(block)
    valid_children = (
        len(children) == 3
        and all(isinstance(child, HtmlElement) for child in children)
        and [child.tag for child in children] == ["p", "p", "a"]
    )
    if not valid_children:
        report.add_error(
            f"{context} article CTA must contain exactly the question paragraph, service "
            "description paragraph, and call-scheduling link in that order"
        )
        return

    question, description, button = children
    if (
        element_classes(question) != {"article-cta-question"}
        or set(question.attrs) != {"class"}
        or not question.closed
    ):
        report.add_error(f"{context} CTA question must use class 'article-cta-question'")
    question_children = significant_children(question)
    if (
        len(question_children) != 1
        or not isinstance(question_children[0], HtmlElement)
        or question_children[0].tag != "strong"
        or question_children[0].attrs
        or not question_children[0].closed
        or any(isinstance(child, HtmlElement) for child in question_children[0].children)
    ):
        report.add_error(f"{context} CTA question must contain exactly one '<strong>' element")
        sentence = ""
    else:
        sentence = element_text(question_children[0])
    if not sentence:
        report.add_error(f"{context} CTA article-specific sentence must not be empty")
    elif len(sentence.split()) > 25:
        report.add_error(
            f"{context} CTA article-specific sentence must contain no more than 25 words; "
            f"found {len(sentence.split())}"
        )

    if (
        element_classes(description) != {"article-cta-description"}
        or set(description.attrs) != {"class"}
        or not description.closed
        or any(isinstance(child, HtmlElement) for child in description.children)
    ):
        report.add_error(f"{context} CTA service description must use class 'article-cta-description'")
    if normalized_element_text(description) != ARTICLE_CTA_DESCRIPTION:
        report.add_error(
            f"{context} CTA service description must match the approved text exactly: "
            f"{ARTICLE_CTA_DESCRIPTION}"
        )

    if (
        element_classes(button) != {"article-cta-button"}
        or set(button.attrs) != {"class", "href", "target", "rel"}
        or not button.closed
        or any(isinstance(child, HtmlElement) for child in button.children)
    ):
        report.add_error(f"{context} CTA link must use class 'article-cta-button'")
    if normalized_element_text(button) != ARTICLE_CTA_BUTTON_LABEL:
        report.add_error(
            f"{context} CTA button label must be exactly '{ARTICLE_CTA_BUTTON_LABEL}'"
        )
    if button.attrs.get("href") != ARTICLE_CTA_URL:
        report.add_error(f"{context} CTA href must be exactly '{ARTICLE_CTA_URL}'")
    if button.attrs.get("target") != "_blank":
        report.add_error(f"{context} CTA link must include target=\"_blank\"")
    if set(button.attrs.get("rel", "").split()) != {"noopener", "noreferrer"}:
        report.add_error(f"{context} CTA link must include rel=\"noopener noreferrer\"")

    following = next_significant_sibling(block)
    if (
        not isinstance(following, HtmlElement)
        or following.tag != "div"
        or "connect-section" not in element_classes(following)
    ):
        report.add_error(
            f"{context} article CTA must appear immediately before the existing LinkedIn footer "
            "'<div class=\"connect-section\">'"
        )


def markdown_unescape(value: str) -> str:
    """Remove backslash escapes used in common Markdown destinations."""

    punctuation = set("\\`*{}[]()#+.!_>~|-")
    result: list[str] = []
    offset = 0
    while offset < len(value):
        if value[offset] == "\\" and offset + 1 < len(value) and value[offset + 1] in punctuation:
            result.append(value[offset + 1])
            offset += 2
        else:
            result.append(value[offset])
            offset += 1
    return "".join(result)


def extract_destination(value: str) -> str | None:
    """Extract a Markdown link destination without titles or Quarto attributes."""

    candidate = value.strip()
    if not candidate:
        return None
    if candidate.startswith("<"):
        for offset in range(1, len(candidate)):
            if candidate[offset] == ">" and not is_escaped(candidate, offset):
                return markdown_unescape(candidate[1:offset])
        return None

    offset = 0
    while offset < len(candidate):
        if candidate[offset].isspace() and not is_escaped(candidate, offset):
            break
        offset += 1
    destination = markdown_unescape(candidate[:offset])
    return destination or None


def normalize_reference_label(value: str) -> str:
    return " ".join(value.strip().split()).casefold()


def closing_bracket(value: str, start: int) -> int:
    for offset in range(start, len(value)):
        if value[offset] == "]" and not is_escaped(value, offset):
            return offset
    return -1


def closing_parenthesis(value: str, start: int) -> int:
    depth = 1
    angle_destination = False
    for offset in range(start, len(value)):
        character = value[offset]
        if character == "<" and depth == 1 and not is_escaped(value, offset):
            angle_destination = True
        elif character == ">" and angle_destination and not is_escaped(value, offset):
            angle_destination = False
        elif not angle_destination and not is_escaped(value, offset):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    return offset
    return -1


def reference_definitions(body: str) -> dict[str, dict[str, Any]]:
    """Collect single-line Markdown reference definitions."""

    definitions: dict[str, dict[str, Any]] = {}
    for number, line in enumerate(body.splitlines(), start=1):
        match = REFERENCE_DEFINITION_PATTERN.match(line)
        if not match:
            continue
        destination = extract_destination(match.group(2))
        if destination:
            definitions[normalize_reference_label(match.group(1))] = {
                "target": destination,
                "line": number,
            }
    return definitions


def markdown_images(body: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Find inline, attributed, full, collapsed, and shortcut images."""

    definitions = reference_definitions(body)
    images: list[dict[str, Any]] = []
    errors: list[str] = []
    offset = 0

    while offset < len(body):
        start = body.find("![", offset)
        if start < 0:
            break
        alt_end = closing_bracket(body, start + 2)
        if alt_end < 0:
            errors.append(f"Unclosed Markdown image alt text at body line {line_number(body, start)}")
            break

        image_line = line_number(body, start)
        alt_text = body[start + 2 : alt_end]
        following = alt_end + 1

        if following < len(body) and body[following] == "(":
            target_end = closing_parenthesis(body, following + 1)
            if target_end < 0:
                errors.append(f"Unclosed Markdown image destination at body line {image_line}")
                offset = following + 1
                continue
            target = extract_destination(body[following + 1 : target_end])
            if target:
                images.append(
                    {"target": target, "syntax": "Markdown inline image", "line": image_line}
                )
            else:
                errors.append(f"Empty Markdown image destination at body line {image_line}")
            offset = target_end + 1
            continue

        if following < len(body) and body[following] == "[":
            label_end = closing_bracket(body, following + 1)
            if label_end < 0:
                errors.append(f"Unclosed Markdown image reference at body line {image_line}")
                offset = following + 1
                continue
            label_text = body[following + 1 : label_end] or alt_text
            definition = definitions.get(normalize_reference_label(label_text))
            if definition:
                images.append(
                    {
                        "target": definition["target"],
                        "syntax": "Markdown reference image",
                        "line": image_line,
                        "definition_line": definition["line"],
                        "label": label_text,
                    }
                )
            else:
                errors.append(
                    f"Undefined Markdown image reference [{label_text}] at body line {image_line}"
                )
            offset = label_end + 1
            continue

        definition = definitions.get(normalize_reference_label(alt_text))
        if definition:
            images.append(
                {
                    "target": definition["target"],
                    "syntax": "Markdown shortcut reference image",
                    "line": image_line,
                    "definition_line": definition["line"],
                    "label": alt_text,
                }
            )
        offset = following

    return images, errors


class SourceImageParser(HTMLParser):
    """Collect HTML image sources while leaving raw HTML untouched."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[dict[str, Any]] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "img":
            return
        attributes = {name.casefold(): value for name, value in attrs if name}
        source = attributes.get("src")
        if not source or not source.strip():
            self.errors.append(f"HTML img element without src at body line {self.getpos()[0]}")
            return
        self.images.append(
            {
                "target": source.strip(),
                "syntax": "HTML img element",
                "line": self.getpos()[0],
            }
        )

    handle_startendtag = handle_starttag


def inspect_math(body: str) -> dict[str, Any]:
    """Identify likely Pandoc dollar math without treating counts as syntax proof."""

    display_ranges: list[tuple[int, int]] = []
    inline_ranges: list[tuple[int, int]] = []
    offset = 0

    while offset < len(body) - 1:
        if body[offset : offset + 2] != "$$" or is_escaped(body, offset):
            offset += 1
            continue
        closing = offset + 2
        while True:
            closing = body.find("$$", closing)
            if closing < 0 or not is_escaped(body, closing):
                break
            closing += 2
        if closing < 0:
            offset += 2
            continue
        if body[offset + 2 : closing].strip():
            display_ranges.append((offset, closing + 2))
        offset = closing + 2

    inline_source = list(body)
    for start, end in display_ranges:
        for index in range(start, end):
            if inline_source[index] != "\n":
                inline_source[index] = " "
    inline_text = "".join(inline_source)

    offset = 0
    while offset < len(inline_text):
        if inline_text[offset] != "$" or is_escaped(inline_text, offset):
            offset += 1
            continue
        if offset + 1 >= len(inline_text) or inline_text[offset + 1].isspace():
            offset += 1
            continue

        candidate = offset + 1
        found = -1
        while candidate < len(inline_text):
            candidate = inline_text.find("$", candidate)
            if candidate < 0:
                break
            if is_escaped(inline_text, candidate):
                candidate += 1
                continue
            if "\n" in inline_text[offset + 1 : candidate]:
                break
            previous = inline_text[candidate - 1] if candidate > offset + 1 else ""
            following = inline_text[candidate + 1] if candidate + 1 < len(inline_text) else ""
            if previous and not previous.isspace() and not following.isdigit():
                found = candidate
                break
            candidate += 1
        if found >= 0:
            inline_ranges.append((offset, found + 1))
            offset = found + 1
        else:
            offset += 1

    return {
        "present": bool(display_ranges or inline_ranges),
        "inline_count": len(inline_ranges),
        "display_count": len(display_ranges),
    }


def legacy_math_delimiter_errors(body: str) -> list[str]:
    """Reject unsupported TeX delimiters after code regions have been masked."""

    errors: list[str] = []
    conventions = (
        (r"\(", "inline", "$...$"),
        (r"\[", "display", "$$...$$"),
    )
    for delimiter, kind, required_syntax in conventions:
        offset = 0
        while offset < len(body):
            start = body.find(delimiter, offset)
            if start < 0:
                break
            if not is_escaped(body, start):
                errors.append(
                    f"Unsupported {kind} math delimiter '{delimiter}' at body line "
                    f"{line_number(body, start)}. Use Pandoc {kind} math syntax "
                    f"'{required_syntax}'."
                )
            offset = start + len(delimiter)
    return errors


def metadata_execution_indicators(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Report engine declarations and explicit notebook requirements."""

    indicators: list[dict[str, Any]] = []
    for key in ("jupyter", "knitr", "engine"):
        if key not in metadata:
            continue
        value = metadata[key]
        requires_execution = key == "jupyter" and ".ipynb" in str(value).casefold()
        indicators.append(
            {
                "kind": "yaml",
                "line": None,
                "language": None,
                "detail": f"YAML {key} declaration: {value}",
                "requires_execution": requires_execution,
            }
        )

    for key in ("notebook", "source-notebook", "ipynb"):
        if key not in metadata:
            continue
        value = metadata[key]
        requires_execution = value not in (None, False, "", [])
        indicators.append(
            {
                "kind": "yaml",
                "line": None,
                "language": None,
                "detail": f"YAML {key} declaration: {value}",
                "requires_execution": requires_execution,
            }
        )
    return indicators


def inspect_article(article_path: pathlib.Path) -> ArticleInspection:
    """Inspect source content without writing or formatting it."""

    text = article_path.read_text(encoding="utf-8")
    metadata, body = parse_front_matter(text)
    safe_body, cell_indicators, structural_errors = mask_code_regions(body)
    cta = inspect_cta_html(source_html_for_validation(body))
    markdown_refs, markdown_errors = markdown_images(safe_body)

    html_parser = SourceImageParser()
    html_parser.feed(safe_body)

    images: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for image in markdown_refs + html_parser.images:
        key = (image["target"], image["syntax"], image["line"])
        if key not in seen:
            seen.add(key)
            images.append(image)

    indicators = cell_indicators + metadata_execution_indicators(metadata)
    classification = (
        "Computational" if any(item["requires_execution"] for item in indicators) else "Editorial"
    )
    return ArticleInspection(
        metadata=metadata,
        body_images=images,
        math=inspect_math(safe_body),
        execution_indicators=indicators,
        classification=classification,
        structural_errors=(
            structural_errors
            + legacy_math_delimiter_errors(safe_body)
            + markdown_errors
            + html_parser.errors
        ),
        cta=cta,
    )


def validate_article_location(
    repo_root: pathlib.Path, article_path: pathlib.Path, report: ValidationReport
) -> bool:
    """Require a direct articles/ child with a stable slug filename."""

    articles_root = (repo_root / "articles").resolve()
    try:
        articles_root.relative_to(repo_root.resolve())
    except ValueError:
        report.add_error(f"Article source directory resolves outside the repository: {articles_root}")
        return False
    if not article_path.is_file():
        report.add_error(f"Article file does not exist: {article_path}")
        return False
    if article_path.parent.resolve() != articles_root:
        report.add_error(f"Article must be a direct child of {articles_root}")
        return False
    if not ARTICLE_NAME_PATTERN.fullmatch(article_path.name):
        report.add_error(
            "Article filename must use lowercase ASCII letters/numbers and single hyphens: "
            f"{article_path.name}"
        )
        return False
    report.expected_article_slug = article_path.stem
    return True


def validate_metadata(metadata: dict[str, Any], report: ValidationReport) -> None:
    """Validate the required publication front matter contract."""

    for field_name in REQUIRED_METADATA:
        if field_name not in metadata:
            report.add_error(f"Required YAML field '{field_name}' is missing")

    for field_name in STRING_METADATA:
        if field_name not in metadata:
            continue
        value = metadata[field_name]
        if not isinstance(value, str):
            report.add_error(f"YAML field '{field_name}' must be a string")
        elif not value.strip():
            report.add_error(f"Required YAML field '{field_name}' is empty")

    if "featured" in metadata and metadata["featured"] is not True:
        report.add_error("YAML field 'featured' must be the boolean true")

    if (
        ARTICLE_CTA_OPT_OUT_FIELD in metadata
        and not isinstance(metadata[ARTICLE_CTA_OPT_OUT_FIELD], bool)
    ):
        report.add_error(
            f"YAML field '{ARTICLE_CTA_OPT_OUT_FIELD}' must be a boolean; use false for an "
            "explicit article-level CTA opt-out"
        )

    if "date" not in metadata:
        return
    date_value = metadata["date"]
    if isinstance(date_value, datetime_module.datetime):
        date_text = date_value.isoformat()
    elif isinstance(date_value, datetime_module.date):
        date_text = date_value.isoformat()
    elif isinstance(date_value, str):
        date_text = date_value
    else:
        report.add_error("YAML field 'date' must use YYYY-MM-DD")
        return

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
        report.add_error(f"YAML field 'date' must use YYYY-MM-DD: {date_text}")
        return
    try:
        datetime_module.datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError:
        report.add_error(f"YAML field 'date' is not a valid calendar date: {date_text}")


def is_external_reference(reference: str) -> bool:
    """Return true for remote, data, protocol-relative, or other URI schemes."""

    stripped = reference.strip()
    if stripped.startswith("//"):
        return True
    return bool(urlsplit(stripped).scheme)


def source_image_record(
    reference: str,
    article_path: pathlib.Path,
    repo_root: pathlib.Path,
    role: str,
    syntax: str,
    line: int | None,
    report: ValidationReport,
) -> dict[str, Any] | None:
    """Resolve and verify one local image constrained to images/**."""

    stripped = html.unescape(reference.strip())
    context = role if line is None else f"{syntax} at body line {line}"
    if is_external_reference(stripped):
        report.add_error(f"{context} must use a local image under images/: {reference}")
        return None

    parsed = urlsplit(stripped)
    relative_path = unquote(parsed.path)
    if not relative_path:
        report.add_error(f"{context} has an empty image path")
        return None

    images_root = (repo_root / "images").resolve()
    try:
        images_root.relative_to(repo_root.resolve())
    except ValueError:
        report.add_error(f"Source image directory resolves outside the repository: {images_root}")
        return None
    candidate = (article_path.parent / relative_path).resolve()
    try:
        image_relative = candidate.relative_to(images_root)
    except ValueError:
        report.add_error(f"{context} must resolve under {images_root}: {reference}")
        return None
    if not candidate.is_file():
        report.add_error(f"{context} does not exist: {candidate}")
        return None

    return {
        "reference": reference,
        "role": role,
        "syntax": syntax,
        "line": line,
        "source_path": repo_relative(candidate, repo_root),
        "deploy_path": (pathlib.PurePosixPath("_site/images") / image_relative.as_posix()).as_posix(),
    }


def read_quarto_render_inputs(
    repo_root: pathlib.Path, report: ValidationReport
) -> tuple[str, ...] | None:
    """Read and normalize the explicit Quarto project render allowlist."""

    config_path = repo_root / "_quarto.yml"
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        report.add_error(f"Could not read Quarto configuration {config_path}: {error}")
        return None
    project = config.get("project") if isinstance(config, dict) else None
    render = project.get("render") if isinstance(project, dict) else None
    if not isinstance(render, list) or not all(isinstance(item, str) for item in render):
        report.add_error("_quarto.yml project.render must be an explicit string allowlist")
        return None
    return tuple(item.replace("\\", "/") for item in render)


def validate_quarto_render_config(repo_root: pathlib.Path, report: ValidationReport) -> None:
    """Require the exact article-only Quarto render inputs."""

    render_inputs = read_quarto_render_inputs(repo_root, report)
    if render_inputs is None:
        return
    if render_inputs != INTENDED_QUARTO_RENDER_INPUTS:
        report.add_error(
            "_quarto.yml project.render must contain only "
            f"{list(INTENDED_QUARTO_RENDER_INPUTS)}; found {list(render_inputs)}"
        )


def validate_source(
    repo_root: pathlib.Path,
    article_path: pathlib.Path,
    *,
    fail_on_computational: bool = True,
) -> ValidationReport:
    """Validate source metadata, classification, and local images."""

    repo_root = repo_root.resolve()
    article_path = article_path.resolve()
    report = ValidationReport(expected_article_slug=article_path.stem)
    if not validate_article_location(repo_root, article_path, report):
        return report
    validate_quarto_render_config(repo_root, report)
    if report.errors:
        return report

    try:
        inspection = inspect_article(article_path)
    except (OSError, UnicodeError, ValueError) as error:
        report.add_error(f"Could not parse article source: {error}")
        return report

    report.classification = inspection.classification
    report.detected_executable_indicators = inspection.execution_indicators
    report.math = inspection.math
    for error in inspection.structural_errors:
        report.add_error(error)
    validate_metadata(inspection.metadata, report)
    validate_article_cta(inspection.metadata, inspection.cta, report, "Article source")

    for indicator in inspection.execution_indicators:
        if not indicator["requires_execution"]:
            report.add_warning(
                f"{indicator['detail']}. This declaration alone does not classify the article "
                "as Computational."
            )

    featured = inspection.metadata.get("image")
    if isinstance(featured, str) and featured.strip():
        record = source_image_record(
            featured,
            article_path,
            repo_root,
            "featured",
            "YAML featured image",
            None,
            report,
        )
        if record:
            report.add_source_image(record)

    for image in inspection.body_images:
        record = source_image_record(
            image["target"],
            article_path,
            repo_root,
            "body",
            image["syntax"],
            image["line"],
            report,
        )
        if record:
            report.add_source_image(record)

    if fail_on_computational and inspection.classification == "Computational":
        report.add_error(
            "Computational articles stop before generation. The current generator uses "
            "'quarto render --no-execute'; code or notebook execution requires explicit user "
            "authorization and a separately approved workflow."
        )
    return report


def classify_article(repo_root: pathlib.Path, article_path: pathlib.Path) -> ValidationReport:
    """Classify an article without requiring publication metadata or assets."""

    repo_root = repo_root.resolve()
    article_path = article_path.resolve()
    report = ValidationReport(expected_article_slug=article_path.stem)
    if not validate_article_location(repo_root, article_path, report):
        return report
    try:
        inspection = inspect_article(article_path)
    except (OSError, UnicodeError, ValueError) as error:
        report.add_error(f"Could not parse article source: {error}")
        return report
    report.classification = inspection.classification
    report.detected_executable_indicators = inspection.execution_indicators
    report.math = inspection.math
    for error in inspection.structural_errors:
        report.add_error(error)
    return report


class GeneratedHtmlParser(HTMLParser):
    """Inspect deployable HTML references without executing page content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.image_refs: list[dict[str, Any]] = []
        self.stylesheets: list[dict[str, Any]] = []
        self.links: list[dict[str, Any]] = []
        self.anchors: set[str] = set()
        self.math_signals: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered_tag = tag.casefold()
        attributes = {name.casefold(): value for name, value in attrs if name}

        element_id = attributes.get("id")
        if element_id:
            self.anchors.add(element_id)
        if lowered_tag == "a" and attributes.get("name"):
            self.anchors.add(attributes["name"] or "")

        classes = set((attributes.get("class") or "").casefold().split())
        if (
            "math" in classes
            or any(item.startswith("katex") for item in classes)
            or lowered_tag in {"math", "mjx-container"}
        ):
            self.math_signals.add("math markup")

        if lowered_tag == "img" and attributes.get("src"):
            self.image_refs.append(
                {"reference": attributes["src"], "line": self.getpos()[0]}
            )
        elif lowered_tag == "link":
            relationships = set((attributes.get("rel") or "").casefold().split())
            if "stylesheet" in relationships and attributes.get("href"):
                self.stylesheets.append(
                    {"reference": attributes["href"], "line": self.getpos()[0]}
                )
        elif lowered_tag == "a" and attributes.get("href"):
            self.links.append({"reference": attributes["href"], "line": self.getpos()[0]})
        elif lowered_tag == "script":
            source = (attributes.get("src") or "").casefold()
            script_type = (attributes.get("type") or "").casefold()
            if "mathjax" in source:
                self.math_signals.add("MathJax script")
            if "katex" in source:
                self.math_signals.add("KaTeX script")
            if script_type.startswith("math/tex"):
                self.math_signals.add("math/tex script")

    handle_startendtag = handle_starttag


def inspect_generated_html(path: pathlib.Path) -> HtmlInspection:
    """Parse generated HTML as text and return local-reference facts."""

    text = path.read_text(encoding="utf-8")
    parser = GeneratedHtmlParser()
    parser.feed(text)
    return HtmlInspection(
        image_refs=parser.image_refs,
        stylesheets=parser.stylesheets,
        links=parser.links,
        anchors=parser.anchors,
        math_signals=parser.math_signals,
        cta=inspect_cta_html(text),
    )


def resolve_deployable_reference(
    reference: str, containing_html: pathlib.Path, site_root: pathlib.Path
) -> tuple[pathlib.Path, str] | None:
    """Resolve a generated URL against a virtual site root.

    URL resolution deliberately clamps ``..`` at the deployment root, matching
    browser behavior for a custom-domain root deployment.
    """

    decoded = html.unescape(reference.strip())
    if not decoded or is_external_reference(decoded):
        return None
    relative_html = containing_html.resolve().relative_to(site_root.resolve()).as_posix()
    resolved_url = urlsplit(urljoin(f"https://deploy.invalid/{relative_html}", decoded))
    if resolved_url.hostname != "deploy.invalid":
        return None
    site_relative = unquote(resolved_url.path).lstrip("/")
    candidate = (site_root / site_relative).resolve() if site_relative else site_root / "index.html"
    try:
        candidate.relative_to(site_root.resolve())
    except ValueError as error:
        raise ValueError(f"Generated reference resolves outside _site: {reference}") from error
    return candidate, unquote(resolved_url.fragment)


def copy_verified_image(
    source: pathlib.Path,
    destination: pathlib.Path,
    source_root: pathlib.Path,
    destination_root: pathlib.Path,
    report: ValidationReport,
    context: str,
    sync_images: bool,
) -> None:
    """Optionally sync a local image, then require byte-for-byte equality."""

    try:
        resolved_source_root = source_root.resolve()
        resolved_destination_root = destination_root.resolve()
        resolved_source = source.resolve()
        resolved_destination = destination.resolve()
        resolved_source.relative_to(resolved_source_root)
        resolved_destination.relative_to(resolved_destination_root)
    except (OSError, ValueError):
        report.add_error(
            f"Refusing to access {context} outside its authorized image roots: "
            f"{source} -> {destination}"
        )
        return

    try:
        if sync_images:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.is_file() or sha256_file(source) != sha256_file(destination):
                shutil.copyfile(source, destination)
        if not destination.is_file():
            report.add_error(f"{context} is missing from deployable output: {destination}")
            return
        if sha256_file(source) != sha256_file(destination):
            report.add_error(
                f"SHA-256 mismatch for {context} in deployable output: {destination}"
            )
    except OSError as error:
        report.add_error(f"Could not verify {context}: {error}")


def collect_featured_images(
    repo_root: pathlib.Path, report: ValidationReport
) -> list[dict[str, Any]]:
    """Collect every featured image used by the generated article index."""

    records: list[dict[str, Any]] = []
    for article_path in sorted((repo_root / "articles").glob("*.qmd")):
        try:
            metadata, _ = parse_front_matter(article_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            report.add_error(f"Could not inspect featured metadata in {article_path.name}: {error}")
            continue
        if metadata.get("featured") is not True:
            continue
        image = metadata.get("image")
        if not isinstance(image, str) or not image.strip():
            report.add_error(f"Featured article {article_path.name} has no valid image field")
            continue
        record = source_image_record(
            image,
            article_path,
            repo_root,
            "featured",
            f"Featured image in {article_path.name}",
            None,
            report,
        )
        if record:
            record["article"] = article_path.name
            records.append(record)
            report.add_source_image(record)
    return records


def local_reference_paths(
    references: Iterable[dict[str, Any]],
    containing_html: pathlib.Path,
    site_root: pathlib.Path,
    report: ValidationReport,
    context: str,
) -> set[pathlib.Path]:
    """Resolve local generated references and warn about external ones."""

    paths: set[pathlib.Path] = set()
    for item in references:
        reference = str(item["reference"])
        try:
            resolved = resolve_deployable_reference(reference, containing_html, site_root)
        except ValueError as error:
            report.add_error(f"{context} at HTML line {item['line']}: {error}")
            continue
        if resolved is None:
            report.add_warning(f"{context} contains an external reference: {reference}")
            continue
        paths.add(resolved[0])
    return paths


def validate_stylesheets(
    html_path: pathlib.Path,
    inspection: HtmlInspection,
    site_root: pathlib.Path,
    repo_root: pathlib.Path,
    report: ValidationReport,
) -> None:
    """Require every local stylesheet and the article.css contract."""

    article_css_found = False
    for stylesheet in inspection.stylesheets:
        reference = str(stylesheet["reference"])
        try:
            resolved = resolve_deployable_reference(reference, html_path, site_root)
        except ValueError as error:
            report.add_error(f"Stylesheet at HTML line {stylesheet['line']}: {error}")
            continue
        if resolved is None:
            continue
        path = resolved[0]
        if not path.is_file():
            report.add_error(f"Generated stylesheet reference is missing: {reference} -> {path}")
            continue
        report.add_generated_asset(path, "stylesheet", repo_root)
        if path.name == "article.css":
            article_css_found = True
    if not article_css_found:
        report.add_error("Generated article HTML does not reference a deployable article.css file")


def validate_internal_links(
    html_path: pathlib.Path,
    inspection: HtmlInspection,
    site_root: pathlib.Path,
    report: ValidationReport,
) -> None:
    """Check definite local targets and anchors; ambiguous deployment links warn."""

    anchor_cache: dict[pathlib.Path, set[str]] = {html_path.resolve(): inspection.anchors}
    for link in inspection.links:
        reference = str(link["reference"])
        if not reference.strip() or is_external_reference(reference):
            continue
        try:
            resolved = resolve_deployable_reference(reference, html_path, site_root)
        except ValueError as error:
            report.add_error(f"Internal link at HTML line {link['line']}: {error}")
            continue
        if resolved is None:
            continue
        target, fragment = resolved
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            report.add_warning(
                f"Could not validate local link '{reference}' at generated HTML line "
                f"{link['line']}: target is absent from _site ({target}). It may be supplied "
                "separately on gh-pages."
            )
            continue
        if not fragment:
            continue
        if target.suffix != ".html":
            report.add_warning(f"Could not validate anchor '#{fragment}' in non-HTML target '{reference}'")
            continue
        if target not in anchor_cache:
            try:
                anchor_cache[target] = inspect_generated_html(target).anchors
            except (OSError, UnicodeError) as error:
                report.add_warning(f"Could not inspect anchors in {target}: {error}")
                continue
        if fragment not in anchor_cache[target]:
            report.add_warning(
                f"Generated link '{reference}' at HTML line {link['line']} targets missing "
                f"anchor '#{fragment}' in {target}"
            )


def validate_template_restoration(
    repo_root: pathlib.Path, expected_sha256: str, report: ValidationReport
) -> None:
    """Require articles.qmd restoration and absence of its temporary backup."""

    template = repo_root / "articles.qmd"
    backup = repo_root / "articles.qmd.bak"
    if backup.exists():
        report.add_error(f"Temporary source backup remains after generation: {backup}")
    if not template.is_file():
        report.add_error(f"articles.qmd is missing after generation: {template}")
        return
    if sha256_file(template).casefold() != expected_sha256.casefold():
        report.add_error("articles.qmd was not restored byte-for-byte after generation")


def resolved_site_root(repo_root: pathlib.Path, report: ValidationReport) -> pathlib.Path | None:
    """Return a generated-site root that cannot escape the repository."""

    site_root = (repo_root / "_site").resolve()
    try:
        site_root.relative_to(repo_root.resolve())
    except ValueError:
        report.add_error(f"Generated site directory resolves outside the repository: {site_root}")
        return None
    if not site_root.is_dir():
        report.add_error(f"Generated site directory is missing: {site_root}")
        return None
    return site_root


def validate_no_internal_markdown_artifacts(
    repo_root: pathlib.Path, site_root: pathlib.Path, report: ValidationReport
) -> None:
    """Reject deployable HTML or resource bundles derived from repository Markdown."""

    excluded_directories = {".git", ".quarto", "_site"}
    for directory, subdirectories, filenames in os.walk(repo_root):
        subdirectories[:] = [
            name for name in subdirectories if name not in excluded_directories
        ]
        directory_path = pathlib.Path(directory)
        for filename in filenames:
            if pathlib.Path(filename).suffix.casefold() != ".md":
                continue
            markdown_path = directory_path / filename
            relative = markdown_path.relative_to(repo_root)
            generated_html = site_root / relative.with_suffix(".html")
            generated_files = generated_html.with_name(f"{generated_html.stem}_files")
            if generated_html.exists():
                report.add_error(
                    f"Internal Markdown was rendered into deployable HTML: {relative.as_posix()} "
                    f"-> {generated_html.relative_to(repo_root).as_posix()}"
                )
            if generated_files.exists():
                report.add_error(
                    f"Internal Markdown resources are present in deployable output: "
                    f"{generated_files.relative_to(repo_root).as_posix()}"
                )


def deployment_image_records(
    repo_root: pathlib.Path,
    article_path: pathlib.Path,
    site_root: pathlib.Path,
    report: ValidationReport,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], pathlib.Path, pathlib.Path] | None:
    """Collect featured and target-body images plus their authorized roots."""

    featured_images = collect_featured_images(repo_root, report)
    if report.errors:
        return None
    target_body_images = [item for item in report.source_images if item.get("role") == "body"]
    source_images_root = repo_root / "images"
    site_images_root = site_root / "images"
    try:
        site_images_root.resolve().relative_to(site_root)
    except ValueError:
        report.add_error(
            f"Deployable image directory resolves outside the generated site: {site_images_root.resolve()}"
        )
        return None
    return featured_images, target_body_images, source_images_root, site_images_root


def check_deployment_images(
    repo_root: pathlib.Path,
    article_path: pathlib.Path,
    report: ValidationReport,
    records: tuple[list[dict[str, Any]], list[dict[str, Any]], pathlib.Path, pathlib.Path],
    *,
    sync_images: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Synchronize or verify every deployment image with SHA-256 checks."""

    featured_images, target_body_images, source_images_root, site_images_root = records
    for item in featured_images + target_body_images:
        source = repo_root / item["source_path"]
        destination = repo_root / item["deploy_path"]
        label = (
            f"Featured image for {item.get('article', article_path.name)}"
            if item["role"] == "featured"
            else f"{item['syntax']} at body line {item['line']}"
        )
        errors_before = len(report.errors)
        copy_verified_image(
            source,
            destination,
            source_images_root,
            site_images_root,
            report,
            label,
            sync_images,
        )
        if (
            len(report.errors) == errors_before
            and destination.is_file()
            and sha256_file(source) == sha256_file(destination)
        ):
            report.add_generated_asset(destination, "image", repo_root)
    return featured_images, target_body_images


def synchronize_images(repo_root: pathlib.Path, article_path: pathlib.Path) -> ValidationReport:
    """Copy changed deployment images after rendering and verify their hashes."""

    repo_root = repo_root.resolve()
    article_path = article_path.resolve()
    report = validate_source(repo_root, article_path, fail_on_computational=False)
    if report.classification == "Computational":
        report.add_error(
            "Image synchronization does not authorize Computational article execution; use a "
            "separately approved workflow"
        )
    if report.errors:
        return report

    site_root = resolved_site_root(repo_root, report)
    if site_root is None:
        return report
    records = deployment_image_records(repo_root, article_path, site_root, report)
    if records is None:
        return report
    check_deployment_images(
        repo_root,
        article_path,
        report,
        records,
        sync_images=True,
    )
    return report


def validate_rendered(
    repo_root: pathlib.Path,
    article_path: pathlib.Path,
    template_sha256: str,
) -> ValidationReport:
    """Validate generated pages and already-synchronized deployment assets."""

    repo_root = repo_root.resolve()
    article_path = article_path.resolve()
    report = validate_source(repo_root, article_path, fail_on_computational=False)
    if report.classification == "Computational":
        report.add_error(
            "Rendered validation does not authorize Computational article execution; use a "
            "separately approved workflow"
        )

    validate_template_restoration(repo_root, template_sha256, report)
    if report.errors:
        return report

    site_root = resolved_site_root(repo_root, report)
    if site_root is None:
        return report
    validate_no_internal_markdown_artifacts(repo_root, site_root, report)
    index_path = site_root / "articles.html"
    article_html = site_root / "articles" / f"{report.expected_article_slug}.html"
    if not index_path.is_file():
        report.add_error(f"Generated article index is missing: {index_path}")
    if not article_html.is_file():
        report.add_error(f"Generated article HTML is missing: {article_html}")
    if not index_path.is_file() or not article_html.is_file():
        return report

    try:
        index_inspection = inspect_generated_html(index_path)
        article_inspection = inspect_generated_html(article_html)
    except (OSError, UnicodeError) as error:
        report.add_error(f"Could not inspect generated HTML: {error}")
        return report

    report.add_generated_asset(index_path, "article-index", repo_root)
    report.add_generated_asset(article_html, "article-html", repo_root)

    linked = False
    for link in index_inspection.links:
        try:
            resolved = resolve_deployable_reference(str(link["reference"]), index_path, site_root)
        except ValueError as error:
            report.add_error(f"Article-index link at HTML line {link['line']}: {error}")
            continue
        if resolved and resolved[0] == article_html:
            linked = True
            break
    if not linked:
        report.add_error(
            f"Generated article index does not link to 'articles/{report.expected_article_slug}.html'"
        )

    records = deployment_image_records(repo_root, article_path, site_root, report)
    if records is None:
        return report
    featured_images, target_body_images = check_deployment_images(
        repo_root,
        article_path,
        report,
        records,
        sync_images=False,
    )

    index_image_paths = local_reference_paths(
        index_inspection.image_refs,
        index_path,
        site_root,
        report,
        "Generated article index",
    )
    for item in featured_images:
        destination = (repo_root / item["deploy_path"]).resolve()
        if destination not in index_image_paths:
            report.add_error(
                f"Generated article index does not reference the deployable featured image for "
                f"{item['article']}: {destination}"
            )
    for path in index_image_paths:
        if not path.is_file():
            report.add_error(f"Article-index image reference is missing from deployable output: {path}")

    article_image_paths = local_reference_paths(
        article_inspection.image_refs,
        article_html,
        site_root,
        report,
        "Generated article",
    )
    for item in target_body_images:
        destination = (repo_root / item["deploy_path"]).resolve()
        if destination not in article_image_paths:
            report.add_error(
                f"{item['syntax']} at body line {item['line']} was not referenced at its "
                f"deployable path in generated HTML: {item['reference']}"
            )
    for path in article_image_paths:
        if not path.is_file():
            report.add_error(f"Generated article image is missing from deployable output: {path}")

    validate_stylesheets(article_html, article_inspection, site_root, repo_root, report)
    validate_internal_links(article_html, article_inspection, site_root, report)
    try:
        metadata, _ = parse_front_matter(article_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        report.add_error(f"Could not re-read article CTA metadata: {error}")
    else:
        validate_article_cta(metadata, article_inspection.cta, report, "Generated article")
    if report.math["present"] and not article_inspection.math_signals:
        report.add_warning(
            "Source contains likely Quarto/Pandoc math, but generated HTML inspection could not "
            "confirm math markup or MathJax/KaTeX support. Quarto rendering remains authoritative; "
            "inspect the rendered equation manually."
        )
    return report


def build_argument_parser() -> argparse.ArgumentParser:
    """Create the command-line contract consumed by PowerShell and tests."""

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("source", "classify", "sync-images"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--repo-root", required=True, type=pathlib.Path)
        subparser.add_argument("--article", required=True, type=pathlib.Path)

    rendered = subparsers.add_parser("rendered")
    rendered.add_argument("--repo-root", required=True, type=pathlib.Path)
    rendered.add_argument("--article", required=True, type=pathlib.Path)
    rendered.add_argument("--template-sha256", required=True)
    return parser


def run_command(arguments: argparse.Namespace) -> ValidationReport:
    """Dispatch one explicit validator mode."""

    if arguments.command == "classify":
        return classify_article(arguments.repo_root, arguments.article)
    if arguments.command == "source":
        return validate_source(arguments.repo_root, arguments.article)
    if arguments.command == "sync-images":
        return synchronize_images(arguments.repo_root, arguments.article)
    return validate_rendered(
        arguments.repo_root,
        arguments.article,
        arguments.template_sha256,
    )


def main(argv: list[str] | None = None) -> int:
    """Emit exactly one JSON document and use exit status for hard failures."""

    parser = build_argument_parser()
    arguments = parser.parse_args(argv)
    try:
        report = run_command(arguments)
    except Exception as error:  # Keep the CLI JSON contract even for unexpected failures.
        report = ValidationReport(expected_article_slug=arguments.article.stem)
        report.add_error(f"Unexpected validator failure: {type(error).__name__}: {error}")
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2, default=str))
    return 0 if report.status == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
