import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import requests
import redis
from datetime import datetime

    

class HraparaktvSpider(scrapy.Spider):
    name = "hraparaktv"
    allowed_domains = ["hraparaktv.am"]
    start_urls = ["https://hraparaktv.am/"]
    
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
        super(HraparaktvSpider, self).__init__(*args, **kwargs)
        
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
        cache_key = f"processed_hraparaktv:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_hraparaktv:{article_hash}"
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
        # Extract articles using hraparaktv.am structure
        # Look for post links in the main content area
        articles = (response.css("a[href*='/post/']") or
                   response.css(".post-item-small a") or
                   response.css(".recommended-posts a") or
                   response.css("h3.title a"))
        
        # Optimize: limit to only latest 10 articles
        articles = articles[:10]
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված (սահմանափակված 10-ով)")

        for article in articles:
            # Extract link
            link = article.css("::attr(href)").get()
            
            if link:
                full_url = response.urljoin(link)
                
                # Get title preview from link text or nearby elements
                title_preview = (article.css("::text").get() or 
                               article.css("::attr(title)").get() or 
                               article.xpath("text()").get() or
                               "Article")
                
                # Clean title preview
                if title_preview:
                    title_preview = title_preview.strip()
                    # Remove time stamps like "16:51 - "
                    if " - " in title_preview and title_preview.split(" - ")[0].replace(":", "").isdigit():
                        title_preview = " - ".join(title_preview.split(" - ")[1:])
                
                # Check Redis cache first
                if self.is_article_processed(full_url, title_preview):
                    self.cached_skips += 1
                    continue
                    
                yield scrapy.Request(full_url, callback=self.parse_article)

    def parse_article(self, response):
        self.processed_articles += 1
        
        self.logger.info(f"🔍 Պրոցեսինգ URL: {response.url}")

        # Specific title selectors for hraparaktv.am
        title = (response.css("h1::text").get() or
                response.css("h1 *::text").get() or
                response.css(".article-title::text").get() or
                response.css(".post-title::text").get() or
                response.css(".entry-title::text").get() or
                response.css(".title::text").get() or
                response.css(".page-title::text").get() or
                response.css("meta[property='og:title']::attr(content)").get() or
                response.css("title::text").get())
        
        # Improved content extraction for hraparaktv.am
        # Try multiple approaches to find content
        content_text = ""
        
        # Method 1: Try to find content directly in common containers
        content_selectors = [
            '.post-content p::text',
            '.article-content p::text',
            '.entry-content p::text',
            '.content p::text',
            '.single-content p::text',
            '.post-body p::text',
            '.article-body p::text',
            '.news-content p::text',
            '.main-content p::text',
            '.content-area p::text',
            # More general selectors
            'article p::text',
            '.main p::text',
            # Very general fallback
            'p::text'
        ]
        
        for selector in content_selectors:
            content_parts = response.css(selector).getall()
            if content_parts:
                # Clean and filter content
                cleaned_content = []
                for part in content_parts:
                    if part and part.strip():
                        text = part.strip()
                        # Filter out navigation, ads, etc.
                        if (len(text) > 20 and  # Meaningful content length
                            not text.lower().startswith(('մենյու', 'navigation', 'menu', 'կիսվել', 'share', 'tags', 'category', 'comment', 'login', 'register', 'բաժին', 'կարծիք', 'մեկնաբանություն')) and
                            not text.isdigit() and
                            'hraparaktv' not in text.lower() and
                            'copyright' not in text.lower() and
                            'developed by' not in text.lower() and
                            not text.count('|') > 3):  # Not navigation breadcrumbs
                            cleaned_content.append(text)
                
                if cleaned_content:
                    content_text = ' '.join(cleaned_content)
                    break
        
        # Method 2: If no content found, try extracting all visible text and filter
        if not content_text:
            # Get all text from the page and try to identify article content
            all_text = response.css('*::text').getall()
            potential_content = []
            
            for text in all_text:
                if text and text.strip():
                    clean_text = text.strip()
                    # Look for substantial text blocks that could be article content
                    if (len(clean_text) > 50 and  # Longer text blocks
                        not clean_text.lower().startswith(('menu', 'navigation', 'կիսվել', 'share', 'login', 'register', 'home', 'գլխավոր', 'մենյու', 'նորություններ', 'հոդվածներ', 'բաժին', 'կատեգորիա', 'tags', 'category', 'comment', 'մեկնաբանություն', 'հետադարձ կապ', 'contact', 'about', 'մեր մասին')) and
                        not clean_text.isdigit() and
                        'hraparaktv' not in clean_text.lower() and
                        'copyright' not in clean_text.lower() and
                        'developed by' not in clean_text.lower() and
                        not clean_text.count('|') > 3 and
                        not clean_text.count('»') > 2 and
                        not clean_text.count('→') > 2):
                        potential_content.append(clean_text)
            
            # Take the longest meaningful text blocks
            if potential_content:
                # Sort by length and take the longest ones
                potential_content.sort(key=len, reverse=True)
                content_text = ' '.join(potential_content[:3])  # Take top 3 longest blocks
        
        # Final cleanup
        if content_text:
            content_text = content_text.replace('Կիսվել Facebook-ում', '')
            content_text = content_text.replace('Կիսվել Twitter-ում', '')
            content_text = content_text.replace('Կիսվել Telegram-ում', '')
            content_text = content_text.replace('Տպել', '')
            content_text = content_text.replace('Print', '')
            content_text = content_text.replace('Share', '')
            content_text = content_text.replace('Կիսվել', '')
            content_text = ' '.join(content_text.split())  # Remove extra whitespace
        
        # Clean title
        if title:
            title = title.strip()
            # Remove time stamps from title like "16:51 - "
            if " - " in title and title.split(" - ")[0].replace(":", "").isdigit():
                title = " - ".join(title.split(" - ")[1:])

        # Extract scraped time - try multiple selectors
        scraped_time = (response.css('time::attr(datetime)').get() or
                       response.css('time::text').get() or 
                       response.css('.date::text').get() or 
                       response.css('.publish-date::text').get() or 
                       response.css('.post-date::text').get() or 
                       response.css('.article-date::text').get() or
                       response.css('[class*="date"]::text').get() or
                       datetime.now().isoformat())

        # Clean title for display
        display_title = title[:60] + "..." if title and len(title) > 60 else title or "Անանուն հոդված"
        
        # Debug info - show content length
        self.logger.info(f"📝 Բովանդակության երկարություն: {len(content_text)} նիշ")
        
        if self.article_contains_keyword(title) or self.article_contains_keyword(content_text):
            self.logger.info(f"✅ Բանալի բառ գտնվեց: {display_title}")
            # Mark as processed only after successful keyword match
            self.mark_article_processed(response.url, title)
            self.new_articles += 1
            
            item = NewsScraperItem()
            item['title'] = title or f'Article from {response.url.split("/")[-1]}'
            item['link'] = response.url
            item['source_url'] = response.url
            item['content'] = content_text
            item['scraped_time'] = scraped_time
            yield item
        else:
            self.logger.info(f"❌ Բանալի բառ չգտնվեց: {display_title}")
            # Mark as processed even if no keyword match to avoid re-checking
            self.mark_article_processed(response.url, title)

    def closed(self, reason):
        """Called when spider finishes"""
        self.logger.info(f"""
📊 ԱՄՓՈՓՈՒՄ HRAPARAKTV.AM (օպտիմիզացված - միայն 10 հոդված):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip()) 