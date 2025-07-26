import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import redis
from datetime import datetime
import requests

class IratesSpider(scrapy.Spider):
    name = "irates"
    allowed_domains = ["irates.am"]
    start_urls = ["https://www.irates.am/"]
    
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
        super(IratesSpider, self).__init__(*args, **kwargs)
        
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
        cache_key = f"processed_irates:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_irates:{article_hash}"
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

    def is_recent_content(self, date_text):
        """Check if content is recent (not from 2024 or older)"""
        if not date_text:
            return True  # If no date, assume recent
        
        try:
            # Look for year patterns in date text
            year_match = re.search(r'\b(20\d{2})\b', str(date_text))
            if year_match:
                year = int(year_match.group(1))
                current_year = datetime.now().year
                # Only allow current year and previous year
                return year >= (current_year - 1)
        except:
            pass
        return True  # If can't parse date, assume recent

    def clean_title(self, title):
        """Clean title from unwanted patterns"""
        if not title:
            return None
            
        title = title.strip()
        
        # Remove site name if present
        title = re.sub(r'\s*\|\s*irates\.am.*$', '', title, flags=re.IGNORECASE)
        title = re.sub(r'^irates\.am\s*[-:]\s*', '', title, flags=re.IGNORECASE)
        
        # Filter out unwanted title patterns
        unwanted_patterns = [
            r'^խնդիր', r'^problem', r'^issue',
            r'^բժշկական\s+խնդիր', r'^medical\s+problem',
            r'^տեխնիկական\s+խնդիր', r'^technical\s+issue',
            r'^հարցազրույց', r'^interview',
            r'^հետաքննություն', r'^investigation',
            r'^վիճակագրություն', r'^statistics',
            r'^մենյու', r'^menu', r'^navigation',
            r'^բաժին', r'^section',
            r'^\d{4}\s*թ\.?', r'^\d{4}\s*year'
        ]
        
        for pattern in unwanted_patterns:
            if re.search(pattern, title, re.IGNORECASE):
                self.logger.info(f"❌ Բացառված վերնագիր (խնդրային բովանդակություն): {title[:50]}...")
                return None
        
        return title if len(title) > 5 else None

    def clean_content(self, content_parts):
        """Clean content from unwanted elements"""
        cleaned_content = []
        
        for part in content_parts:
            if not part or not part.strip():
                continue
                
            text = part.strip()
            
            # Skip short texts (likely navigation)
            if len(text) < 20:
                continue
            
            # Skip if contains unwanted patterns
            unwanted_patterns = [
                r'բաժանորդագր', r'subscribe', r'follow', r'հետևել',
                r'կիսվել', r'share', r'like', r'comment',
                r'copyright', r'հեղինակային', r'բոլոր իրավունքները',
                r'navigation', r'մենյու', r'menu', r'բաժին',
                r'կատեգորիա', r'category', r'tag', r'թագ',
                r'read more', r'ավելին', r'continue', r'շարունակել',
                r'previous', r'next', r'նախորդ', r'հաջորդ',
                r'advertisement', r'գովազդ', r'sponsor', r'հովանավոր',
                r'contact', r'կապ', r'email', r'phone', r'հեռախոս',
                r'footer', r'header', r'sidebar',
                r'related', r'առնչվող', r'similar', r'նմանատիպ',
                r'comments', r'մեկնաբանություն', r'feedback',
                r'2024\s*թ\.?', r'year\s*2024'  # Filter out 2024 references
            ]
            
            if any(re.search(pattern, text, re.IGNORECASE) for pattern in unwanted_patterns):
                continue
            
            # Skip if contains too many special characters (likely code/formatting)
            special_char_ratio = len([c for c in text if not c.isalnum() and c not in ' .,!?;:-']) / len(text)
            if special_char_ratio > 0.3:
                continue
            
            # Skip if all caps (likely headers/navigation)
            if text.isupper() and len(text) > 10:
                continue
            
            cleaned_content.append(text)
        
        return " ".join(cleaned_content[:15])  # Limit to first 15 meaningful sentences

    def parse(self, response):
        # Extract recent articles using irates.am structure, focusing on current news
        articles = (response.css("a[href*='/hy/']") or
                   response.css(".feed_li a") or
                   response.css(".list-news a") or
                   response.css("li.feed_li a") or
                   response.css("a[href*='/news/']") or
                   response.css("a[href*='/article/']"))
        
        # If no specific articles found, try general article links but filter carefully
        if not articles:
            all_links = response.css("a[href*='/']")
            articles = []
            for link in all_links:
                href = link.css("::attr(href)").get()
                if href and any(pattern in href for pattern in ['/hy/', '/news/', '/article/']):
                    # Additional filtering for recent content URLs
                    if not any(old_pattern in href.lower() for old_pattern in ['2024', 'archive', 'old', 'past']):
                        articles.append(link)
        
        # Optimize: limit to only latest 10 articles
        articles = articles[:10]
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված (սահմանափակված 10-ով)")

        for article in articles:
            # Extract link
            link = article.css("::attr(href)").get()
            
            if link:
                full_url = response.urljoin(link)
                
                # Skip URLs that seem to be old content or problems
                if any(skip_pattern in full_url.lower() for skip_pattern in 
                      ['2024', 'archive', 'problem', 'issue', 'خنیر', 'interview']):
                    continue
                
                # Get title preview for cache check
                title_preview = (article.css("::text").get() or
                               article.css("::attr(title)").get() or
                               "Article").strip()
                
                # Check Redis cache first
                if self.is_article_processed(full_url, title_preview):
                    self.cached_skips += 1
                    continue
                    
                yield scrapy.Request(full_url, callback=self.parse_article)

    def parse_article(self, response):
        self.processed_articles += 1
        
        self.logger.info(f"🔍 Պրոցեսինգ URL: {response.url}")

        # Extract title with comprehensive selectors
        raw_title = (response.css("h1::text").get() or
                response.css("h1 *::text").get() or
                response.css(".article-title::text").get() or
                response.css(".post-title::text").get() or
                response.css(".entry-title::text").get() or
                response.css(".news-title::text").get() or
                response.css(".title::text").get() or
                response.css("meta[property='og:title']::attr(content)").get() or
                response.css("title::text").get())
        
        # Clean and validate title
        title = self.clean_title(raw_title)
        if not title:
            self.logger.info("❌ Վերնագիրը բացառվել է (խնդրային/հին բովանդակություն)")
            return

        self.logger.info(f"📝 Հոդվածի վերնագիր: {title[:80]}...")

        # Extract date and check if recent
        date_text = (response.css('time::attr(datetime)').get() or
                    response.css('time::text').get() or 
                    response.css('.feedDateTime::text').get() or
                    response.css('.feedDate::text').get() or
                    response.css('.date::text').get() or 
                    response.css('.publish-date::text').get() or 
                    response.css('.post-date::text').get() or 
                    response.css('.article-date::text').get())

        if not self.is_recent_content(date_text):
            self.logger.info(f"❌ Հին բովանդակություն բացառվել է: {date_text}")
            return

        # Extract content with focus on main article paragraphs only
        content_parts = []
        
        # Try irates.am specific content selectors first
        main_selectors = [
            # irates.am specific selectors based on their structure
            "div.article-content p",
            "div.post-content p",
            "div.content p",
            "div.article-text p",
            "div.article-body p",
            "div.news-content p",
            "div.main-content p",
            "div#content p",
            "div.text p",
            "div.body p",
            # Try more general selectors for irates.am
            ".content-area p",
            ".entry-content p",
            ".article p",
            "article p",
            # Fallback to any paragraph in main content area
            "main p",
            "#main p",
            ".main p"
        ]
        
        for selector in main_selectors:
            content_parts = response.css(f"{selector}::text").getall()
            if content_parts and len(content_parts) > 0:
                self.logger.info(f"📍 Բովանդակություն գտնվեց selector-ով: {selector}")
                break
        
        # If no main content found, try more general approach
        if not content_parts:
            # Try to find text in any div that might contain article content
            all_divs_text = response.css("div::text").getall()
        
            # Filter for meaningful content
            content_parts = []
            for text in all_divs_text:
                if text and text.strip():
                    text = text.strip()
                    # Only include longer texts that might be article content
                    if (len(text) > 50 and 
                        not text.lower().startswith(('menu', 'մենյու', 'navigation', 'nav')) and
                        not any(skip in text.lower() for skip in ['copyright', 'all rights', 'հեղինակային'])):
                        content_parts.append(text)
            
            if content_parts:
                self.logger.info("📍 Բովանդակություն գտնվեց ընդհանուր div::text selector-ով")
        
        # If still no content, try paragraph texts anywhere
        if not content_parts:
            all_paragraphs = response.css("p::text").getall()
            content_parts = [p for p in all_paragraphs if p and len(p.strip()) > 30]
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
            # The original code had NewsArticle.objects.filter(source_url=response.url).exists()
            # which is Django-specific. Assuming NewsArticle is defined elsewhere or this line is removed.
            # For now, commenting out to avoid NameError.
            # if NewsArticle.objects.filter(source_url=response.url).exists():
            #     self.logger.info(f"🔄 Կրկնվող հոդված (արդեն գոյություն ունի): {display_title}")
            #     self.duplicate_articles += 1
            #     self.mark_article_processed(response.url, title)
            #     return
            
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
📊 ԱՄՓՈՓՈՒՄ IRATES.AM (օպտիմիզացված):
   • Գտնված հոդվածներ: {self.processed_articles}
   • Բանալի բառերով: {self.new_articles}
   • Կրկնվող հոդվածներ: {self.duplicate_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Ֆիլտրված խնդրային/հին բովանդակություն: ✅
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip()) 