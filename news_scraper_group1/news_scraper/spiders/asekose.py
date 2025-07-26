import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import redis
from datetime import datetime
import requests

class AsekoseSpider(scrapy.Spider):
    name = "asekose"
    allowed_domains = ["asekose.am"]
    start_urls = ["http://asekose.am/am/"]
    
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
        super(AsekoseSpider, self).__init__(*args, **kwargs)
        
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
        self.duplicate_articles = 0  # For pipeline compatibility

    def is_article_processed(self, url, title):
        """Check if article was already processed using Redis cache"""
        if not self.redis_client:
            return False
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_asekose:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_asekose:{article_hash}"
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
        # Extract articles using the asekose.am structure
        # Articles are in the latest-news aside, within list-group items
        articles = response.css("aside#latest-news ul.list-group li.list-group-item")
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված (օպտիմիզացված - սահմանափակված 10-ով)")
        
        # Limit to latest 10 articles only for optimization (running every 10 minutes)
        articles = articles[:10]

        for article in articles:
            # Extract link and title using asekose.am structure
            link = article.css("a::attr(href)").get()
            title_preview = article.css("a::text").get()
            
            if link and title_preview:
                # Clean the title by removing extra whitespace and caret icons
                title_preview = title_preview.strip()
                full_url = response.urljoin(link)
                
                # Check Redis cache first
                if self.is_article_processed(full_url, title_preview):
                    self.cached_skips += 1
                    continue
                    
                yield scrapy.Request(full_url, callback=self.parse_article)

    def parse_article(self, response):
        self.processed_articles += 1

        # Extract article title - more precise selectors
        title = (response.css("h1.page-header::text").get() or
                response.css("h1::text").get() or
                response.css("meta[property='og:title']::attr(content)").get())
        
        # Extract main article content - precise selectors to avoid code blocks, footer, etc.
        content_parts = []
        
        # Try specific article content selectors for asekose.am
        main_content = (response.css("div.row div.col-md-8 p::text").getall() or
                       response.css("div.container div.row div.col-md-8 div p::text").getall() or
                       response.css("main p::text").getall() or
                       response.css("div.main-content p::text").getall())
        
        if main_content:
            content_parts = main_content
        else:
            # Fallback to broader selectors but exclude common non-content areas
            all_paragraphs = response.css("p::text").getall()
            # Filter out code blocks, navigation, footer and short texts
            content_parts = [p for p in all_paragraphs if p.strip() and 
                           len(p.strip()) > 15 and  # Skip very short texts
                           not any(skip_pattern in p.lower() for skip_pattern in 
                                 ['<?', '</', 'function', 'var ', 'document.', 'window.',
                                  'copyright', 'footer', 'menu', 'navigation', 'sidebar',
                                  'social', 'follow', 'subscribe', 'newsletter', 'share',
                                  'կապ', 'հետևել', 'բաժանորդագրում', 'բաժանել', 'footer',
                                  'email', '@', 'http://', 'https://']) and
                           not p.strip().startswith('<') and  # Exclude HTML tags
                           not p.strip().startswith('/')]    # Exclude closing tags
        
        content = "\n".join([p.strip() for p in content_parts if p.strip()])

        # Use current time as scraped_time (simplified)
        scraped_time = datetime.now().isoformat()

        # Clean title for display
        display_title = title[:60] + "..." if title and len(title) > 60 else title or "Անանուն հոդված"
        
        if self.article_contains_keyword(title) or self.article_contains_keyword(content):
            self.logger.info(f"✅ Բանալի բառ գտնվեց: {display_title}")
            # Mark as processed only after successful keyword match
            self.mark_article_processed(response.url, title)
            self.new_articles += 1
            
            item = NewsScraperItem()
            item['title'] = title or f'Article from {response.url.split("/")[-1]}'
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
        """Called when spider finishes"""
        self.logger.info(f"""
📊 ԱՄՓՈՓՈՒՄ ASEKOSE.AM (օպտիմիզացված - միայն հոդվածի տեքստ):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Կրկնություններ: {self.duplicate_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip()) 