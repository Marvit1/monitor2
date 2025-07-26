import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import redis
from datetime import datetime
import requests

class AuroraNewsSpider(scrapy.Spider):
    name = "auroranews"
    allowed_domains = ["auroranews.am"]
    start_urls = ["https://auroranews.am/"]
    
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
        super(AuroraNewsSpider, self).__init__(*args, **kwargs)
        
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

    def is_article_processed(self, url, title=""):
        """Check if article was already processed using Redis cache"""
        if not self.redis_client:
            return False
        # Create unique hash for article - use URL only for consistency
        article_hash = hashlib.md5(url.encode()).hexdigest()
        cache_key = f"processed_auroranews:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title=""):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        # Create unique hash for article - use URL only for consistency
        article_hash = hashlib.md5(url.encode()).hexdigest()
        cache_key = f"processed_auroranews:{article_hash}"
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
        # Extract articles using the auroranews.am structure
        # Look for actual article links - try multiple selectors
        article_links = (response.css("article a[href*='/']::attr(href)").getall() or
                        response.css("a[href*='/news/']::attr(href)").getall() or
                        response.css("a[href*='/politics/']::attr(href)").getall() or
                        response.css("a[href*='/society/']::attr(href)").getall() or
                        response.css("a[href*='/world/']::attr(href)").getall() or
                        response.css("a[href*='/sport/']::attr(href)").getall() or
                        response.css("a[href*='/economy/']::attr(href)").getall() or
                        response.css("a[href*='/culture/']::attr(href)").getall() or
                        response.css("a[href*='/health/']::attr(href)").getall() or
                        response.css("a[href*='/tech/']::attr(href)").getall() or
                        response.css("a[href*='/2024/']::attr(href)").getall() or
                        response.css("a[href*='/2025/']::attr(href)").getall())
        
        # Remove duplicates and invalid links
        unique_links = []
        seen = set()
        for link in article_links:
            if link and link not in seen:
                # Skip non-article links
                if any(skip in link.lower() for skip in ['javascript:', 'mailto:', '#', 'facebook.com', 'twitter.com', 'instagram.com', 'telegram.me', 'youtube.com']):
                    continue
                if link.startswith('/') or 'auroranews.am' in link:
                    seen.add(link)
                    unique_links.append(link)
        
        self.logger.info(f"📰 Գտնվել է {len(unique_links)} հոդված (օպտիմիզացված - սահմանափակված 10-ով)")
        
        # Limit to latest 10 articles only for optimization (running every 10 minutes)
        unique_links = unique_links[:10]

        for link in unique_links:
            full_url = response.urljoin(link)
            
            # Check Redis cache first with URL only
            if self.is_article_processed(full_url, ""):
                self.cached_skips += 1
                continue
                
            yield scrapy.Request(full_url, callback=self.parse_article)

    def parse_article(self, response):
        self.processed_articles += 1

        # Extract article title - more precise selectors
        title = (response.css("article h1::text").get() or
                response.css("h1.article-title::text").get() or
                response.css("h1::text").get() or
                response.css("meta[property='og:title']::attr(content)").get())
        
        # Extract main article content - focus only on main article paragraphs
        content_parts = []
        
        # Try specific article content selectors for auroranews.am
        main_content = (response.css("article .article-content p::text").getall() or
                       response.css("article .post-content p::text").getall() or
                       response.css("article .entry-content p::text").getall() or
                       response.css("main article p::text").getall() or
                       response.css(".article-content p::text").getall() or
                       response.css(".post-content p::text").getall())
        
        if main_content:
            content_parts = main_content
        else:
            # Fallback but filter strictly
            all_paragraphs = response.css("p::text").getall()
            # Very strict filtering to avoid news lists and navigation
            content_parts = [p for p in all_paragraphs if p.strip() and 
                           len(p.strip()) > 20 and  # Only longer text paragraphs
                           not any(skip_pattern in p.lower() for skip_pattern in 
                                 ['կարդալ ավելին', 'read more', 'share', 'comment', 'menu',
                                  'search', 'follow', 'subscribe', 'newsletter', 'social',
                                  'facebook', 'twitter', 'instagram', 'telegram', 'youtube',
                                  'copyright', '©', 'terms', 'privacy', 'cookie',
                                  'source:', 'photo:', 'image:', 'advertisement', 'sponsored',
                                  'related', 'see also', 'read also', 'contact',
                                  'այլ լուրեր', 'նոր նյութեր', 'հետևյալ', 'ետևյալ',
                                  'կապ', 'հետևել', 'բաժանորդագրում', 'բաժանել',
                                  'գովազդ', 'տեղեկատվական', 'bookmark', 'print']) and
                           not p.strip().startswith('•') and  # No bullet points
                           not p.strip().startswith('→') and  # No arrows
                           not p.strip().startswith('►') and
                           not p.strip().startswith('»') and
                           not p.strip().startswith('›') and
                           not p.strip().startswith('◆')]
        
        content = "\n".join([p.strip() for p in content_parts if p.strip()])

        # Use current time as scraped_time (simplified)
        scraped_time = datetime.now().isoformat()

        # Clean title for display
        display_title = title[:60] + "..." if title and len(title) > 60 else title or "Անանուն հոդված"
        
        if self.article_contains_keyword(title) or self.article_contains_keyword(content):
            self.logger.info(f"✅ Բանալի բառ գտնվեց: {display_title}")
            # Mark as processed only after successful keyword match
            self.mark_article_processed(response.url)
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
            self.mark_article_processed(response.url)

    def closed(self, reason):
        """Called when spider finishes"""
        self.logger.info(f"""
📊 ԱՄՓՈՓՈՒՄ AURORANEWS.AM (օպտիմիզացված - միայն հոդվածի տեքստ):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Կրկնություններ: {self.duplicate_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip()) 