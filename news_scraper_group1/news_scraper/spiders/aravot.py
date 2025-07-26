import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import redis
from datetime import datetime
import requests
import random
import time

class AravotSpider(scrapy.Spider):
    name = "aravot"
    allowed_domains = ["aravot.am"]
    start_urls = [
        "https://www.aravot.am/",
        "https://www.aravot.am/newsfeed/"
    ]
    
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
        super(AravotSpider, self).__init__(*args, **kwargs)
        
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

    def get_random_headers(self):
        """Generate random headers to avoid blocking"""
        user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'
        referers = [
            'https://www.google.com/',
            'https://www.facebook.com/',
            'https://www.bing.com/',
            'https://duckduckgo.com/',
            'https://t.me/',
            'https://www.youtube.com/'
        ]
        return {
            'User-Agent': user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'hy-AM,hy;q=0.9,en;q=0.8,ru;q=0.7',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'DNT': '1',
            'Cache-Control': 'max-age=0',
            'Referer': random.choice(referers)
        }

    def start_requests(self):
        """Override start_requests to use random headers"""
        for url in self.start_urls:
            time.sleep(random.uniform(1, 3))  # Random delay
            yield scrapy.Request(
                url=url,
                headers=self.get_random_headers(),
                callback=self.parse,
                dont_filter=True
            )

    def is_article_processed(self, url, title):
        """Check if article was already processed using Redis cache"""
        if not self.redis_client:
            return False
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_aravot:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_aravot:{article_hash}"
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
        # Handle 403 errors with retry
        if response.status == 403:
            self.logger.warning(f"🚫 403 Forbidden. Retrying with different headers...")
            time.sleep(random.uniform(5, 10))
            yield scrapy.Request(
                url=response.url,
                headers=self.get_random_headers(),
                callback=self.parse,
                dont_filter=True
            )
            return

        # Extract articles from the newsfeed structure
        # Based on the HTML structure provided: div.newsfeed-block-wrapper div.card
        articles = response.css("div.newsfeed-block-wrapper div.card")
        
        # Also try alternative selectors if the main one doesn't work
        if not articles:
            articles = response.css("div.card.mb-3")
        
        if not articles:
            articles = response.css("div.card")
        
        # Limit to latest 10 articles for optimization
        articles = articles[:10]
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված (սահմանափակված 10-ով)")

        for article in articles:
            # Extract link using the structure from provided HTML
            link = article.css("a.btn-link.text-reset.stretched-link::attr(href)").get()
            
            # If the main selector doesn't work, try alternatives
            if not link:
                link = article.css("h6 a::attr(href)").get()
            if not link:
                link = article.css("a[href*='/2025/']::attr(href)").get()
            if not link:
                link = article.css("a[href*='/2024/']::attr(href)").get()
            
            # Extract title
            title = article.css("a.btn-link.text-reset.stretched-link::text").get()
            if not title:
                title = article.css("h6 a::text").get()
            if not title:
                title = article.css("a::attr(title)").get()
            
            # Extract date
            date_info = article.css("li.nav-item::text").get()
            if date_info:
                date_info = date_info.strip()
            
            if link and title:
                # Ensure absolute URL
                full_url = response.urljoin(link) if not link.startswith('http') else link
                
                # Check Redis cache first
                if self.is_article_processed(full_url, title):
                    self.cached_skips += 1
                    continue
                
                # Add metadata
                meta = {
                    'scraped_time': date_info,
                    'preview_title': title
                }
                
                yield scrapy.Request(
                    full_url, 
                    callback=self.parse_article, 
                    meta=meta,
                    headers=self.get_random_headers()
                )

    def parse_article(self, response):
        self.processed_articles += 1

        # Get metadata from the main page
        preview_title = response.meta.get('preview_title', '')
        main_page_time = response.meta.get('scraped_time', '')

        # Try multiple title selectors for aravot.am
        title = (response.css("h1.entry-title::text").get() or
                response.css("h1::text").get() or
                response.css(".post-title::text").get() or
                response.css("meta[property='og:title']::attr(content)").get() or
                response.css("title::text").get() or
                preview_title)
        
        if title:
            title = title.strip()
            # Remove site name from title if present
            if " - Առավոտ" in title:
                title = title.replace(" - Առավոտ", "")
            if " | Առավոտ" in title:
                title = title.replace(" | Առավոտ", "")
        
        # Try multiple content selectors for aravot.am
        content_parts = (response.css("div.entry-content p::text").getall() or
                        response.css("div.post-content p::text").getall() or
                        response.css("div.article-content p::text").getall() or
                        response.css("div.content p::text").getall() or
                        response.css(".entry-content ::text").getall() or
                        response.css("p::text").getall())
        
        content = "\n".join([p.strip() for p in content_parts if p.strip()])

        # Extract scraped time - try multiple selectors
        scraped_time = (response.css('time::attr(datetime)').get() or
                       response.css('time::text').get() or 
                       response.css('.entry-date::text').get() or
                       response.css('.post-date::text').get() or
                       response.css('.date::text').get() or
                       main_page_time or
                       datetime.now().isoformat())

        # Clean title for display
        display_title = title[:60] + "..." if title and len(title) > 60 else title or "Անանուն հոդված"
        
        # Check for keywords
        full_text = f"{title or ''} {content}".strip()
        
        if self.article_contains_keyword(full_text):
            self.logger.info(f"✅ Բանալի բառ գտնվեց: {display_title}")
            # Mark as processed only after successful keyword match
            self.mark_article_processed(response.url, title)
            self.new_articles += 1
            
            item = NewsScraperItem()
            item['title'] = title or f'Article from {response.url.split("/")[-2] or response.url.split("/")[-1]}'
            item['link'] = response.url
            item['source_url'] = response.url
            item['content'] = content
            item['scraped_time'] = scraped_time
            yield item
        else:
            self.logger.info(f"❌ Բանալի բառ չգտնվեց: {display_title}")
            # Mark as processed even if no keyword match to avoid re-checking
            self.mark_article_processed(response.url, title)

    def closed(self, reason):
        """Called when the spider is closed"""
        self.logger.info(f"""
🏁 Առավոտ spider ավարտ:
📊 Ընդամենը մշակված: {self.processed_articles}
✅ Նոր հոդվածներ: {self.new_articles}
💨 Cache-ից բաց թողնված: {self.cached_skips}
🔄 Ավարտման պատճառ: {reason}
""") 