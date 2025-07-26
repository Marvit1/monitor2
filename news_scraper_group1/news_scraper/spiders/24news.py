import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import redis
from datetime import datetime
import requests

class TwentyFourNewsSpider(scrapy.Spider):
    name = "24news"
    allowed_domains = ["24news.am"]
    start_urls = ["https://24news.am/"]
    
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
        super(TwentyFourNewsSpider, self).__init__(*args, **kwargs)
        
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
        """Check if article was already processed using Redis cache"""
        if not self.redis_client:
            return False
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_24news:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_24news:{article_hash}"
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
        # Extract articles from both "Լրահոս" (latest-list) and "Ամենաշատ դիտվածները" (most-viewed) sections
        latest_articles = response.css("ul.sidebar-list.latest-list li")
        most_viewed_articles = response.css("ul.sidebar-list.most-viewed li")
        
        all_articles = latest_articles + most_viewed_articles
        
        # Filter out the "newsfeed-link" item which is not an article
        articles = [article for article in all_articles if not article.css("a[href*='lrahos']")]
        
        # Limit to latest 10 articles only for optimization (running every 10 minutes)
        articles = articles[:10]
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված (սահմանափակված 10-ով) (Լրահոս + Ամենաշատ դիտվածները)")

        for article in articles:
            # Extract link and title using 24news.am structure
            link = article.css("p a::attr(href)").get()
            title_text = article.css("p a::text").get()
            
            # Extract additional info for context
            time_info = article.css("span.ll-time::text").get()
            category = article.css("a.ll-category::text").get()
            
            if link and title_text:
                # Ensure absolute URL
                full_url = response.urljoin(link) if not link.startswith('http') else link
                
                # Check Redis cache first
                if self.is_article_processed(full_url, title_text):
                    self.cached_skips += 1
                    continue
                
                # Add metadata for context
                meta = {
                    'scraped_time': time_info,
                    'category': category,
                    'preview_title': title_text
                }
                    
                yield scrapy.Request(full_url, callback=self.parse_article, meta=meta)

    def parse_article(self, response):
        self.processed_articles += 1

        # Get metadata from the main page
        preview_title = response.meta.get('preview_title', '')
        category = response.meta.get('category', '')
        main_page_time = response.meta.get('scraped_time', '')

        # Try multiple title selectors for 24news.am
        title = (response.css("h1.entry-title::text").get() or
                response.css("h1::text").get() or
                response.css(".post-title::text").get() or
                response.css(".article-title::text").get() or
                response.css("meta[property='og:title']::attr(content)").get() or
                response.css("title::text").get() or
                preview_title)
        
        # Try multiple content selectors for 24news.am
        content_parts = (response.css("div.entry-content p::text").getall() or
                        response.css(".post-content p::text").getall() or
                        response.css(".article-content p::text").getall() or
                        response.css(".content p::text").getall() or
                        response.css("article p::text").getall() or
                        response.css(".main-content p::text").getall() or
                        response.css("div.entry-content ::text").getall() or
                        response.css("p::text").getall())
        
        content = "\n".join([p.strip() for p in content_parts if p.strip()])

        # Extract scraped time - try multiple selectors
        scraped_time = (response.css('time::attr(datetime)').get() or
                       response.css('time::text').get() or 
                       response.css('.date::text').get() or 
                       response.css('.publish-date::text').get() or
                       response.css('.post-date::text').get() or
                       response.css('.entry-date::text').get() or
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
            item['title'] = title or f'24News Article - {category}'
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
📊 ԱՄՓՈՓՈՒՄ 24NEWS.AM (օպտիմիզացված - միայն 10 հոդված):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip()) 