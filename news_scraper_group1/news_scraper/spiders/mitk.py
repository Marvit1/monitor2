import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import requests
import redis
from datetime import datetime

    

class MitkSpider(scrapy.Spider):
    name = "mitk"
    allowed_domains = ["mitk.am"]
    start_urls = ["https://mitk.am/"]
    
    # Add duplicate_articles attribute
    duplicate_articles = 0
    
    # Add custom headers to bypass potential blocking
    custom_settings = {
        'USER_AGENT': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'DEFAULT_REQUEST_HEADERS': {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'hy-AM,hy;q=0.9,en-US;q=0.8,en;q=0.7,ru;q=0.6',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Referer': 'https://mitk.am/',
            'Cache-Control': 'max-age=0',
            'sec-ch-ua': '"Google Chrome";v="91", "Chromium";v="91", ";Not A Brand";v="99"',
            'sec-ch-ua-mobile': '?0',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-User': '?1',
        }
    }

    def __init__(self, *args, **kwargs):
        super(MitkSpider, self).__init__(*args, **kwargs)
        
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
        cache_key = f"processed_mitk:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_mitk:{article_hash}"
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
        # Extract articles from mitk.am structure
        articles = response.css("div#recent-posts-2 ul li")
        
        # Optimize: limit to only latest 10 articles
        articles = articles[:10]
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված (սահմանափակված 10-ով)")

        for article in articles:
            # Extract link and title using mitk.am structure
            link = article.css("a::attr(href)").get()
            title_preview = article.css("a::text").get()
            
            if link and title_preview:
                # Check Redis cache first
                if self.is_article_processed(link, title_preview):
                    self.cached_skips += 1
                    continue
                    
                yield scrapy.Request(link, callback=self.parse_article)

    def parse_article(self, response):
        self.processed_articles += 1

        # Extract title from mitk.am article page structure with hierarchy
        title = None
        title_selectors = [
            "h1.entry-title::text",
            "h1.post-title::text",
            "h1.article-title::text",
            ".entry-title::text",
            ".post-title::text",
            ".article-title::text",
            "article h1::text",
            ".main-content h1::text",
            ".content h1::text",
            "h1::text",
            "h2.entry-title::text",
            "h2.post-title::text",
            "h2::text",
            "meta[property='og:title']::attr(content)",
            "meta[name='title']::attr(content)",
            "title::text"
        ]
        
        for selector in title_selectors:
            title = response.css(selector).get()
            if title and title.strip():
                title = title.strip()
                # Only accept substantial titles that aren't navigation elements
                if (len(title) > 10 and 
                    not title.lower().startswith(('բաժին', 'մենյու', 'section', 'menu', 'home', 'գլխավոր')) and
                    not title.endswith(('...', '→', '»', '›', 'more', 'ավելին'))):
                    break
        
        # Extract content from mitk.am article page structure with improved filtering
        content_parts = []
        
        # Try article-specific content selectors first
        content_selectors = [
            "div.entry-content p::text",
            "div.post-content p::text",
            "div.article-content p::text",
            ".entry-content p::text",
            ".post-content p::text", 
            ".article-content p::text",
            "article .content p::text",
            "article p::text",
            ".main-content p::text",
            ".content p::text",
            "p::text"
        ]
        
        for selector in content_selectors:
            content_parts = response.css(selector).getall()
            if content_parts and len(content_parts) > 2:  # Need substantial content
                break
        
        # Clean and filter content extensively
        cleaned_content = []
        unwanted_fragments = [
            # Navigation and UI elements
            '•', '›', '»', '«', '‹', '→', '←', '↑', '↓', '▲', '▼', '◄', '►',
            # Social media and sharing
            'share', 'facebook', 'twitter', 'whatsapp', 'telegram', 'viber', 'linkedin',
            'կիսվել', 'տարածել', 'ուղարկել', 'պահպանել', 'տպել', 'print',
            # Navigation terms
            'գլխավոր', 'մենյու', 'բաժին', 'կատեգորիա', 'ետ', 'առաջ', 'հետ',
            'home', 'menu', 'back', 'next', 'previous', 'continue', 'section', 'category',
            # Footer elements
            'copyright', '©', 'բոլոր իրավունքները', 'all rights reserved',
            'կայքի իրավունքները', 'terms of use', 'privacy policy', 'legal',
            # Ads and sponsored content
            'advertisement', 'sponsored', 'реклама', 'գովազդ', 'promo',
            # Common website elements
            'read more', 'ավելին', 'continue reading', 'բացել', 'կարդալ ավելին',
            'search', 'որոնում', 'login', 'մուտք', 'register', 'գրանցվել',
            # Subscription and newsletter
            'subscribe', 'բաժանորդագրություն', 'newsletter', 'email',
            # Comments and feedback
            'comment', 'մեկնաբանություն', 'feedback', 'reply', 'պատասխանել',
            # Source attribution
            'source:', 'աղբյուր:', 'منبع:', 'источник:', 'via:', 'from:',
            # Armenian navigation terms
            'կարդալ ավելին', 'շարունակել', 'կարդալ', 'դիտել',
            'բոլոր նորությունները', 'այլ նորություններ', 'ամբողջական',
            # Time/date fragments that are not content
            'ժամ', 'րոպե', 'օր', 'ամիս', 'տարի', 'am', 'pm',
            # Website specific elements
            'mitk.am', 'mitk',
            # Author and byline elements
            'author:', 'հեղինակ:', 'by:', 'edited by:', 'խմբագիր:',
            # Rating and voting
            'rating', 'vote', 'գնահատել', 'քվեարկել',
            # Tags and categories
            'tags:', 'պիտակներ:', 'categories:', 'կատեգորիա:',
            # Related articles
            'related', 'հարակից', 'նմանատիպ', 'առնչվող'
        ]
        
        for part in content_parts:
            if part and part.strip():
                text = part.strip()
                # Filter out unwanted content with comprehensive checks
                if (len(text) > 10 and  # Minimum meaningful length
                    not any(unwanted in text.lower() for unwanted in unwanted_fragments) and
                    not text.isdigit() and  # Not just numbers
                    not text.lower().startswith(('բաժին', 'մենյու', 'navigation', 'menu', 'home', 'գլխավոր')) and
                    not text.endswith(('...', '→', '»', '›', 'more', 'ավելին')) and
                    not text.strip() in ['', '\n', '\t', '\r'] and
                    not text.startswith(('http://', 'https://')) and  # Not URLs
                    not text.lower().startswith(('click', 'կտտացնել', 'press', 'սեղմել')) and
                    len(text.split()) > 2):  # Must have multiple words
                    cleaned_content.append(text)
        
        content = "\n".join(cleaned_content[:15])  # Limit to first 15 paragraphs

        # Extract scraped time with mitk.am specific selectors
        scraped_time = (response.css('time::attr(datetime)').get() or
                       response.css('time::text').get() or 
                       response.css('.date::text').get() or 
                       response.css('.publish-date::text').get() or
                       response.css('.post-date::text').get() or
                       response.css('.entry-meta .posted-on::text').get() or
                       response.css('.entry-date::text').get() or
                       response.css('[class*="date"]::text').get() or
                       datetime.now().isoformat())

        # Clean title for display
        display_title = title[:60] + "..." if title and len(title) > 60 else title or "Անանուն հոդված"
        
        # Check for keywords in both title and content
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
📊 ԱՄՓՈՓՈՒՄ MITK.AM (օպտիմիզացված - միայն 10 հոդված):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Դուպլիկատներ: {self.duplicate_articles}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip()) 