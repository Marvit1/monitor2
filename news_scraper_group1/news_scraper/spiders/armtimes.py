import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import requests
import redis
from datetime import datetime

    

class ArmTimesSpider(scrapy.Spider):
    name = "armtimes"
    allowed_domains = ["armtimes.com"]
    start_urls = ["https://armtimes.com/hy"]
    
    # Add custom headers to bypass potential blocking
    custom_settings = {
        'USER_AGENT': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        'DEFAULT_REQUEST_HEADERS': {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9,hy;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Cache-Control': 'max-age=0',
        }
    }

    def __init__(self, *args, **kwargs):
        super(ArmTimesSpider, self).__init__(*args, **kwargs)
        
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
        cache_key = f"processed_armtimes:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_armtimes:{article_hash}"
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
        # Extract articles using the armtimes.com structure
        # Articles are in div.col.s12.lastnews div.item elements
        articles = response.css("div.col.s12.lastnews div.item")
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված (օպտիմիզացված - սահմանափակված 10-ով)")
        
        # Limit to latest 10 articles only for optimization (running every 10 minutes)
        articles = articles[:10]

        for article in articles:
            # Extract link and title using armtimes.com structure
            link = article.css("a.black-text.clamp-hidden-4::attr(href)").get()
            title_preview = article.css("a.black-text.clamp-hidden-4 p.marg-bot-0.clamp-hidden-4.marg-top-0::text").get()
            
            # Also try alternative selectors
            if not link:
                link = article.css("a::attr(href)").get()
            if not title_preview:
                title_preview = article.css("p::text").get()
            
            if link and title_preview:
                # Clean the title by removing extra whitespace
                title_preview = title_preview.strip()
                if len(title_preview) < 10:  # Skip too short titles
                    continue
                    
                full_url = response.urljoin(link)
                
                # Check Redis cache first
                if self.is_article_processed(full_url, title_preview):
                    self.cached_skips += 1
                    continue
                    
                yield scrapy.Request(full_url, callback=self.parse_article)

    def parse_article(self, response):
        self.processed_articles += 1

        # More specific title selectors for armtimes.com - avoid generic selectors
        title = (response.css("article h1::text").get() or
                response.css(".article-title::text").get() or
                response.css(".post-title::text").get() or
                response.css(".entry-title::text").get() or
                response.css("h1.main-title::text").get() or
                response.css("h1.title::text").get() or
                response.css("meta[property='og:title']::attr(content)").get())
        
        # More specific content selectors for armtimes.com - target only article content
        content_parts = (response.css("article .article-content p::text").getall() or
                        response.css("article .post-content p::text").getall() or
                        response.css("article .entry-content p::text").getall() or
                        response.css(".article-content p::text").getall() or
                        response.css(".post-content p::text").getall() or
                        response.css(".entry-content p::text").getall() or
                        response.css("article .content p::text").getall() or
                        response.css("main article p::text").getall() or
                        response.css(".article-body p::text").getall())
        
        # If no paragraphs found, try more specific div selectors but avoid navigation/footer
        if not content_parts:
            content_parts = (response.css("article .article-content ::text").getall() or
                            response.css("article .post-content ::text").getall() or
                            response.css("article .entry-content ::text").getall() or
                            response.css(".article-content ::text").getall() or
                            response.css(".post-content ::text").getall() or
                            response.css(".entry-content ::text").getall() or
                            response.css("main article ::text").getall())
        
        # Filter out navigation, footer, and other unwanted elements
        if content_parts:
            # Remove text from navigation, footer, sidebar, comments, etc.
            filtered_content = []
            for text in content_parts:
                text = text.strip()
                # Skip empty text, navigation items, and common unwanted elements
                if (text and 
                    len(text) > 5 and  # Skip very short text
                    not text.startswith('•') and  # Skip bullet points from navigation
                    not text.startswith('→') and  # Skip navigation arrows
                    not text.startswith('►') and  # Skip navigation arrows
                    not text.startswith('»') and  # Skip navigation arrows
                    not text.lower().startswith('կարդալ') and  # Skip "read more" links
                    not text.lower().startswith('share') and  # Skip share buttons
                    not text.lower().startswith('comment') and  # Skip comment sections
                    not text.lower().startswith('բաց') and  # Skip "open" links
                    not text.lower().startswith('menu') and  # Skip menu items
                    not text.lower().startswith('search') and  # Skip search elements
                    not text.lower().startswith('տեղեկատվական') and  # Skip info sections
                    not text.lower().startswith('գովազդ') and  # Skip ads
                    not text.lower().startswith('advertising') and  # Skip ads
                    not text.lower().startswith('advertisement') and  # Skip ads
                    not text.lower().startswith('sponsored') and  # Skip sponsored content
                    not text.lower().startswith('related') and  # Skip related articles
                    not text.lower().startswith('see also') and  # Skip see also links
                    not text.lower().startswith('read also') and  # Skip read also links
                    'facebook' not in text.lower() and  # Skip social media
                    'twitter' not in text.lower() and
                    'instagram' not in text.lower() and
                    'telegram' not in text.lower() and
                    'youtube' not in text.lower() and
                    'whatsapp' not in text.lower() and
                    'linkedin' not in text.lower() and
                    'tiktok' not in text.lower() and
                    'copyright' not in text.lower() and  # Skip copyright
                    '©' not in text and  # Skip copyright symbol
                    'terms' not in text.lower() and  # Skip terms
                    'privacy' not in text.lower() and  # Skip privacy policy
                    'cookie' not in text.lower() and  # Skip cookie policy
                    'subscribe' not in text.lower() and  # Skip subscription calls
                    'newsletter' not in text.lower()):  # Skip newsletter signup
                    filtered_content.append(text)
            
            content = "\n".join(filtered_content)
        else:
            content = ""

        # Extract scraped time - more specific selectors
        scraped_time = (response.css('article time::attr(datetime)').get() or
                       response.css('article .date::text').get() or
                       response.css('article span.datetext::text').get() or
                       response.css('.post-date::text').get() or
                       response.css('.entry-date::text').get() or
                       response.css('.article-date::text').get() or
                       response.css('span.datetext::text').get() or
                       response.css('time::text').get() or
                       datetime.now().isoformat())

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
📊 ԱՄՓՈՓՈՒՄ ARMTIMES.COM (օպտիմիզացված - միայն 10 հոդված):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip())
