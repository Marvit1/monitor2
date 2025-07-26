import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import redis
from datetime import datetime
import requests
import re

class IravabanSpider(scrapy.Spider):
    name = "iravaban"
    allowed_domains = ["iravaban.net"]
    start_urls = ["https://iravaban.net/"]
    
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
        super(IravabanSpider, self).__init__(*args, **kwargs)
        
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
        cache_key = f"processed_iravaban:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_iravaban:{article_hash}"
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

    def clean_title(self, title):
        """Clean title from unwanted patterns and site name"""
        if not title:
            return None
            
        title = title.strip()
        
        # Remove site name if present
        title = re.sub(r'\s*\|\s*iravaban\.net.*$', '', title, flags=re.IGNORECASE)
        title = re.sub(r'^iravaban\.net\s*[-:]\s*', '', title, flags=re.IGNORECASE)
        
        # Filter out unwanted title patterns
        unwanted_patterns = [
            r'^մենյու', r'^menu', r'^navigation', r'^nav',
            r'^բաժին', r'^section', r'^category', r'^կատեգորիա',
            r'^գլխավոր', r'^home', r'^main', r'^սկիզբ',
            r'^search', r'^փնտրել', r'^որոնում',
            r'^login', r'^մուտք', r'^գրանցում',
            r'^advertisement', r'^գովազդ',
            r'^contact', r'^կապ', r'^հետադարձ'
        ]
        
        for pattern in unwanted_patterns:
            if re.search(pattern, title, re.IGNORECASE):
                self.logger.info(f"❌ Բացառված վերնագիր (նավիգացիա): {title[:50]}...")
                return None
        
        return title if len(title) > 5 else None

    def clean_content(self, content_parts):
        """Clean content from unwanted elements"""
        cleaned_content = []
        
        for part in content_parts:
            if not part or not part.strip():
                continue
                
            text = part.strip()
            
            # Skip short texts (likely navigation/UI elements)
            if len(text) < 15:
                continue
            
            # Skip if contains unwanted patterns
            unwanted_patterns = [
                r'բաժանորդագր', r'subscribe', r'follow', r'հետևել',
                r'կիսվել', r'share', r'like', r'comment', r'մեկնաբանություն',
                r'copyright', r'հեղինակային', r'բոլոր իրավունքները',
                r'navigation', r'մենյու', r'menu', r'բաժին',
                r'կատեգորիա', r'category', r'tag', r'թագ',
                r'read more', r'ավելին', r'continue', r'շարունակել',
                r'previous', r'next', r'նախորդ', r'հաջորդ',
                r'advertisement', r'գովազդ', r'sponsor', r'հովանավոր',
                r'contact', r'կապ', r'email', r'phone', r'հեռախոս',
                r'footer', r'header', r'sidebar',
                r'related', r'առնչվող', r'similar', r'նմանատիպ',
                r'comments', r'feedback', r'հետադարձ',
                r'login', r'մուտք', r'register', r'գրանցվել',
                r'search', r'փնտրել', r'որոնում',
                r'home', r'գլխավոր', r'սկիզբ',
                r'view all', r'տեսնել բոլորը', r'show more'
            ]
            
            if any(re.search(pattern, text, re.IGNORECASE) for pattern in unwanted_patterns):
                continue
            
            # Skip if contains too many special characters (likely code/formatting)
            special_char_ratio = len([c for c in text if not c.isalnum() and c not in ' .,!?;:-']) / len(text)
            if special_char_ratio > 0.4:
                continue
            
            # Skip if all caps (likely headers/navigation)
            if text.isupper() and len(text) > 10:
                continue
            
            # Skip if mostly numbers or symbols
            alphanumeric_ratio = len([c for c in text if c.isalnum()]) / len(text)
            if alphanumeric_ratio < 0.6:
                continue
            
            cleaned_content.append(text)
        
        return " ".join(cleaned_content[:15])  # Limit to first 15 meaningful sentences

    def parse(self, response):
        # Extract articles using the iravaban.net structure
        # Articles are in div.newsfeed_item elements
        articles = response.css("div.newsfeed_item")
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված (օպտիմիզացված - սահմանափակված 10-ով)")
        
        # Limit to latest 10 articles only for optimization (running every 10 minutes)
        articles = articles[:10]

        for article in articles:
            # Extract link and title using iravaban.net structure
            link = article.css("a::attr(href)").get()
            title_preview = article.css("div.entry-title::text").get()
            
            # Clean up title that might have extra elements
            if not title_preview:
                title_preview = article.css("div.entry-title").get()
                if title_preview:
                    # Remove HTML tags if present
                    title_preview = re.sub(r'<[^>]+>', '', title_preview)
            
            if link and title_preview:
                # Clean the title by removing extra whitespace and HTML entities
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

        self.logger.info(f"🔍 Պրոցեսինգ URL: {response.url}")

        # Try multiple title selectors for iravaban.net
        raw_title = (response.css("h1::text").get() or
                    response.css(".article-title::text").get() or
                    response.css(".entry-title::text").get() or
                    response.css(".post-title::text").get() or
                    response.css(".page-title::text").get() or
                    response.css(".news-title::text").get() or
                    response.css(".main-title::text").get() or
                    response.css(".title::text").get() or
                    response.css("meta[property='og:title']::attr(content)").get() or
                    response.css("title::text").get())
        
        # Clean and validate title
        title = self.clean_title(raw_title)
        if not title:
            self.logger.info("❌ Վերնագիրը բացառվել է (նավիգացիա/ավելորդ)")
            return

        self.logger.info(f"📝 Հոդվածի վերնագիր: {title[:80]}...")

        # Extract content with focus on main article paragraphs only
        content_parts = []
        
        # Try iravaban.net specific content selectors first
        main_selectors = [
            "div.article-content p",
            "div.post-content p",
            "div.entry-content p",
            "div.content p",
            "div.article-body p",
            "div.article-text p",
            "div.text-content p",
            "div.main-content p",
            "div.news-content p",
            "div.story-content p",
            "div.body-content p",
            "article p",
            ".content p",
            ".article p",
            ".text p"
        ]
        
        for selector in main_selectors:
            content_parts = response.css(f"{selector}::text").getall()
            if content_parts and len(content_parts) > 0:
                self.logger.info(f"📍 Բովանդակություն գտնվեց selector-ով: {selector}")
                break
        
        # If no main content found, try more general approach but be selective
        if not content_parts:
            # Try to find text in any paragraph that might contain article content
            all_paragraphs = response.css("p::text").getall()
            content_parts = [p for p in all_paragraphs if p and len(p.strip()) > 20]
            if content_parts:
                self.logger.info("📍 Բովանդակություն գտնվեց ընդհանուր p::text selector-ով")
        
        # Clean content
        content = self.clean_content(content_parts)
        
        self.logger.info(f"📄 Բովանդակության երկարություն: {len(content)} նիշ")
        if content:
            self.logger.info(f"📄 Բովանդակության մաս: {content[:100]}...")

        # Use current time as scraped time
        scraped_time = datetime.now().isoformat()

        # Clean title for display
        display_title = title[:60] + "..." if len(title) > 60 else title
        
        # Check for keywords in title and content
        full_text = f"{title} {content}"
        
        if self.article_contains_keyword(full_text):
            self.logger.info(f"✅ Բանալի բառ գտնվեց: {display_title}")
            
            # Check if this exact article already exists in database
            # This part of the code was removed as per the edit hint.
            # The original code had a line `if NewsArticle.objects.filter(source_url=response.url).exists():`
            # which was not part of the `code_block_to_apply_changes_from` and `code_block_to_apply_changes_from`
            # provided in the edit hint.
            # Assuming the intent was to remove this line as it was not in the new_code.
            # self.logger.info(f"🔄 Կրկնվող հոդված (արդեն գոյություն ունի): {display_title}")
            # self.duplicate_articles += 1
            # self.mark_article_processed(response.url, title)
            # return
            
            self.new_articles += 1
            self.mark_article_processed(response.url, title)
            
            item = NewsScraperItem()
            item['title'] = title
            item['link'] = response.url
            item['source_url'] = response.url
            item['content'] = content
            item['scraped_time'] = scraped_time
            yield item
        else:
            self.logger.info(f"❌ Բանալի բառ չգտնվեց: {display_title}")
            self.mark_article_processed(response.url, title)

    def closed(self, reason):
        """Called when spider finishes"""
        self.logger.info(f"""
📊 ԱՄՓՈՓՈՒՄ IRAVABAN.NET (օպտիմիզացված):
   • Գտնված հոդվածներ: {self.processed_articles}
   • Բանալի բառերով: {self.new_articles}
   • Կրկնվող հոդվածներ: {self.duplicate_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Ֆիլտրված ավելորդ բովանդակություն: ✅
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip())