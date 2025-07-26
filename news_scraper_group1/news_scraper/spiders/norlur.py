import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import requests
import redis
from datetime import datetime

    

class NorlurSpider(scrapy.Spider):
    name = "norlur"
    allowed_domains = ["norlur.am"]
    start_urls = ["https://norlur.am/"]
    
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
        super(NorlurSpider, self).__init__(*args, **kwargs)
        
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

    def is_article_processed(self, url, title):
        """Check if article already processed using Redis"""
        if not self.redis_client:
            return False
        
        cache_key = f"norlur_article:{hashlib.md5(f'{url}:{title}'.encode()).hexdigest()}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis with 7-day expiration"""
        if not self.redis_client:
            return
        
        cache_key = f"norlur_article:{hashlib.md5(f'{url}:{title}'.encode()).hexdigest()}"
        self.redis_client.setex(cache_key, 7 * 24 * 3600, "processed")  # 7 days

    def article_contains_keyword(self, article_text):
        """Check if article contains any keywords"""
        if not self.keywords:
            return True  # If no keywords defined, process all articles
        
        article_text_lower = article_text.lower()
        return any(keyword in article_text_lower for keyword in self.keywords)

    def parse(self, response):
        # Extract articles from the recent posts widget
        articles = response.css('div.widget_recent_entries ul li')
        
        # Optimize: limit to only latest 10 articles
        articles = articles[:10]
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված recent posts-ից (սահմանափակված 10-ով)")
        
        for article in articles:
            article_url = article.css('a::attr(href)').get()
            title = article.css('a::text').get()
            
            if article_url and title:
                # Make URL absolute if it's relative
                article_url = response.urljoin(article_url)
                title = title.strip()
                
                self.processed_articles += 1
                
                # Check if already processed
                if self.is_article_processed(article_url, title):
                    self.cached_skips += 1
                    self.logger.debug(f"⏭️ Արդեն պրոցեսինգ է անցել: {title[:50]}...")
                    continue
                
                self.logger.info(f"🔍 Նոր հոդված գտնվեց: {title[:50]}...")
                yield response.follow(article_url, self.parse_article, 
                                    meta={'title': title, 'source_url': article_url})

        # Also check for main content articles (limit to 5 to keep total reasonable)
        main_articles = response.css('article h2 a, article h3 a, .entry-title a')
        main_articles = main_articles[:5]
        
        self.logger.info(f"📰 Գտնվել է {len(main_articles)} հոդված main content-ից (սահմանափակված 5-ով)")
        
        for article in main_articles:
            article_url = article.css('::attr(href)').get()
            title = article.css('::text').get()
            
            if article_url and title:
                article_url = response.urljoin(article_url)
                title = title.strip()
                
                self.processed_articles += 1
                
                if self.is_article_processed(article_url, title):
                    self.cached_skips += 1
                    self.logger.debug(f"⏭️ Արդեն պրոցեսինգ է անցել: {title[:50]}...")
                    continue
                
                self.logger.info(f"🔍 Նոր հոդված գտնվեց (main): {title[:50]}...")
                yield response.follow(article_url, self.parse_article, 
                                    meta={'title': title, 'source_url': article_url})

    def parse_article(self, response):
        title = response.meta.get('title', '')
        source_url = response.meta.get('source_url', '')
        
        # Try different selectors for content extraction
        content_selectors = [
            'div.entry-content',
            'div.post-content',
            'article .content',
            'div.article-content',
            'div.single-content',
            'div[class*="content"]',
            'main article p',
            'article p'
        ]
        
        content = ""
        for selector in content_selectors:
            content_elements = response.css(selector)
            if content_elements:
                content = ' '.join(content_elements.css('::text').getall()).strip()
                if content:
                    break
        
        # If no content found, try extracting all paragraphs
        if not content:
            paragraphs = response.css('p::text').getall()
            content = ' '.join([p.strip() for p in paragraphs if p.strip()])
        
        # Clean content
        content = ' '.join(content.split())
        
        # Check if article contains keywords
        full_text = f"{title} {content}"
        if not self.article_contains_keyword(full_text):
            self.logger.debug(f"⏭️ Բանալի բառեր չգտնվեցին: {title[:50]}...")
            return
        
        self.new_articles += 1
        
        # Mark as processed
        self.mark_article_processed(source_url, title)
        
        # Create item
        item = NewsScraperItem()
        item['title'] = title
        item['content'] = content[:5000] if content else ""  # Limit content length
        item['source_url'] = source_url
        item['link'] = source_url  # Use link field as well
        item['scraped_time'] = datetime.now().isoformat()
        
        self.logger.info(f"✅ Հոդված պրոցեսինգ: {title[:50]}...")
        
        yield item

    def closed(self, reason):
        self.logger.info(f"""
🏁 Norlur.am spider ավարտված (օպտիմիզացված - 10+5 հոդված):
   📊 Ընդանմանը պրոցեսինգ: {self.processed_articles}
   ✅ Նոր հոդվածներ: {self.new_articles}
   ⏭️ Cache-ից բաց թողնված: {self.cached_skips}
   🔴 Redis: {'Կա' if self.redis_client else 'Չկա'}
   🔑 Բանալի բառեր: {len(self.keywords)}
   ⏹️ Պատճառ: {reason}
        """) 