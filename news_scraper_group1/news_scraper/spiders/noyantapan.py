import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import redis
from datetime import datetime
import re
import requests

class NoyantapanSpider(scrapy.Spider):
    name = "noyantapan"
    allowed_domains = ["nt.am"]
    start_urls = ["https://nt.am/am/"]
    
    # Add custom headers
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
        super(NoyantapanSpider, self).__init__(*args, **kwargs)
        
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

    def clean_title(self, title):
        """Clean title from unwanted patterns and site name"""
        if not title:
            return None
            
        title = title.strip()
        
        # Remove site name if present
        title = re.sub(r'\s*\|\s*nt\.am.*$', '', title, flags=re.IGNORECASE)
        title = re.sub(r'^nt\.am\s*[-:]\s*', '', title, flags=re.IGNORECASE)
        title = re.sub(r'\s*[-–]\s*Noyan\s*Tapan.*$', '', title, flags=re.IGNORECASE)
        title = re.sub(r'\s*[-–]\s*Նոյան\s*Տապան.*$', '', title, flags=re.IGNORECASE)
        title = re.sub(r'\s*[-–]\s*Լրատվական\s*Գործակալություն.*$', '', title, flags=re.IGNORECASE)
        
        # Filter out unwanted title patterns
        unwanted_patterns = [
            r'^մենյու', r'^menu', r'^navigation', r'^nav',
            r'^բաժին', r'^section', r'^category', r'^կատեգորիա',
            r'^գլխավոր', r'^home', r'^main', r'^սկիզբ',
            r'^search', r'^փնտրել', r'^որոնում',
            r'^login', r'^մուտք', r'^գրանցում',
            r'^advertisement', r'^գովազդ',
            r'^contact', r'^կապ', r'^հետադարձ',
            r'^share', r'^կիսվել', r'^follow', r'^հետևել',
            r'^rec-item', r'^d-flex', r'^img-content', r'^rec', r'^item'
        ]
        
        for pattern in unwanted_patterns:
            if re.search(pattern, title, re.IGNORECASE):
                self.logger.info(f"❌ Բացառված վերնագիր (նավիգացիա): {title[:50]}...")
                return None
        
        return title if len(title) > 5 else None

    def clean_content(self, content_parts):
        """Clean content from unwanted elements with enhanced filtering"""
        cleaned_content = []
        
        for part in content_parts:
            if not part or not part.strip():
                continue
                
            text = part.strip()
            
            # Skip short texts (likely navigation/UI elements)
            if len(text) < 20:
                continue
            
            # Skip if contains unwanted patterns
            unwanted_patterns = [
                # Social media and sharing
                r'բաժանորդագր', r'subscribe', r'follow', r'հետևել',
                r'կիսվել', r'share', r'like', r'comment', r'մեկնաբանություն',
                r'տպել', r'print', r'share this article',
                r'facebook', r'twitter', r'instagram', r'youtube', r'telegram',
                r'whatsapp', r'linkedin', r'tiktok',
                
                # Site branding and copyright
                r'nt\.am', r'noyan\s*tapan', r'նոյան\s*տապան', r'լրատվական\s*գործակալություն',
                r'news\s*agency', r'գործակալություն', r'copyright', r'հեղինակային', r'բոլոր իրավունքները',
                r'all rights reserved', r'©',
                
                # Navigation and UI
                r'navigation', r'մենյու', r'menu', r'բաժին',
                r'կատեգորիա', r'category', r'tag', r'թագ',
                r'read more', r'ավելին', r'continue', r'շարունակել',
                r'previous', r'next', r'նախորդ', r'հաջորդ',
                r'back', r'forward', r'վերադառնալ',
                
                # Ads and promotion
                r'advertisement', r'գովազդ', r'sponsor', r'հովանավոր',
                r'sponsored', r'անվճար', r'զեղչ', r'ակցիա',
                
                # Contact and subscription
                r'contact', r'կապ', r'email', r'phone', r'հեռախոս',
                r'subscription', r'newsletter', r'բաժանորդագրություն',
                
                # Footer and header
                r'footer', r'header', r'sidebar',
                
                # Related content
                r'related', r'առնչվող', r'similar', r'նմանատիպ',
                r'see also', r'read also', r'տես նաև', r'կարդա նաև',
                
                # Comments and feedback
                r'comments', r'feedback', r'հետադարձ',
                r'reply', r'replies', r'պատասխան',
                
                # Login and registration
                r'login', r'մուտք', r'register', r'գրանցվել',
                
                # Search and filters
                r'search', r'փնտրել', r'որոնում',
                r'filter', r'sort', r'տեսակավորել', r'ֆիլտր',
                
                # Home and navigation
                r'home', r'գլխավոր', r'սկիզբ',
                r'view all', r'տեսնել բոլորը', r'show more',
                
                # Technical elements
                r'loading', r'բեռնում', r'please wait', r'սպասել',
                r'error', r'սխալ', r'success', r'հաջողություն',
                
                # Links and URLs
                r'http', r'https', r'www\.', r'\.com', r'\.am', r'\.org',
                r'url', r'link', r'հղում',
                
                # Date and time standalone
                r'^ամսաթիվ$', r'^date$', r'^time$', r'^ժամ$',
                
                # Weather content
                r'եղանակ', r'weather', r'temperature', r'ջերմություն',
                
                # Video and media controls
                r'play', r'pause', r'stop', r'volume', r'mute',
                r'նվագարկել', r'դադարեցնել', r'կանգնել',
                
                # Pagination
                r'page', r'pages', r'էջ', r'էջեր',
                r'first', r'last', r'առաջին', r'վերջին',
                
                # Legal terms
                r'terms', r'conditions', r'privacy', r'policy',
                r'պայմաններ', r'գաղտնիություն', r'քաղաքականություն',
                
                # Noyan Tapan specific patterns
                r'rec-item', r'd-flex', r'img-content', r'rec', r'item',
                r'strong', r'span', r'div', r'p', r'a', r'href'
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
            
            # Skip single characters or very short fragments
            if len(text) < 3 or text in ['•', '›', '→', '←', '↑', '↓']:
                continue
            
            # Skip if it's mostly punctuation
            punct_ratio = len([c for c in text if c in '.,!?;:-()[]{}']) / len(text)
            if punct_ratio > 0.5:
                continue
            
            cleaned_content.append(text)
        
        return " ".join(cleaned_content[:15])  # Limit to first 15 meaningful sentences

    def is_article_processed(self, url, title):
        """Check if article was already processed using Redis cache"""
        if not self.redis_client:
            return False
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_noyantapan:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_noyantapan:{article_hash}"
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
        # Extract articles from nt.am structure
        articles = response.css("div.rec-item.d-flex")
        
        # Optimize: limit to latest 10 articles
        articles = articles[:10]
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված (սահմանափակված 10-ով)")

        for article in articles:
            # Get the time from strong span
            time_text = article.css("div.img-content strong span::text").get()
            
            # Get the link and title from img-content p a
            content_section = article.css("div.img-content p a")
            if content_section:
                link = content_section.css("::attr(href)").get()
                title = content_section.css("::text").get()
                
                if link and title:
                    full_url = response.urljoin(link)
                    
                    # Check Redis cache first
                    if self.is_article_processed(full_url, title):
                        self.cached_skips += 1
                        continue
                    
                    # Include time in meta for additional context
                    yield scrapy.Request(
                        full_url, 
                        callback=self.parse_article,
                        meta={'time_text': time_text, 'preview_title': title}
                    )

    def parse_article(self, response):
        self.processed_articles += 1
        time_text = response.meta.get('time_text', '')
        preview_title = response.meta.get('preview_title', '')

        self.logger.info(f"🔍 Պրոցեսինգ URL: {response.url}")

        # Extract title with comprehensive selectors
        raw_title = (response.css("h1::text").get() or
                    response.css(".entry-title::text").get() or
                    response.css(".post-title::text").get() or
                    response.css(".article-title::text").get() or
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
        
        # Try nt.am specific content selectors first
        main_selectors = [
            "div.entry-content p",
            "div.post-content p",
            "div.article-content p",
            "div.content p",
            "article p",
            "div.main-content p",
            "div.article-body p",
            "div.text p",
            "div.article_text p",
            "div.news-content p",
            "div.news-body p",
            "div.post-body p",
            "div.text-content p",
            ".entry-content p",
            ".post-content p",
            ".article-content p",
            ".content p",
            ".main-content p",
            ".article-body p",
            ".text p",
            ".article_text p",
            ".news-content p",
            ".news-body p",
            ".post-body p",
            ".text-content p"
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

        # Skip if content is too short
        if not content or len(content.strip()) < 30:
            self.logger.info(f"❌ Շատ կարճ բովանդակություն: {title[:50]}...")
            return

        # Use current time as scraped time
        scraped_time = datetime.now().isoformat()

        # Clean title for display
        display_title = title[:60] + "..." if len(title) > 60 else title
        
        # Check for keywords in title and content
        full_text = f"{title} {content}"
        
        if self.article_contains_keyword(full_text):
            self.logger.info(f"✅ Բանալի բառ գտնվեց: {display_title}")
            
            # Mark as processed only after successful keyword match
            self.mark_article_processed(response.url, title)
            self.new_articles += 1
            
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
📊 ԱՄՓՈՓՈՒՄ NOYAN TAPAN (օպտիմիզացված):
   • Գտնված հոդվածներ: {self.processed_articles}
   • Բանալի բառերով: {self.new_articles}
   • Կրկնվող հոդվածներ: {self.duplicate_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Ֆիլտրված HTML tags և ավելորդ բովանդակություն: ✅
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip()) 