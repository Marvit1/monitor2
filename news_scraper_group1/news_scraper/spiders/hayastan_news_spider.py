import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import redis
from datetime import datetime, timedelta
import requests

class HayastanNewsSpider(scrapy.Spider):
    name = "hayastan_news"
    allowed_domains = ["hayastan.news"]
    
    # Add custom headers to bypass potential bot blocking
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
        super(HayastanNewsSpider, self).__init__(*args, **kwargs)
        
        # Redis connection
        try:
            self.redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
            self.redis_client.ping()  # Test connection
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
        self.duplicate_articles = 0
        self.cached_skips = 0
        
        # Always start from page 1 for optimization (only process latest 10 articles)
        self.current_page = 1
        self.start_urls = ["https://hayastan.news/am"]

    def is_article_processed(self, url, title):
        """Check if article was already processed using Redis cache"""
        if not self.redis_client:
            return False
            
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_hayastan:{article_hash}"
        
        # Check if exists in cache
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
            
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_hayastan:{article_hash}"
        
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
        """Parse the main news page to find links to individual articles."""
        # Get news items
        news_items = response.css('div.py-12.border-b.border-gray-light')
        
        # Limit to latest 10 articles only for optimization (running every 10 minutes)
        news_items = news_items[:10]
        
        self.logger.info(f"📰 Էջ {self.current_page}: Հայտնաբերվել է {len(news_items)} հոդված (սահմանափակված 10-ով)")
        
        for item in news_items:
            link_tag = item.css('h4.text-sm a.animate-text::attr(href)').get()
            if link_tag:
                absolute_link = response.urljoin(link_tag)
                
                # Quick title check for Redis caching
                title_preview = item.css('h4.text-sm a.animate-text::text').get() or ""
                
                # Check Redis cache first
                if self.is_article_processed(absolute_link, title_preview):
                    self.cached_skips += 1
                    continue
                    
                yield scrapy.Request(absolute_link, callback=self.parse_article)

        # Pagination removed - only processing latest 10 articles from first page for optimization
        self.logger.info("⏹️ Միայն առաջին էջից վերջին 10 հոդվածները (օպտիմիզացիա)")

    def parse_article(self, response):
        """Parse an individual article page to extract the required data."""
        self.processed_articles += 1
        
        # Try multiple selectors for title
        title = response.css('h1.font-bold.text-2xl::text').get()
        if not title:
            title = response.css('h1::text').get()
        if not title:
            title = response.css('title::text').get()
        if not title:
            title = response.css('meta[property="og:title"]::attr(content)').get()

        # Optimized content selectors - only get main article content
        content_selectors = [
            'div.prose p::text',
            '.content p::text',
            'article p::text',
            '.article-content p::text',
            '.main-content p::text',
            '.post-content p::text'
        ]
        
        content_parts = []
        for selector in content_selectors:
            try:
                parts = response.css(selector).getall()
                if parts:
                    content_parts = parts
                    break
            except:
                continue
        
        # Clean content - remove navigation, ads, footer and other unwanted elements
        if not content_parts:
            # Fallback to all p tags if specific selectors fail
            content_parts = response.css('p::text').getall()
        
        # Filter out unwanted content
        filtered_content = []
        unwanted_phrases = [
            "կարդալ ավելին",
            "կարդալ ավելի",
            "ավելին",
            "կարդալ",
            "share",
            "like",
            "follow",
            "subscribe",
            "կիսվել",
            "հավանել",
            "տեսնել ավելին",
            "բաժանորդագրվել",
            "facebook",
            "twitter",
            "instagram",
            "կապ",
            "contact",
            "մեկնաբանություն",
            "comment",
            "tag",
            "պիտակ",
            "category",
            "կատեգորիա",
            "author",
            "հեղինակ",
            "date",
            "ամսաթիվ",
            "մարտ",
            "ապրիլ",
            "մայիս",
            "հունիս",
            "հուլիս",
            "օգոստոս",
            "սեպտեմբեր",
            "հոկտեմբեր",
            "նոյեմբեր",
            "դեկտեմբեր",
            "փետրվար",
            "հունվար",
            "copyright",
            "© ",
            "all rights reserved",
            "բոլոր իրավունքները",
            "advertisement",
            "գովազդ",
            "footer",
            "ծանուցակցություն",
            "անվտանգություն",
            "Click here",
            "Մանրամասն",
            "Ավելի տեղեկություն",
            "Տեղեկություն",
            "Կարծիք",
            "Գրել",
            "Այլ",
            "hayastan.news",
            "hayastan",
            "related",
            "հարակից",
            "navigation",
            "նավիգացիա",
            "menu",
            "մենյու",
            "search",
            "որոնում",
            "header",
            "վերև",
            "footer",
            "ներքև",
            "sidebar",
            "կողային",
            "widget",
            "վիջետ",
            "popular",
            "հայտնի",
            "trending",
            "թրենդ",
            "recent",
            "վերջին",
            "more news",
            "ավելի լուրեր",
            "other articles",
            "այլ հոդվածներ"
        ]
        
        for part in content_parts:
            if part and len(part.strip()) > 10:  # Only keep meaningful content
                clean_part = part.strip()
                
                # Check if content contains unwanted phrases
                is_unwanted = False
                for phrase in unwanted_phrases:
                    if phrase.lower() in clean_part.lower():
                        is_unwanted = True
                        break
                
                # Skip short or unwanted content
                if not is_unwanted and len(clean_part) > 20:
                    filtered_content.append(clean_part)
        
        # Join filtered content
        content = "\n".join(filtered_content)
        
        # Additional content cleaning - remove empty lines and extra spaces
        content_lines = [line.strip() for line in content.split('\n') if line.strip()]
        content = "\n".join(content_lines)

        # Extract scraped time
        scraped_time = response.css('div.text-sm.text-gray-darker.font-medium span::text').get() or datetime.now().isoformat()

        # Clean title for display
        display_title = title[:60] + "..." if title and len(title) > 60 else title or "Անանուն հոդված"
        
        # Only process if we have meaningful content
        if content and len(content.strip()) > 50:  # Ensure minimum content length
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
        else:
            self.logger.info(f"⚠️ Անբավարար պարունակություն: {display_title}")
            # Mark as processed to avoid re-checking
            self.mark_article_processed(response.url, title)

    def closed(self, reason):
        """Called when spider finishes"""
        self.logger.info(f"""
📊 ԱՄՓՈՓՈՒՄ HAYASTAN.NEWS (օպտիմիզացված - միայն 10 հոդված):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Կրկնություն հոդվածներ: {self.duplicate_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip()) 