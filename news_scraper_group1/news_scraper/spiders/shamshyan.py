import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import redis
from datetime import datetime
import requests

class ShamshyanSpider(scrapy.Spider):
    name = "shamshyan"
    allowed_domains = ["shamshyan.com"]
    start_urls = ["https://shamshyan.com/hy"]
    
    # Add custom headers to bypass potential blocking
    custom_settings = {
        'USER_AGENT': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'DEFAULT_REQUEST_HEADERS': {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'hy-AM,hy;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        }
    }

    def __init__(self, *args, **kwargs):
        super(ShamshyanSpider, self).__init__(*args, **kwargs)
        
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
        cache_key = f"processed_shamshyan:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_shamshyan:{article_hash}"
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
        # Extract articles from the news feed section using the provided HTML structure
        articles = response.css("ul.news-items li.item")
        
        # Optimize: limit to only latest 15 articles
        articles = articles[:15]
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված (սահմանափակված 15-ով)")

        for article in articles:
            # Extract link and title using shamshyan.com structure
            link = article.css("a.item-title::attr(href)").get()
            title = article.css("a.item-title span.xfont-medium::text").get()
            
            # Extract date from the additional information
            date_text = article.css("span.item-date::text").get()
            
            if link and title:
                full_url = response.urljoin(link)
                
                # Check Redis cache first
                if self.is_article_processed(full_url, title):
                    self.cached_skips += 1
                    continue
                    
                yield scrapy.Request(full_url, callback=self.parse_article, meta={'date_text': date_text})

    def parse_article(self, response):
        self.processed_articles += 1

        # Clean title extraction for shamshyan.com
        title = (response.css("h1::text").get() or
                response.css(".entry-title::text").get() or
                response.css(".post-title::text").get() or
                response.css(".article-title::text").get() or
                response.css(".title::text").get() or
                response.css("meta[property='og:title']::attr(content)").get() or
                response.css("title::text").get())
        
        # Clean title - remove site name if present
        if title:
            title = title.strip()
            if " - Shamshyan.com" in title:
                title = title.replace(" - Shamshyan.com", "")
            if " | Shamshyan.com" in title:
                title = title.replace(" | Shamshyan.com", "")
        
        # Clean content extraction - only main article text
        content_parts = []
        
        # Try to find main article container first
        main_container = (response.css("div.entry-content") or
                         response.css(".post-content") or
                         response.css(".article-content") or
                         response.css(".content") or
                         response.css("article") or
                         response.css(".main-content"))
        
        if main_container:
            # Extract only paragraph text from main container
            content_parts = main_container.css("p::text").getall()
            
            # If no paragraphs, try div text but be selective
            if not content_parts or len(content_parts) < 2:
                content_parts = main_container.css("div > *::text").getall()
        
        # Fallback to direct paragraph extraction
        if not content_parts or len(content_parts) < 2:
            content_parts = response.css("p::text").getall()
        
        # Clean and filter content parts
        cleaned_parts = []
        for part in content_parts:
            if not part or not part.strip():
                continue
                
            cleaned_part = part.strip()
            
            # Skip very short content
            if len(cleaned_part) < 10:
                continue
                
            # Skip navigation and UI elements
            if any(unwanted in cleaned_part.lower() for unwanted in [
                "share", "facebook", "twitter", "telegram", "հետևեք", "կիսվել", 
                "հեղինակային", "պաշտպանված", "copyright", "shamshyan.com",
                "կայքէջի", "վերարտադրումը", "արգելվում է", "կարդալ ավելին",
                "տեսնել ավելին", "ցույց տալ", "սեղմել", "մուտք", "ելք"
            ]):
                continue
                
            # Skip if contains ellipsis (truncated content)
            if "[…]" in cleaned_part or "..." in cleaned_part:
                continue
                
            cleaned_parts.append(cleaned_part)
            
            # Stop after collecting enough content
            if len(cleaned_parts) >= 5 and len("\n".join(cleaned_parts)) > 300:
                break
        
        content = "\n".join(cleaned_parts)
        
        # Validate content quality
        if not content or len(content.strip()) < 100:
            self.logger.info(f"❌ Շատ կարճ բովանդակություն ({len(content.strip()) if content else 0} նիշ): {title[:50] if title else 'Անանուն'}...")
            return
            
        # Check if content has enough meaningful words
        content_words = content.split()
        if len(content_words) < 20:  # Less than 20 words is likely not real article content
            self.logger.info(f"❌ Շատ քիչ բառեր ({len(content_words)} բառ): {title[:50] if title else 'Անանուն'}...")
            return

        # Use the date from the list page or try to extract from article page
        scraped_time = (response.meta.get('date_text') or
                       response.css('time::attr(datetime)').get() or
                       response.css('time::text').get() or 
                       response.css('.date::text').get() or 
                       response.css('.publish-date::text').get() or
                       response.css('.post-date::text').get() or
                       response.css('.entry-date::text').get() or
                       datetime.now().isoformat())

        # Clean title for display
        display_title = title[:60] + "..." if title and len(title) > 60 else title or "Անանուն հոդված"
        
        if self.article_contains_keyword(title) or self.article_contains_keyword(content):
            self.logger.info(f"✅ Բանալի բառ գտնվեց: {display_title}")
            self.logger.info(f"📄 Բովանդակության երկարություն: {len(content)} նիշ, {len(content.split())} բառ")
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
📊 ԱՄՓՈՓՈՒՄ SHAMSHYAN.COM (օպտիմիզացված - միայն 15 հոդված):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Կրկնվող հոդվածներ: {self.duplicate_articles}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip()) 