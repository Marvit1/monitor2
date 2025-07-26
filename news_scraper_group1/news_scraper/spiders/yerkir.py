import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import requests
import redis
from datetime import datetime

    

class YerkirSpider(scrapy.Spider):
    name = "yerkir"
    allowed_domains = ["yerkir.am"]
    start_urls = ["https://yerkir.am/"]
    
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
        super(YerkirSpider, self).__init__(*args, **kwargs)
        
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
            self.logger.info(f"🔑 Բանալի բառեր: {', '.join(self.keywords) if self.keywords else 'Չկա (բոլոր հոդվածները)'}")

        # Statistics
        self.processed_articles = 0
        self.new_articles = 0
        self.duplicate_articles = 0  # Add missing counter used by pipeline
        self.cached_skips = 0

    def is_article_processed(self, url, title):
        """Check if article already processed using Redis"""
        if not self.redis_client:
            return False
        
        cache_key = f"yerkir_article:{hashlib.md5(f'{url}:{title}'.encode()).hexdigest()}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis with 7-day expiration"""
        if not self.redis_client:
            return
        
        cache_key = f"yerkir_article:{hashlib.md5(f'{url}:{title}'.encode()).hexdigest()}"
        self.redis_client.setex(cache_key, 7 * 24 * 3600, "processed")  # 7 days

    def article_contains_keyword(self, article_text):
        """Check if article contains any keywords"""
        if not self.keywords:
            return True  # If no keywords defined, process all articles
        
        article_text_lower = article_text.lower()
        return any(keyword in article_text_lower for keyword in self.keywords)

    def parse(self, response):
        # Extract articles using the yerkir.am structure
        articles = response.css('ul#ajaxfeed li a.lrahos-link.ajaxlink')
        
        # Optimize: limit to only latest 10 articles
        articles = articles[:10]
        
        for article in articles:
            article_url = article.css('::attr(href)').get()
            if article_url:
                # Make URL absolute if it's relative
                article_url = response.urljoin(article_url)
                
                # Extract title from the lrahos-bigtext div - corrected selector
                title = article.css('div.lrahos-bigtext div:nth-child(2)::text').get()
                if not title:
                    title = article.css('div.lrahos-bigtext div::text').get()
                if not title:
                    # Try alternative selector for title
                    title = article.css('div.titler a::text').get()
                
                if title:
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
                else:
                    self.logger.debug(f"❌ Title չգտնվեց URL-ի համար: {article_url}")
        
        # Log how many articles were found
        total_articles = len(articles)
        self.logger.info(f"📊 Գտնվեց {total_articles} հոդված yerkir.am-ում (սահմանափակված 10-ով)")

    def parse_article(self, response):
        title = response.meta.get('title', '')
        source_url = response.meta.get('source_url', '')
        
        # Improved content extraction for yerkir.am - avoid unwanted elements
        content_parts = []
        
        # First try to find main article content containers
        main_content = response.css('.single-content, .entry-content, .article-content, .news-content, .post-content, .content-area')
        
        if main_content:
            # Extract only paragraph text from main content area
            content_parts = main_content.css('p::text').getall()
            # If no paragraphs, try div text but be more selective
            if not content_parts:
                content_parts = main_content.css('div::text').getall()
        else:
            # Fallback selectors - be very specific to avoid navigation/sidebar content
            content_parts = (
                # Try article tag first
                response.css('article p::text').getall() or
                # Try main content areas
                response.css('.main p::text').getall() or
                response.css('.main-content p::text').getall() or
                response.css('.content p::text').getall() or
                # Try specific news content classes
                response.css('.news-body p::text').getall() or
                response.css('.article-text p::text').getall() or
                response.css('.article-body p::text').getall() or
                response.css('.single-post-content p::text').getall() or
                response.css('.post-body p::text').getall() or
                # Last resort - simple paragraph selector
                response.css('p::text').getall()
            )
        
        # Clean and filter content - remove short fragments that are likely navigation
        cleaned_content = []
        for part in content_parts:
            if part and part.strip():
                text = part.strip()
                # Only include text that's likely to be article content
                if (len(text) > 15 and  # Longer than navigation text
                    not text.lower().startswith(('բաժին', 'մենյու', 'navigation', 'menu', 'կիսվել', 'share', 'tags', 'category', 'բաժին', 'կարծիք', 'մեկնաբանություն')) and
                    not text.isdigit() and  # Not just numbers
                    not text.lower().strip() in ['read more', 'ավելին', 'continue reading', 'կարդալ ավելին', 'source', 'منبع', 'աղբյուր'] and
                    not text.count('|') > 2 and  # Not navigation breadcrumbs
                    not text.count('»') > 1 and  # Not navigation arrows
                    not text.count('→') > 1):    # Not navigation arrows
                    cleaned_content.append(text)
        
        # Join content and clean extra whitespace
        content = ' '.join(cleaned_content)
        content = ' '.join(content.split())  # Remove extra whitespace
        
        # Additional cleaning - remove common navigation patterns
        content = content.replace('Կիսվել Facebook-ում', '')
        content = content.replace('Կիսվել Twitter-ում', '')
        content = content.replace('Կիսվել Telegram-ում', '')
        content = content.replace('Տպել', '')
        content = content.replace('Print', '')
        content = content.replace('Share', '')
        content = content.replace('Կիսվել', '')
        
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
🏁 Yerkir.am spider ավարտված (օպտիմիզացված - միայն 10 հոդված):
   📊 Ընդանմանը պրոցեսինգ: {self.processed_articles}
   ✅ Նոր հոդվածներ: {self.new_articles}
   ⏭️ Cache-ից բաց թողնված: {self.cached_skips}
   🔴 Redis: {'Կա' if self.redis_client else 'Չկա'}
   🔑 Բանալի բառեր: {len(self.keywords)}
   ⏹️ Պատճառ: {reason}
        """) 