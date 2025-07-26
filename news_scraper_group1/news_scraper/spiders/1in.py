import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import redis
from datetime import datetime, timedelta

import requests

class OneInSpider(scrapy.Spider):
    name = "1in"
    allowed_domains = ["1in.am"]
    start_urls = [
        "https://www.1in.am/",
        "https://www.1in.am/newsfeed",
        "https://www.1in.am/section/newsfeed/armenia",
        "https://www.1in.am/section/newsfeed/armenia/politics",
        "https://www.1in.am/section/newsfeed/armenia/foreignpolicy",
        "https://www.1in.am/section/newsfeed/armenia/economy",
        "https://www.1in.am/section/newsfeed/armenia/society",
        "https://www.1in.am/section/newsfeed/region",
        "https://www.1in.am/section/newsfeed/world",
        "https://www.1in.am/section/newsfeed/business",
        "https://www.1in.am/section/newsfeed/sport"
    ]
    
    # Enhanced headers for better scraping
    custom_settings = {
        'USER_AGENT': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'DEFAULT_REQUEST_HEADERS': {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'hy-AM,hy;q=0.9,en-US;q=0.8,en;q=0.7,ru;q=0.6',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Cache-Control': 'max-age=0',
        },
        'DOWNLOAD_DELAY': 1,
        'RANDOMIZE_DOWNLOAD_DELAY': True,
    }

    def __init__(self, *args, **kwargs):
        super(OneInSpider, self).__init__(*args, **kwargs)
        
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
        "Check if article was already processed using Redis cache"
        if not self.redis_client:
            return False
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_1in:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        "Mark article as processed in Redis cache"
        if not self.redis_client:
            return
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_1in:{article_hash}"
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
        # Extract articles using 1in.am structure
        articles = response.css("div.newsfeed-item")
        
        # Also look for other article patterns on the site
        if not articles:
            # Fallback selectors for different page layouts
            articles = (response.css("div.post-item") or 
                       response.css("article") or
                       response.css("div.item") or
                       response.css("div[id*='post-']"))
        
        # Limit to latest 10 articles only for optimization (running every 10 minutes)
        articles = articles[:10]
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված (սահմանափակված 10-ով) [{response.url}]")

        for article in articles:
            # Extract link using 1in.am structure - looking for actual article links
            link = article.css("a[href*='.html']::attr(href)").get()
            
            # If no .html link found, try general link extraction
            if not link:
                link = (article.css("a::attr(href)").get() or
                       article.css("div.item_title a::attr(href)").get() or
                       article.css("h3 a::attr(href)").get() or
                       article.css("h2 a::attr(href)").get())
            
            # Extract title preview from the article structure
            title_preview = (article.css("div.item_title::text").get() or
                           article.css("a div.item_title::text").get() or
                           article.css("h3::text").get() or
                           article.css("h2::text").get() or
                           article.css(".title::text").get())
            
            if link and title_preview:
                full_url = response.urljoin(link)
                
                # Only process actual article URLs (containing .html)
                if not ('.html' in full_url or '/3' in full_url):
                    continue
                
                # Check Redis cache first
                if self.is_article_processed(full_url, title_preview):
                    self.cached_skips += 1
                    continue
                    
                yield scrapy.Request(full_url, callback=self.parse_article, 
                                   meta={'article_date': article.css("div.item_date::text").get()})

        # Pagination removed - only processing latest 10 articles for optimization

    def parse_article(self, response):
        self.processed_articles += 1

        # Try multiple title selectors for 1in.am
        title = (response.css("h1.entry-title::text").get() or
                response.css("h1::text").get() or
                response.css(".post-title::text").get() or
                response.css(".article-title::text").get() or
                response.css("meta[property='og:title']::attr(content)").get() or
                response.css("title::text").get())
        
        # Try multiple content selectors for 1in.am
        content_parts = (response.css("div.entry-content ::text").getall() or
                        response.css("div.post-content ::text").getall() or
                        response.css("div.article-content ::text").getall() or
                        response.css("div.content ::text").getall() or
                        response.css("article ::text").getall() or
                        response.css(".main-content ::text").getall() or
                        response.css("div.text ::text").getall() or
                        response.css("p::text").getall())
        
        content = "\n".join([p.strip() for p in content_parts if p.strip()])

        # Extract scraped time - try multiple selectors
        scraped_time = (response.meta.get('article_date') or
                       response.css('time::attr(datetime)').get() or
                       response.css('time::text').get() or 
                       response.css('.date::text').get() or 
                       response.css('.publish-date::text').get() or 
                       response.css('.item_date::text').get() or
                       response.css('.post-date::text').get() or
                       datetime.now().isoformat())

        # Clean title for display
        display_title = title[:60] + "..." if title and len(title) > 60 else title or "Անանուն հոդված"
        
        if self.article_contains_keyword(title) or self.article_contains_keyword(content):
            self.logger.info(f"✅ Բանալի բառ գտնվեց: {display_title}")
            # Mark as processed only after successful keyword match
            self.mark_article_processed(response.url, title)
            self.new_articles += 1
            
            item = NewsScraperItem()
            item['title'] = title or f'Article from 1in.am'
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
        "Called when spider finishes"
        self.logger.info(f"""
📊 ԱՄՓՈՓՈՒՄ 1IN.AM (օպտիմիզացված - միայն 10 հոդված):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip())
