#!/usr/bin/env python3
"""
Script to update all remaining spiders that still use Django models
"""

import os
import re
import glob

def update_spider_file(file_path):
    """Update a single spider file to use API instead of Django models"""
    
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Check if this spider already uses API
    if 'self.api_base_url = "https://beackkayq.onrender.com"' in content:
        print(f"⏭️ {file_path} already uses API, skipping...")
        return False
    
    # Get spider name from class definition
    spider_name_match = re.search(r'class\s+(\w+)Spider', content)
    if not spider_name_match:
        print(f"⚠️ Could not find spider class in {file_path}")
        return False
    
    spider_name = spider_name_match.group(1).lower()
    
    # Remove Django imports and setup
    content = re.sub(r'import\s+django.*?\n', '', content)
    content = re.sub(r'from\s+django\.core\.wsgi\s+import.*?\n', '', content)
    content = re.sub(r'#\s*Setup\s+Django\s+environment.*?\n', '', content)
    content = re.sub(r'current_dir\s*=.*?\n', '', content)
    content = re.sub(r'backend_path\s*=.*?\n', '', content)
    content = re.sub(r'if\s+backend_path\s+not\s+in\s+sys\.path.*?\n', '', content)
    content = re.sub(r'sys\.path\.insert\(0,\s+backend_path\).*?\n', '', content)
    content = re.sub(r'os\.environ\.setdefault.*?\n', '', content)
    content = re.sub(r'django\.setup\(\).*?\n', '', content)
    content = re.sub(r'application\s*=.*?\n', '', content)
    
    # Remove Django model imports
    content = re.sub(r'from\s+main\.models\s+import.*?\n', '', content)
    
    # Add requests import if not present
    if 'import requests' not in content:
        content = re.sub(r'(import\s+hashlib)', r'\1\nimport requests', content)
    
    # Replace Django keyword loading with API
    django_keyword_pattern = r'self\.keywords\s*=\s*\[kw\.word\.lower\(\)\s+for\s+kw\s+in\s+Keyword\.objects\.all\(\)\]'
    api_keyword_replacement = f'''        # API client
        self.api_base_url = "https://beackkayq.onrender.com"
        self.session = requests.Session()
        self.session.headers.update({{
            'Content-Type': 'application/json',
            'User-Agent': 'NewsMonitor/1.0'
        }})
        
        # Load keywords via API
        try:
            response = self.session.get(f"{{self.api_base_url}}/api/keywords/", timeout=10)
            if response.status_code == 200:
                keywords_data = response.json()
                self.keywords = [kw.get('word', '').lower() for kw in keywords_data]
                self.logger.info(f"🔑 Բանալի բառեր: {{', '.join(self.keywords) if self.keywords else 'Չկա (բոլոր հոդվածները)'}}")
            else:
                self.logger.warning(f"API keywords error: {{response.status_code}}")
                self.keywords = []
        except Exception as e:
            self.logger.warning(f"Բանալի բառերը չհաջողվեց բեռնել: {{e}}")
            self.keywords = []'''
    
    content = re.sub(django_keyword_pattern, api_keyword_replacement, content)
    
    # Add duplicate_articles counter if missing
    if 'self.duplicate_articles = 0' not in content:
        content = re.sub(r'(self\.cached_skips\s*=\s*0)', r'\1\n        self.duplicate_articles = 0', content)
    
    # Write updated content back to file
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    print(f"✅ Updated {file_path}")
    return True

def main():
    """Main function to update all remaining spider files"""
    
    # Get all spider files
    spiders_dir = "news_scraper_group1/news_scraper/spiders"
    spider_files = glob.glob(os.path.join(spiders_dir, "*.py"))
    
    # Filter out __init__.py and files that already use API
    spider_files = [f for f in spider_files if not f.endswith('__init__.py')]
    
    print(f"🔍 Found {len(spider_files)} spider files to check")
    
    updated_count = 0
    skipped_count = 0
    
    for spider_file in spider_files:
        try:
            if update_spider_file(spider_file):
                updated_count += 1
            else:
                skipped_count += 1
        except Exception as e:
            print(f"❌ Error updating {spider_file}: {e}")
    
    print(f"\n📊 Summary:")
    print(f"✅ Successfully updated: {updated_count} spider files")
    print(f"⏭️ Already using API: {skipped_count} spider files")
    print("🎉 All spiders now use the same API structure as 24news.py")

if __name__ == "__main__":
    main() 