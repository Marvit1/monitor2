import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import requests
import redis
from datetime import datetime
import random
import time

    

class ShabatSpider(scrapy.Spider):
    name = "shabat"
    allowed_domains = ["shabat.news"]
    start_urls = ["https://shabat.news/", "https://shabat.news/am/"]
    
    # Enhanced anti-blocking headers
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'
    ]
    
    custom_settings = {
        'DOWNLOAD_DELAY': 2,
        'RANDOMIZE_DOWNLOAD_DELAY': 1,
        'RETRY_TIMES': 3,
        'RETRY_HTTP_CODES': [500, 502, 503, 504, 408, 429, 403],
        'HTTPERROR_ALLOWED_CODES': [403, 404],
        'COOKIES_ENABLED': True,
        'CONCURRENT_REQUESTS': 2,
        'CONCURRENT_REQUESTS_PER_DOMAIN': 1,
        'DEFAULT_REQUEST_HEADERS': {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'hy-AM,hy;q=0.9,en;q=0.8,ru;q=0.7',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'DNT': '1',
            'Cache-Control': 'max-age=0',
        },
    }

    def __init__(self, *args, **kwargs):
        super(ShabatSpider, self).__init__(*args, **kwargs)
        
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

    def clean_text_fragments(self, text_fragments):
        """Clean text fragments by removing unwanted elements - balanced filtering"""
        if not text_fragments:
            return []
        
        # Unwanted fragments to filter out - focused on most obvious unwanted elements
        unwanted_fragments = [
            # Navigation bullets and arrows
            "•", "›", "→", "←", "↑", "↓", "▪", "▫", "■", "□", "◆", "◇", "○", "●",
            # Social media sharing terms
            "share", "facebook", "twitter", "instagram", "youtube", "telegram", "whatsapp", "viber",
            "like", "comment", "follow", "subscribe", "կիսվել", "հավանել", "տարածել",
            # Common navigation terms
            "կարդալ ավելին", "ցույց տալ", "բացել", "սեղմել", "մուտք", "ելք", "գնալ",
            # Shabat.news specific
            "shabat.news", "shabat.am", "shabat", "շաբաթ",
            # Copyright and ads
            "© 2024", "© 2023", "© shabat", "copyright", "գովազդ", "advertisement",
            # Click baits
            "click here", "այստեղ սեղմել", "կարդալ ամբողջը",
            # Technical elements
            "loading", "error", "բեռնում", "սխալ", "Որոնում", "որոնում", "Search", "search", "Մենյու", "մենյու", "Menu", "menu"
        ]
        
        cleaned_fragments = []
        for fragment in text_fragments:
            if not fragment or not fragment.strip():
                continue
                
            cleaned_fragment = fragment.strip()
            
            # Skip if fragment is too short (less than 2 characters)
            if len(cleaned_fragment) < 2:
                continue
                
            # Skip if fragment matches unwanted patterns exactly
            if cleaned_fragment.lower() in unwanted_fragments:
                continue
                
            # Skip if fragment is mostly punctuation (more than 90% punctuation)
            if len([c for c in cleaned_fragment if c.isalnum()]) < len(cleaned_fragment) * 0.1:
                continue
                
            # Skip if fragment doesn't contain any letters
            if not any(c.isalpha() for c in cleaned_fragment):
                continue
                
            # Skip if fragment is just numbers
            if cleaned_fragment.replace(" ", "").replace(".", "").replace("-", "").replace("/", "").replace(":", "").replace(",", "").isdigit():
                continue
                
            # Add the fragment if it passes all filters
            cleaned_fragments.append(cleaned_fragment)
        
        return cleaned_fragments

    def get_random_headers(self):
        """Generate random headers to avoid blocking"""
        user_agent = random.choice(self.user_agents)
        referers = [
            'https://www.google.com/',
            'https://www.facebook.com/',
            'https://www.bing.com/',
            'https://duckduckgo.com/',
            'https://t.me/',
            'https://www.youtube.com/'
        ]
        return {
            'User-Agent': user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'hy-AM,hy;q=0.9,en;q=0.8,ru;q=0.7',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'DNT': '1',
            'Cache-Control': 'max-age=0',
            'Referer': random.choice(referers)
        }

    def start_requests(self):
        """Override start_requests to use random headers"""
        for url in self.start_urls:
            time.sleep(random.uniform(1, 3))  # Random delay
            yield scrapy.Request(
                url=url,
                headers=self.get_random_headers(),
                callback=self.parse,
                dont_filter=True
            )

    def is_article_processed(self, url, title):
        """Check if article was already processed using Redis cache"""
        if not self.redis_client:
            return False
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_shabat:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_shabat:{article_hash}"
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
        # Handle 403 errors with retry
        if response.status == 403:
            self.logger.warning(f"🚫 403 Forbidden. Retrying with different headers...")
            time.sleep(random.uniform(5, 10))
            yield scrapy.Request(
                url=response.url,
                headers=self.get_random_headers(),
                callback=self.parse,
                dont_filter=True
            )
            return

        # Extract articles from the shabat.news structure
        # Based on the HTML structure provided: div.artcl
        articles = response.css("div.artcl")
        
        # Also try alternative selectors if the main one doesn't work
        if not articles:
            articles = response.css("div.artcl-list div.artcl")
        
        if not articles:
            articles = response.css("section.list-section div.artcl")
        
        # Limit to latest 10 articles for optimization
        articles = articles[:10]
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված (սահմանափակված 10-ով)")

        for article in articles:
            # Extract link using the structure from provided HTML
            # The link is in the <a> tag wrapping the article content
            link = article.css("a::attr(href)").get()
            
            # Extract title from h1 inside the article
            title = article.css("h1::text").get()
            
            # Extract date from h2
            date_info = article.css("h2::text").get()
            
            # Extract preview text from p
            preview = article.css("p::text").get()
            
            if link and title:
                # Clean title
                title = title.strip()
                
                # Ensure absolute URL
                full_url = response.urljoin(link) if not link.startswith('http') else link
                
                # Check Redis cache first
                if self.is_article_processed(full_url, title):
                    self.cached_skips += 1
                    continue
                
                # Add metadata
                meta = {
                    'scraped_time': date_info.strip() if date_info else None,
                    'preview_title': title,
                    'preview_text': preview.strip() if preview else None
                }
                
                yield scrapy.Request(
                    full_url, 
                    callback=self.parse_article, 
                    meta=meta,
                    headers=self.get_random_headers()
                )

    def parse_article(self, response):
        self.processed_articles += 1

        # Get metadata from the main page
        preview_title = response.meta.get('preview_title', '')
        main_page_time = response.meta.get('scraped_time', '')
        preview_text = response.meta.get('preview_text', '')

        # Hierarchical title extraction with fallback
        title = None
        title_selectors = [
            "h1.article-title::text",
            "h1.post-title::text",
            "h1.entry-title::text",
            "h1.title::text",
            "h1::text",
            ".article-title::text",
            ".post-title::text",
            ".entry-title::text",
            ".title::text",
            "meta[property='og:title']::attr(content)",
            "title::text"
        ]
        
        for selector in title_selectors:
            title = response.css(selector).get()
            if title and title.strip() and len(title.strip()) > 5:
                title = title.strip()
                # Remove site name from title if present
                if " - Shabat.am" in title:
                    title = title.replace(" - Shabat.am", "")
                if " | Shabat.am" in title:
                    title = title.replace(" | Shabat.am", "")
                if " - shabat.news" in title:
                    title = title.replace(" - shabat.news", "")
                if " | shabat.news" in title:
                    title = title.replace(" | shabat.news", "")
                break
        
        # If no proper title found, use preview title
        if not title and preview_title:
            title = preview_title
        
        # If still no title, generate one
        if not title:
            title = f"Article from {response.url.split('/')[-1] or response.url.split('/')[-2]}"

        # Article-specific content extraction - balanced approach for shabat.news
        content_parts = []
        
        # Primary content selectors - less specific but still targeted
        content_selectors = [
            # Target paragraphs in main content areas
            "div.article-content p::text",
            "div.post-content p::text", 
            "div.entry-content p::text",
            "div.content p::text",
            "div.article-body p::text",
            "div.text-content p::text",
            "div.text p::text",
            "article p::text",
            "div.main-content p::text",
            # Class-based selectors
            ".article-content p::text",
            ".post-content p::text",
            ".entry-content p::text",
            ".content p::text",
            ".article-body p::text",
            ".text-content p::text",
            ".text p::text",
            ".main-content p::text",
            # More general selectors
            "p::text"
        ]
        
        # Try each selector until content is found
        for selector in content_selectors:
            content_parts = response.css(selector).getall()
            if content_parts:
                break
        
        # Clean the extracted content
        cleaned_content = self.clean_text_fragments(content_parts)
        content = "\n".join(cleaned_content)
        
        # Remove common unwanted prefixes that might still get through
        unwanted_prefixes = ["Որոնում", "որոնում", "Search", "search", "Մենյու", "մենյու", "Menu", "menu"]
        for prefix in unwanted_prefixes:
            if content.startswith(prefix):
                content = content[len(prefix):].strip()
                break
        
        # If content is empty, use preview text
        if not content and preview_text:
            content = preview_text

        # Skip if content is too short or empty - more reasonable minimum length
        if not content or len(content.strip()) < 30:
            self.logger.info(f"❌ Շատ կարճ բովանդակություն ({len(content.strip()) if content else 0} նիշ): {title[:50]}...")
            return
            
        # Additional validation - ensure we have meaningful content
        content_words = content.split()
        if len(content_words) < 5:  # Less than 5 words is likely not real article content
            self.logger.info(f"❌ Շատ քիչ բառեր ({len(content_words)} բառ): {title[:50]}...")
            return
            
        # Check if content looks like real article text (not just navigation/UI elements)
        meaningful_words = [word for word in content_words if len(word) > 3 and word.isalpha()]
        if len(meaningful_words) < 3:  # Less than 3 meaningful words
            self.logger.info(f"❌ Շատ քիչ իմաստալի բառեր ({len(meaningful_words)} բառ): {title[:50]}...")
            return

        # Extract scraped time - try multiple selectors
        scraped_time = (response.css('time::attr(datetime)').get() or
                       response.css('time::text').get() or 
                       response.css('.article-date::text').get() or
                       response.css('.post-date::text').get() or
                       response.css('.date::text').get() or
                       main_page_time or
                       datetime.now().isoformat())

        # Clean title for display
        display_title = title[:60] + "..." if title and len(title) > 60 else title or "Անանուն հոդված"
        
        # Check for keywords
        full_text = f"{title or ''} {content}".strip()
        
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
            # Mark as processed even if no keyword match to avoid re-checking
            self.mark_article_processed(response.url, title)

    def closed(self, reason):
        """Called when the spider is closed"""
        self.logger.info(f"""
📊 ԱՄՓՈՓՈՒՄ SHABAT.NEWS (օպտիմիզացված - միայն 10 հոդված):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Կրկնվող հոդվածներ: {self.duplicate_articles}
   • Ավարտման պատճառ: {reason}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip()) 