import os
import shutil
import subprocess
import sys
import yaml
import re
from typing import Dict


def configure_console_streams() -> None:
    """Avoid encoding failures when invoked directly from Windows PowerShell."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")


class ArticleManager:
    def __init__(self):
        self.ARTICLES_QMD_PATH = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "articles.qmd")
        )
        self.ARTICLES_QMD_BACKUP = self.ARTICLES_QMD_PATH + ".bak"
        self.ARTICLES_DIR = os.path.join(os.path.dirname(__file__), "..", "articles")
        self.IMAGES_DIR = os.path.join(os.path.dirname(__file__), "..", "images")
        self.SITE_DIR = os.path.join(os.path.dirname(__file__), "..", "_site")
        self.SITE_ARTICLES_DIR = os.path.join(self.SITE_DIR, "articles")

    def validate_environment(self) -> bool:
        if not os.path.exists(self.ARTICLES_QMD_PATH):
            print(f"ERROR: '{self.ARTICLES_QMD_PATH}' not found.", file=sys.stderr)
            return False
        if not os.path.exists(self.ARTICLES_DIR):
            os.makedirs(self.ARTICLES_DIR)
        if not os.path.exists(self.IMAGES_DIR):
            os.makedirs(self.IMAGES_DIR)
        return True

    def get_article_metadata(self, file_path: str) -> Dict:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        try:
            start = content.find('---')
            end = content.find('---', start + 3)
            if start == -1 or end == -1:
                raise ValueError("Invalid YAML front matter format")
            metadata_str = content[start + 3:end].strip()
            return yaml.safe_load(metadata_str) or {}
        except Exception as error:
            print(f"ERROR: Error parsing metadata in {file_path}: {error}", file=sys.stderr)
            return {}

    def slugify(self, text: str) -> str:
        return re.sub(r'[^a-zA-Z0-9-]', '', text.replace(' ', '-')).lower()

    def generate_featured_articles(self) -> str:
        featured_html = """
        <div class="featured-articles-container">
            <div class="featured-articles-header">
                <h2 class="featured-articles-title"> Featured Articles</h2>
            </div>
            <div class="featured-articles">
        """
        featured_articles = []
        for file in os.listdir(self.ARTICLES_DIR):
            if file.endswith('.qmd'):
                metadata = self.get_article_metadata(os.path.join(self.ARTICLES_DIR, file))
                if metadata and 'title' in metadata and metadata.get('title', '').strip() and metadata.get('featured', False):
                    metadata['filename'] = self.slugify(os.path.splitext(file)[0])
                    featured_articles.append(metadata)

        featured_articles.sort(key=lambda x: str(x.get('date', '')), reverse=True)

        for article in featured_articles:
            image = article.get('image', '').strip()
            if not image:
                print(
                    f"WARNING: No image specified for '{article.get('title', '')}'",
                    file=sys.stderr,
                )
            featured_html += f"""
            <article class="article featured">
                <img src="{image}" alt="{article.get('title', '')}"
                     onerror="this.style.display='none'">
                <div class="article-content">
                    <h3><a href="articles/{article['filename']}.html">
                    {article.get('title', '')}
                    </a></h3>
                    <p>{article.get('description', '')}</p>
                </div>
            </article>
            """

        featured_html += """
            </div>
        </div>
        """
        return featured_html

    def clean_old_articles(self):
        if os.path.exists(self.SITE_ARTICLES_DIR):
            shutil.rmtree(self.SITE_ARTICLES_DIR)
            os.makedirs(self.SITE_ARTICLES_DIR)
            print("OK: Cleaned old articles from _site/articles/")

    def process_articles(self):
        if not self.validate_environment():
            return 1

        exit_code = 0
        try:
            shutil.copyfile(self.ARTICLES_QMD_PATH, self.ARTICLES_QMD_BACKUP)
            print("OK: Backup created")

            with open(self.ARTICLES_QMD_PATH, "r", encoding="utf-8") as file:
                content = file.read()
            featured_html = self.generate_featured_articles()
            new_content = content.replace("{{featured_articles}}", featured_html)

            with open(self.ARTICLES_QMD_PATH, "w", encoding="utf-8") as file:
                file.write(new_content)
            print("OK: Placeholders replaced in articles.qmd")

            self.clean_old_articles()
            completed = subprocess.run(
                ["quarto", "render", "--no-execute"],
                cwd=os.path.dirname(self.ARTICLES_QMD_PATH),
                check=False,
            )
            if completed.returncode != 0:
                print(
                    f"ERROR: Quarto rendering failed with exit code {completed.returncode}.",
                    file=sys.stderr,
                )
                exit_code = completed.returncode or 1
            else:
                print("OK: Quarto rendering completed")

            site_articles_path = os.path.join(self.SITE_DIR, "articles.html")
            if not os.path.exists(site_articles_path):
                print("ERROR: articles.html was not found in _site/", file=sys.stderr)
                exit_code = exit_code or 1
            elif exit_code == 0:
                print("OK: articles.html generated in _site/")
        except Exception as error:
            print(f"ERROR: Error during processing: {error}", file=sys.stderr)
            exit_code = 1
        finally:
            if os.path.exists(self.ARTICLES_QMD_BACKUP):
                try:
                    shutil.move(self.ARTICLES_QMD_BACKUP, self.ARTICLES_QMD_PATH)
                    print("OK: Restored original articles.qmd")
                except Exception as error:
                    print(f"ERROR: Could not restore articles.qmd: {error}", file=sys.stderr)
                    exit_code = 1

        return exit_code


if __name__ == "__main__":
    configure_console_streams()
    manager = ArticleManager()
    raise SystemExit(manager.process_articles())
