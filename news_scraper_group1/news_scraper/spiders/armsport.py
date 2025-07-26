import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import redis
from datetime import datetime
import requests

class ArmsportSpider(scrapy.Spider):
    name = "armsport"
    allowed_domains = ["armsport.am"]
    start_urls = ["https://armsport.am/", "https://armsport.am/lrahos/"]
    
    # Add custom headers to bypass potential blocking
    custom_settings = {
        'USER_AGENT': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'DEFAULT_REQUEST_HEADERS': {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        }
    }

    def __init__(self, *args, **kwargs):
        super(ArmsportSpider, self).__init__(*args, **kwargs)
        
        # Redis connection
        try:
            self.redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
            self.redis_client.ping()
            self.logger.info("🔴 Redis կապակցված է")
        except Exception as e:
            self.logger.warning(f"🔴 Redis չկա, կաշխատի առանց cache: {e}")
            self.redis_client = None

        # API client
        self.api_base_url = os.environ.get('API_BASE_URL', 'https://beackkayq.onrender.com')
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'User-Agent': 'NewsMonitor/1.0'
        })
        
        # Load keywords via API
        try:
            response = self.session.get(f"{self.api_base_url}/api/keywords/", timeout=10)
            if response.status_code == 200:
                keywords_data = response.json()
                self.keywords = [kw.get('word', '').lower() for kw in keywords_data]
                self.logger.info(f"🔑 Բանալի բառեր: {', '.join(self.keywords) if self.keywords else 'Չկա (բոլոր հոդվածները)'}")
            else:
                self.logger.warning(f"API keywords error: {response.status_code}")
                self.keywords = []
        except Exception as e:
            self.logger.warning(f"Բանալի բառերը չհաջողվեց բեռնել: {e}")
            self.keywords = []

        # Statistics
        self.processed_articles = 0
        self.new_articles = 0
        self.cached_skips = 0
        self.duplicate_articles = 0

    def is_article_processed(self, url, title):
        """Check if article was already processed using Redis cache"""
        if not self.redis_client:
            return False
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_armsport:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_armsport:{article_hash}"
        # Mark as processed (expire in 7 days)
        self.redis_client.setex(cache_key, 604800, "1")

    def article_contains_keyword(self, article_text):
        if not article_text:
            return False
        if not self.keywords:  # If no keywords, scrape all articles
            return True
        for keyword in self.keywords:
            if keyword in article_text.lower():
                return True
        return False

    def parse(self, response):
        # Extract articles from the sidebar news section and main content
        # Main sidebar news articles
        sidebar_articles = response.css("div#sidebar-news-main article.jeg_post")
        
        # Also try to get articles from main content area
        main_articles = response.css("article.jeg_post")
        
        # Combine both sets, remove duplicates
        all_articles = sidebar_articles + main_articles
        unique_articles = []
        seen_links = set()
        
        for article in all_articles:
            link = article.css("h3.jeg_post_title a::attr(href)").get()
            if link and link not in seen_links:
                unique_articles.append(article)
                seen_links.add(link)
        
        # Limit to latest 10 articles only for optimization (running every 10 minutes)
        unique_articles = unique_articles[:10]
        
        self.logger.info(f"🏅 Գտնվել է {len(unique_articles)} սպորտային հոդված (սահմանափակված 10-ով)")

        for article in unique_articles:
            # Extract link and title using armsport.am structure
            link = article.css("h3.jeg_post_title a::attr(href)").get()
            title_text = article.css("h3.jeg_post_title a::text").get()
            
            # Extract additional info for context
            date_info = article.css("div.jeg_meta_date a::text").get()
            category_classes = article.css("div.jeg_thumb::attr(class)").get()
            
            # Extract category from CSS classes (e.g., cat-football, cat-armenia)
            category = "Սպորտ"
            if category_classes:
                if "cat-football" in category_classes:
                    category = "Ֆուտբոլ"
                elif "cat-armenia" in category_classes:
                    category = "Հայաստան"
                elif "cat-basketball" in category_classes:
                    category = "Բասկետբոլ"
                elif "cat-tennis" in category_classes:
                    category = "Թենիս"
            
            if link and title_text:
                # Ensure absolute URL
                full_url = response.urljoin(link) if not link.startswith('http') else link
                
                # Check Redis cache first
                if self.is_article_processed(full_url, title_text):
                    self.cached_skips += 1
                    continue
                
                # Add metadata for context
                meta = {
                    'scraped_time': date_info,
                    'category': category,
                    'preview_title': title_text
                }
                    
                yield scrapy.Request(full_url, callback=self.parse_article, meta=meta)

    def parse_article(self, response):
        self.processed_articles += 1

        # Get metadata from the main page
        preview_title = response.meta.get('preview_title', '')
        category = response.meta.get('category', 'Սպորտ')
        main_page_time = response.meta.get('scraped_time', '')

        # Try multiple title selectors for armsport.am
        title = (response.css("h1.jeg_post_title::text").get() or
                response.css("h1::text").get() or
                response.css(".entry-header h1::text").get() or
                response.css(".post-title::text").get() or
                response.css("meta[property='og:title']::attr(content)").get() or
                response.css("title::text").get() or
                preview_title)
        
        # Try multiple content selectors for armsport.am
        content_parts = (response.css("div.content-inner p::text").getall() or
                        response.css("div.jeg_content p::text").getall() or
                        response.css(".entry-content p::text").getall() or
                        response.css(".post-content p::text").getall() or
                        response.css(".article-content p::text").getall() or
                        response.css("div.content-inner ::text").getall() or
                        response.css("p::text").getall())
        
        content = "\n".join([p.strip() for p in content_parts if p.strip()])

        # Extract scraped time - try multiple selectors
        scraped_time = (response.css('time::attr(datetime)').get() or
                       response.css('time::text').get() or 
                       response.css('.jeg_meta_date::text').get() or
                       response.css('.date::text').get() or 
                       response.css('.publish-date::text').get() or
                       response.css('.post-date::text').get() or
                       main_page_time or
                       datetime.now().isoformat())

        # Clean title for display
        display_title = title[:60] + "..." if title and len(title) > 60 else title or "Անանուն հոդված"
        
        # Check for keywords
        full_text = f"{title or ''} {content}".strip()
        
        if self.article_contains_keyword(full_text):
            self.logger.info(f"✅ Բանալի բառ գտնվեց: {display_title} ({category})")
            # Mark as processed only after successful keyword match
            self.mark_article_processed(response.url, title)
            self.new_articles += 1
            
            item = NewsScraperItem()
            item['title'] = title or f'Armsport հոդված - {category}'
            item['link'] = response.url
            item['source_url'] = response.url
            item['content'] = content
            item['scraped_time'] = scraped_time
            yield item
        else:
            self.logger.info(f"❌ Բանալի բառ չգտնվեց: {display_title} ({category})")
            # Mark as processed even if no keyword match to avoid re-checking
            self.mark_article_processed(response.url, title)

    def closed(self, reason):
        """Called when spider finishes"""
        self.logger.info(f"""
📊 ԱՄՓՈՓՈՒՄ ARMSPORT.AM (օպտիմիզացված - միայն 10 հոդված):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip()) 