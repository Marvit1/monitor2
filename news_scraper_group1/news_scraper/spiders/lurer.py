import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import requests
import redis
from datetime import datetime

    

class LurerSpider(scrapy.Spider):
    name = "lurer"
    allowed_domains = ["lurer.com"]
    start_urls = ["https://lurer.com/"]
    
    # Add duplicate_articles attribute
    duplicate_articles = 0
    
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
        super(LurerSpider, self).__init__(*args, **kwargs)
        
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

    def is_article_processed(self, url):
        """Check if article was already processed using Redis cache"""
        if not self.redis_client:
            return False
        cache_key = f"processed_lurer:{hashlib.md5(url.encode()).hexdigest()}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        cache_key = f"processed_lurer:{hashlib.md5(url.encode()).hexdigest()}"
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
        # Extract article links - more specific patterns for lurer.com
        article_links = []
        
        # Try multiple selectors to find article links
        selectors = [
            "a[href*='/article/']",
            "a[href*='/news/']", 
            "a[href*='/post/']",
            "a[href*='/2025/']",
            "a[href*='/2024/']",
            ".article-link",
            ".news-link",
            ".post-link"
        ]
        
        for selector in selectors:
            links = response.css(selector)
            if links:
                article_links.extend(links)
                break
        
        # Remove duplicates by href
        seen_urls = set()
        unique_links = []
        for link in article_links:
            href = link.css("::attr(href)").get()
            if href and href not in seen_urls:
                seen_urls.add(href)
                unique_links.append(link)
        
        # Optimize: limit to only latest 10 articles
        unique_links = unique_links[:10]
        
        self.logger.info(f"📰 Գտնվել է {len(unique_links)} հոդված (սահմանափակված 10-ով)")

        for link in unique_links:
            href = link.css("::attr(href)").get()
            if href:
                full_url = response.urljoin(href)
                
                # Check Redis cache first
                if self.is_article_processed(full_url):
                    self.cached_skips += 1
                    continue
                    
                yield scrapy.Request(full_url, callback=self.parse_article)

    def parse_article(self, response):
        self.processed_articles += 1

        # Extract title - prioritize article-specific selectors
        title = (response.css("h1.article-title::text").get() or
                response.css("h1.post-title::text").get() or
                response.css("h1.entry-title::text").get() or
                response.css("h1.news-title::text").get() or
                response.css("article h1::text").get() or
                response.css(".content h1::text").get() or
                response.css(".main h1::text").get() or
                response.css("h1::text").get() or
                response.css("meta[property='og:title']::attr(content)").get() or
                response.css("title::text").get())

        # Extract content - focus on main article content only
        content_parts = []
        
        # Try article-specific content selectors first
        article_content_selectors = [
            "article .content p::text",
            "article .article-content p::text", 
            "article .post-content p::text",
            "article .entry-content p::text",
            ".article-body p::text",
            ".news-body p::text",
            ".post-body p::text",
            ".content-area p::text",
            ".main-content p::text",
            ".article-text p::text"
        ]
        
        for selector in article_content_selectors:
            content_parts = response.css(selector).getall()
            if content_parts:
                break
        
        # Fallback to more general selectors if needed
        if not content_parts:
            content_parts = response.css("article p::text").getall()
        
        if not content_parts:
            content_parts = response.css(".main p::text").getall()
        
        # Clean and filter content to remove unwanted elements
        cleaned_content = []
        unwanted_fragments = [
            # Navigation and UI elements
            '•', '›', '»', '«', '‹', '→', '←', '↑', '↓',
            # Social media and sharing
            'share', 'facebook', 'twitter', 'whatsapp', 'telegram', 'viber',
            'կիսվել', 'տարածել', 'ուղարկել', 'պահպանել',
            # Navigation terms
            'գլխավոր', 'մենյու', 'բաժին', 'կատեգորիա', 'ետ', 'առաջ',
            'home', 'menu', 'back', 'next', 'previous', 'continue',
            # Footer elements
            'copyright', '©', 'բոլոր իրավունքները', 'all rights reserved',
            'կայքի իրավունքները', 'terms of use', 'privacy policy',
            # Ads and sponsored content
            'advertisement', 'sponsored', 'реклама', 'գովազդ',
            # Common website elements
            'read more', 'ավելին', 'continue reading', 'բացել',
            'search', 'որոնում', 'login', 'մուտք',
            # Subscription and newsletter
            'subscribe', 'բաժանորդագրություն', 'newsletter',
            # Comments and feedback
            'comment', 'մեկնաբանություն', 'feedback',
            # Source attribution
            'source:', 'աղբյուր:', 'منبع:', 'источник:',
            # Armenian navigation terms
            'կարդալ ավելին', 'շարունակել', 'կարդալ',
            'բոլոր նորությունները', 'այլ նորություններ',
            # Time/date fragments that are not content
            'ժամ', 'րոպե', 'օր', 'ամիս',
            # Empty or single character fragments
            ' ', '\n', '\t', '\r'
        ]
        
        for part in content_parts:
            if part and part.strip():
                text = part.strip()
                # Filter out unwanted content
                if (len(text) > 5 and  # Minimum length
                    not any(unwanted in text.lower() for unwanted in unwanted_fragments) and
                    not text.isdigit() and  # Not just numbers
                    not text.lower().startswith(('բաժին', 'մենյու', 'navigation', 'menu')) and
                    not text.endswith(('...', '→', '»', '›')) and
                    not text.strip() in ['', '\n', '\t', '\r']):
                    cleaned_content.append(text)
        
        content = "\n".join(cleaned_content)

        # Extract time with more specific selectors
        scraped_time = (response.css('time::attr(datetime)').get() or
                       response.css('time::text').get() or 
                       response.css('.article-date::text').get() or
                       response.css('.post-date::text').get() or
                       response.css('.publish-date::text').get() or
                       response.css('.date::text').get() or
                       response.css('[class*="date"]::text').get() or
                       datetime.now().isoformat())

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
📊 ԱՄՓՈՓՈՒՄ LURER.COM (օպտիմիզացված - միայն 10 հոդված):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Դուպլիկատներ: {self.duplicate_articles}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip())