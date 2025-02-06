import os
import shutil
import yaml
from datetime import datetime
from typing import Dict, List

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
        """Validate required files and directories exist"""
        if not os.path.exists(self.ARTICLES_QMD_PATH):
            print(f"⚠️ Error: '{self.ARTICLES_QMD_PATH}' not found.")
            return False
        if not os.path.exists(self.ARTICLES_DIR):
            os.makedirs(self.ARTICLES_DIR)
        if not os.path.exists(self.IMAGES_DIR):
            os.makedirs(self.IMAGES_DIR)
        if not os.path.exists(self.SITE_ARTICLES_DIR):
            os.makedirs(self.SITE_ARTICLES_DIR)
        return True

    def get_article_metadata(self, file_path: str) -> Dict:
        """Extract metadata from article file"""
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        try:
            start = content.find('---')
            end = content.find('---', start + 3)
            if start == -1 or end == -1:
                raise ValueError("Invalid YAML front matter format")
            metadata_str = content[start + 3:end].strip()
            return yaml.safe_load(metadata_str) or {}
        except Exception as e:
            print(f"⚠️ Error parsing metadata in {file_path}: {str(e)}")
            return {}

    def generate_featured_articles(self) -> str:
        """Generate HTML for featured articles section"""
        featured_html = """
        <div class="featured-articles-container">
            <div class="featured-articles-header">
                <h2 class="featured-articles-title">🔥 Featured Articles</h2>
            </div>
            <div class="featured-articles">
        """
        featured_articles = []
        for file in os.listdir(self.ARTICLES_DIR):
            if file.endswith('.qmd'):
                metadata = self.get_article_metadata(os.path.join(self.ARTICLES_DIR, file))
                if metadata and 'title' in metadata and metadata.get('title', '').strip() and metadata.get('featured', False):
                    featured_articles.append(metadata)

        featured_articles.sort(key=lambda x: str(x.get('date', '')), reverse=True)

        for article in featured_articles:
            image = article.get('image', '').strip()
            if not image:
                print(f"⚠️ Warning: No image specified for '{article.get('title', '')}'")
            featured_html += f"""
            <article class="article featured">
                <img src="{image}" alt="{article.get('title', '')}"
                     onerror="this.style.display='none'">
                <div class="article-content">
                    <h3><a href="{article.get('filename', '')}.html">
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

    def process_articles(self):
        """Main processing function"""
        if not self.validate_environment():
            return
        try:
            shutil.copyfile(self.ARTICLES_QMD_PATH, self.ARTICLES_QMD_BACKUP)
            print("✅ Backup created successfully")
            
            with open(self.ARTICLES_QMD_PATH, "r", encoding="utf-8") as file:
                content = file.read()
            featured_html = self.generate_featured_articles()
            
            new_content = content.replace("{{featured_articles}}", featured_html)
            
            with open(self.ARTICLES_QMD_PATH, "w", encoding="utf-8") as file:
                file.write(new_content)
            print("✅ Placeholders replaced successfully in articles.qmd!")
            
            os.system("quarto render --no-execute")
            print("✅ Quarto rendering completed!")

            # Ensure articles.html is placed in both locations
            site_articles_path = os.path.join(self.SITE_DIR, "articles.html")
            target_articles_path = os.path.join(self.ARTICLES_DIR, "articles.html")
            site_articles_folder_path = os.path.join(self.SITE_ARTICLES_DIR, "articles.html")

            if os.path.exists(site_articles_path):
                shutil.copy(site_articles_path, target_articles_path)
                shutil.copy(site_articles_path, site_articles_folder_path)
                print("✅ Copied articles.html to both articles/ and _site/articles/")

            else:
                print("⚠️ Error: articles.html was not found in _site/")

        except Exception as e:
            print(f"⚠️ Error during processing: {str(e)}")
            if os.path.exists(self.ARTICLES_QMD_BACKUP):
                shutil.move(self.ARTICLES_QMD_BACKUP, self.ARTICLES_QMD_PATH)
                print("✅ Restored backup due to error")
            return
        finally:
            if os.path.exists(self.ARTICLES_QMD_BACKUP):
                os.remove(self.ARTICLES_QMD_BACKUP)
                print("✅ Backup file removed")

if __name__ == "__main__":
    manager = ArticleManager()
    manager.process_articles()







