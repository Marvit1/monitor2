import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import redis
from datetime import datetime
import requests

class AlphanewsSpider(scrapy.Spider):
    name = "alphanews"
    allowed_domains = ["alphanews.am"]
    start_urls = [
        "https://alphanews.am/",
        "https://alphanews.am/lrahos/",
        "https://alphanews.am/category/politics/",
        "https://alphanews.am/category/economics/",
        "https://alphanews.am/category/culture/"
    ]
    
    # Add custom headers to bypass potential blocking
    custom_settings = {
        'USER_AGENT': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'DEFAULT_REQUEST_HEADERS': {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'hy-AM,hy;q=0.9,en-US;q=0.8,en;q=0.7,ru;q=0.6',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Cache-Control': 'max-age=0',
        },
        'DOWNLOAD_DELAY': 1,
        'RANDOMIZE_DOWNLOAD_DELAY': True,
    }

    def __init__(self, *args, **kwargs):
        super(AlphanewsSpider, self).__init__(*args, **kwargs)
        
        # Redis connection
        try:
            self.redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
            self.redis_client.ping()
            self.logger.info("🔴 Redis կապակցված է")
        except Exception as e:
            self.logger.warning(f"🔴 Redis չկա, կաշխատի առանց cache: {e}")
            self.redis_client = None

        # API client
        self.api_base_url = os.environ.get('API_BASE_URL', 'https://beackkayq.onrender.com')
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
        self.duplicate_articles = 0  # Add missing counter used by pipeline
        self.cached_skips = 0

    def is_article_processed(self, url, title):
        """Check if article was already processed using Redis cache"""
        if not self.redis_client:
            return False
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_alphanews:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_alphanews:{article_hash}"
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
        # Extract articles using the alphanews.am structure
        # Main structure: div.widget_news_posts > div.col-sm-12 > article.aside__article
        articles = response.css("article.aside__article")
        
        if not articles:
            # Fallback: try other common news structures
            articles = response.css("article") or response.css(".post") or response.css(".news-item")
        
        # Limit to latest 10 articles only for optimization (running every 10 minutes)
        articles = articles[:10]
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված (սահմանափակված 10-ով) ({response.url})")

        for article in articles:
            # Extract link from the article
            link = article.css("a::attr(href)").get()
            
            # Extract title from h3.h4 or other title selectors
            title = (article.css("h3.h4::text").get() or
                    article.css("h3::text").get() or
                    article.css("h2::text").get() or
                    article.css(".title::text").get() or
                    article.css("figcaption h3::text").get())
            
            if link and title:
                full_url = response.urljoin(link)
                title = title.strip()
                
                # Check Redis cache first
                if self.is_article_processed(full_url, title):
                    self.cached_skips += 1
                    continue
                    
                yield scrapy.Request(full_url, callback=self.parse_article)

        # Additional article links removed - only processing latest 10 articles for optimization

    def parse_article(self, response):
        self.processed_articles += 1

        # Try multiple title selectors for alphanews.am
        title = (response.css("h1::text").get() or
                response.css(".entry-title::text").get() or
                response.css(".post-title::text").get() or
                response.css(".article-title::text").get() or
                response.css("h1.h1::text").get() or
                response.css("meta[property='og:title']::attr(content)").get() or
                response.css("title::text").get())
        
        # Optimized content selectors - only get main article content
        content_selectors = [
            "div.entry-content p::text",
            ".post-content p::text",
            ".article-content p::text",
            ".content p::text",
            "article p::text",
            ".main-content p::text",
            ".single-content p::text"
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
        
        # Clean content - remove navigation, ads, and other unwanted elements
        if not content_parts:
            # Fallback to all p tags if specific selectors fail
            content_parts = response.css("p::text").getall()
        
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
            "դասակարգիչ",
            "որպես",
            "Click here",
            "Մանրամասն",
            "Ավելի տեղեկություն",
            "Տեղեկություն",
            "Կարծիք",
            "Գրել",
            "Այլ",
            "Alphanews.am",
            "alphanews",
            "related",
            "հարակից",
            "կարգահանել",
            "պարազիտ",
            "keywords",
            "բանալի բառեր",
            "navigation",
            "նավիգացիա",
            "menu",
            "մենյու",
            "search",
            "որոնում",
            "footer",
            "ներքև",
            "header",
            "վերև"
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

        # Extract scraped time - try multiple selectors
        scraped_time = (response.css('time::attr(datetime)').get() or
                       response.css('time::text').get() or 
                       response.css('.date::text').get() or 
                       response.css('.publish-date::text').get() or 
                       response.css('.post-date::text').get() or
                       response.css('p.date::text').get() or
                       datetime.now().isoformat())

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
📊 ԱՄՓՈՓՈՒՄ ALPHANEWS.AM (օպտիմիզացված - միայն 10 հոդված):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Կրկնություն հոդվածներ: {self.duplicate_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip()) 