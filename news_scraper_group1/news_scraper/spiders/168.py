import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import redis
from datetime import datetime
import requests

class News168Spider(scrapy.Spider):
    name = "news168"
    allowed_domains = ["168.am"]
    
    # Add custom headers to bypass 403
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
        super(News168Spider, self).__init__(*args, **kwargs)
        
        # Redis connection
        try:
            self.redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
            self.redis_client.ping()
            self.logger.info("🔴 Redis կապակցված է")
        except Exception as e:
            self.logger.warning(f"🔴 Redis չկա, կաշխատի առանց cache: {e}")
            self.redis_client = None

        # API client
        self.api_base_url = "https://beackkayq.onrender.com"
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

        # Use first page only for optimization (running every 10 minutes)
        self.current_page = 1
        self.start_urls = ["https://168.am"]

    def is_article_processed(self, url, title):
        """Check if article was already processed using Redis cache"""
        if not self.redis_client:
            return False
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_168:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_168:{article_hash}"
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
        # Try multiple selectors for 168.am
        items = response.css('article') or response.css('.post') or response.css('.news-item') or response.css('.article')
        
        # If no articles found with common selectors, try generic ones
        if not items:
            items = response.css('div[class*="news"]') or response.css('div[class*="post"]') or response.css('div[class*="article"]')
        
        self.logger.info(f"📄 Էջ {self.current_page}՝ գտնվեց {len(items)} հոդված (օպտիմիզացված - սահմանափակված 10-ով)")
        
        # Limit to latest 10 articles only for optimization (running every 10 minutes)
        items = items[:10]

        found_new = False

        for article in items:
            # Try multiple link selectors
            link = article.css('a::attr(href)').get() or article.css('h2 a::attr(href)').get() or article.css('h3 a::attr(href)').get()
            title = article.css('a::text').get() or article.css('h2::text').get() or article.css('h3::text').get()
            
            if not link:
                continue
                
            # Make sure link is absolute
            if link.startswith('/'):
                link = f"https://168.am{link}"

            if self.is_article_processed(link, title):
                self.cached_skips += 1
                continue

            found_new = True
            yield scrapy.Request(link, callback=self.parse_article)

        # Skip pagination for optimization (only process first page with latest 10 articles)
        self.logger.info("📄 Pagination բաց թողնվում է օպտիմիզացիայի համար (միայն 10 վերջին հոդվածներ)")

    def parse_article(self, response):
        self.processed_articles += 1

        # Try multiple title selectors
        title = (response.css('h1::text').get() or 
                response.css('.entry-title::text').get() or
                response.css('.post-title::text').get() or
                response.css('title::text').get())
        
        # Try multiple content selectors
        content_parts = (response.css('.entry-content ::text').getall() or
                        response.css('.post-content ::text').getall() or
                        response.css('.content ::text').getall() or
                        response.css('article ::text').getall() or
                        response.css('p::text').getall())
        
        content = "\n".join([p.strip() for p in content_parts if p.strip()])

        scraped_time = response.css('time::text').get() or response.css('.date::text').get() or ""

        if self.article_contains_keyword(title) or self.article_contains_keyword(content):
            self.logger.info(f"✅ Բանալի բառ գտնվեց՝ {title[:60]}...")
            self.mark_article_processed(response.url, title)
            self.new_articles += 1

            item = NewsScraperItem()
            item['title'] = title or "Untitled"
            item['link'] = response.url
            item['source_url'] = response.url
            item['content'] = content
            item['scraped_time'] = scraped_time
            yield item
        else:
            self.logger.info(f"❌ Բանալի բառ չգտնվեց՝ {title[:60]}...")
            self.mark_article_processed(response.url, title)

    def closed(self, reason):
        self.logger.info(f"""
📊 ԱՄՓՈՓՈՒՄ 168.AM (օպտիմիզացված - միայն 10 հոդված):
   • Ստուգված: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Cache բաց թողումներ: {self.cached_skips}
        """.strip())
