import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import redis
from datetime import datetime
import requests

class ArminfoSpider(scrapy.Spider):
    name = "arminfo"
    allowed_domains = ["arminfo.info"]
    start_urls = ["https://arminfo.info/index.php?lang=1"]
    
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
        super(ArminfoSpider, self).__init__(*args, **kwargs)
        
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
        cache_key = f"processed_arminfo:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_arminfo:{article_hash}"
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
        # Extract articles from arminfo.info structure
        # Looking for links in tables and various content areas
        
        # Try to find all links that contain "full_news" (which are article links)
        all_article_links = response.css("a[href*='full_news']")
        
        self.logger.info(f"📰 Գտնվել է {len(all_article_links)} հոդվածի հղում")
        
        # Debug info removed for optimization - processing only latest 10 articles
        
        unique_articles = []
        seen_links = set()
        
        for article_link in all_article_links:
            link = article_link.css("::attr(href)").get()
            title_text = article_link.css("::text").get()
            
            if link and link not in seen_links:
                unique_articles.append(article_link)
                seen_links.add(link)
        
        # Limit to latest 10 articles only for optimization (running every 10 minutes)
        unique_articles = unique_articles[:10]
        
        self.logger.info(f"📰 Uნიकալური հոդվածներ: {len(unique_articles)} (սահմանափակված 10-ով)")

        for article_link in unique_articles:
            # Extract link and title from the article link itself
            link = article_link.css("::attr(href)").get()
            title_text = article_link.css("::text").get()
            
            # Try to find parent container for more context (image, etc.)
            parent_container = article_link.xpath('./ancestor::td[1]').get()
            img_src = None
            img_alt = None
            
            if parent_container:
                # Try to extract image from the same table cell
                img_element = article_link.xpath('./ancestor::td[1]//img').get()
                if img_element:
                    img_src = article_link.xpath('./ancestor::td[1]//img/@src').get()
                    img_alt = article_link.xpath('./ancestor::td[1]//img/@alt').get()
            
            # Determine category based on URL or content
            category = "Նորություններ"
            if link:
                if "full_news.php" in link:
                    category = "Նորություններ"
                elif "full_news2.php" in link:
                    category = "Տնտեսական"
            
            if link and title_text:
                # Ensure absolute URL
                if link.startswith('/') or not link.startswith('http'):
                    full_url = response.urljoin(link)
                else:
                    full_url = link
                
                # Check Redis cache first
                if self.is_article_processed(full_url, title_text):
                    self.cached_skips += 1
                    continue
                
                # Add metadata for context
                meta = {
                    'category': category,
                    'preview_title': title_text,
                    'img_src': img_src,
                    'img_alt': img_alt or title_text
                }
                    
                yield scrapy.Request(full_url, callback=self.parse_article, meta=meta)

    def parse_article(self, response):
        self.processed_articles += 1

        # Get metadata from the main page
        preview_title = response.meta.get('preview_title', '')
        category = response.meta.get('category', 'Նորություններ')
        img_alt = response.meta.get('img_alt', '')

        # Try multiple title selectors for arminfo.info
        title = (response.css("h1::text").get() or
                response.css(".article-title::text").get() or
                response.css(".entry-title::text").get() or
                response.css(".news-title::text").get() or
                response.css("meta[property='og:title']::attr(content)").get() or
                response.css("title::text").get() or
                preview_title)
        
        # Try multiple content selectors for arminfo.info
        content_parts = (response.css("div.article-content p::text").getall() or
                        response.css("div.news-content p::text").getall() or
                        response.css("div.content p::text").getall() or
                        response.css(".entry-content p::text").getall() or
                        response.css(".post-content p::text").getall() or
                        response.css("div.article-content ::text").getall() or
                        response.css("div.news-content ::text").getall() or
                        response.css("p::text").getall())
        
        content = "\n".join([p.strip() for p in content_parts if p.strip()])

        # Extract scraped time - try multiple selectors
        scraped_time = (response.css('time::attr(datetime)').get() or
                       response.css('time::text').get() or 
                       response.css('.date::text').get() or 
                       response.css('.publish-date::text').get() or
                       response.css('.post-date::text').get() or
                       response.css('.news-date::text').get() or
                       datetime.now().isoformat())

        # Clean title for display
        display_title = title[:60] + "..." if title and len(title) > 60 else title or "Անանուն հոդված"
        
        # Check for keywords
        full_text = f"{title or ''} {content} {img_alt}".strip()
        
        if self.article_contains_keyword(full_text):
            self.logger.info(f"✅ Բանալի բառ գտնվեց: {display_title} ({category})")
            # Mark as processed only after successful keyword match
            self.mark_article_processed(response.url, title)
            self.new_articles += 1
            
            item = NewsScraperItem()
            item['title'] = title or f'Arminfo հոդված - {category}'
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
📊 ԱՄՓՈՓՈՒՄ ARMINFO.INFO (օպտիմիզացված - միայն 10 հոդված):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip()) 