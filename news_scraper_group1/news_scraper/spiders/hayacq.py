import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import requests
import redis
from datetime import datetime

    

class HayacqSpider(scrapy.Spider):
    name = "hayacq"
    allowed_domains = ["hayacq.com"]
    start_urls = ["https://hayacq.com/"]
    
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
        super(HayacqSpider, self).__init__(*args, **kwargs)
        
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
        """Check if article already processed using Redis"""
        if not self.redis_client:
            return False
        
        cache_key = f"hayacq_article:{hashlib.md5(f'{url}:{title}'.encode()).hexdigest()}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis with 7-day expiration"""
        if not self.redis_client:
            return
        
        cache_key = f"hayacq_article:{hashlib.md5(f'{url}:{title}'.encode()).hexdigest()}"
        self.redis_client.setex(cache_key, 7 * 24 * 3600, "processed")  # 7 days

    def article_contains_keyword(self, article_text):
        """Check if article contains any keywords"""
        if not self.keywords:
            return True  # If no keywords defined, process all articles
        
        article_text_lower = article_text.lower()
        return any(keyword in article_text_lower for keyword in self.keywords)

    def parse(self, response):
        # Extract articles using the hayacq.com structure
        articles = response.css('article.short-news')
        
        # Optimize: limit to only latest 10 articles
        articles = articles[:10]
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված hayacq.com-ում (սահմանափակված 10-ով)")

        for article in articles:
            # Extract link and title using hayacq structure
            link = article.css('div.short-news-content h1 a::attr(href)').get()
            title = article.css('div.short-news-content h1 a::text').get()
            
            if link and title:
                full_url = response.urljoin(link)
                title = title.strip()
                
                # Check Redis cache first
                if self.is_article_processed(full_url, title):
                    self.cached_skips += 1
                    continue
                    
                yield scrapy.Request(full_url, callback=self.parse_article, 
                                   meta={'title': title, 'source_url': full_url})

    def parse_article(self, response):
        self.processed_articles += 1
        
        title = response.meta.get('title', '')
        source_url = response.meta.get('source_url', '')
        
        # More specific title selectors for hayacq.com - avoid generic selectors
        if not title:
            title = (response.css("article h1::text").get() or
                    response.css(".article-title::text").get() or
                    response.css("h1.post-title::text").get() or
                    response.css("h1.entry-title::text").get() or
                    response.css("h1.title::text").get() or
                    response.css("h1::text").get() or
                    response.css("meta[property='og:title']::attr(content)").get())
        
        # More specific content selectors for hayacq.com - target only article content
        content_parts = []
        
        # Try different content selectors specifically for hayacq.com
        content_selectors = [
            "article .full-text p::text",
            "article .content p::text",
            "article .article-content p::text",
            "article .post-content p::text",
            "article .entry-content p::text",
            ".full-text p::text",
            ".content p::text",
            ".article-content p::text",
            ".post-content p::text",
            ".entry-content p::text",
            "main article p::text",
            ".main-content p::text",
            ".article-body p::text",
            ".news-content p::text",
            ".story-content p::text"
        ]
        
        for selector in content_selectors:
            content_parts = response.css(selector).getall()
            if content_parts:
                break
        
        # If no paragraphs found, try div selectors but be more specific
        if not content_parts:
            div_selectors = [
                "article .full-text ::text",
                "article .content ::text",
                "article .article-content ::text",
                "article .post-content ::text",
                "article .entry-content ::text",
                ".full-text ::text",
                ".content ::text",
                ".article-content ::text",
                ".post-content ::text",
                ".entry-content ::text",
                "main article ::text",
                ".main-content ::text",
                ".article-body ::text",
                ".news-content ::text"
            ]
            
            for selector in div_selectors:
                content_parts = response.css(selector).getall()
                if content_parts:
                    break
        
        # Filter out navigation, footer, and other unwanted elements
        if content_parts:
            # Remove text from navigation, footer, sidebar, comments, etc.
            filtered_content = []
            for text in content_parts:
                text = text.strip()
                # Skip empty text, navigation items, and common unwanted elements
                if (text and 
                    len(text) > 5 and  # Skip very short text
                    not text.startswith('•') and  # Skip bullet points from navigation
                    not text.startswith('→') and  # Skip navigation arrows
                    not text.startswith('►') and  # Skip navigation arrows
                    not text.startswith('»') and  # Skip navigation arrows
                    not text.startswith('›') and  # Skip navigation arrows
                    not text.startswith('◆') and  # Skip diamond bullets
                    not text.lower().startswith('կարդալ') and  # Skip "read more" links
                    not text.lower().startswith('share') and  # Skip share buttons
                    not text.lower().startswith('comment') and  # Skip comment sections
                    not text.lower().startswith('բաց') and  # Skip "open" links
                    not text.lower().startswith('menu') and  # Skip menu items
                    not text.lower().startswith('search') and  # Skip search elements
                    not text.lower().startswith('տեղեկատվական') and  # Skip info sections
                    not text.lower().startswith('գովազդ') and  # Skip ads
                    not text.lower().startswith('advertising') and  # Skip ads
                    not text.lower().startswith('advertisement') and  # Skip ads
                    not text.lower().startswith('sponsored') and  # Skip sponsored content
                    not text.lower().startswith('related') and  # Skip related articles
                    not text.lower().startswith('see also') and  # Skip see also links
                    not text.lower().startswith('read also') and  # Skip read also links
                    not text.lower().startswith('contact') and  # Skip contact info
                    not text.lower().startswith('follow') and  # Skip follow links
                    not text.lower().startswith('ետևյալ') and  # Skip "next" links
                    not text.lower().startswith('հետևյալ') and  # Skip "next" links
                    not text.lower().startswith('նոր նյութեր') and  # Skip "new materials"
                    not text.lower().startswith('այլ լուրեր') and  # Skip "other news"
                    not text.lower().startswith('bookmark') and  # Skip bookmark links
                    not text.lower().startswith('print') and  # Skip print links
                    not text.lower().startswith('navigation') and  # Skip navigation
                    not text.lower().startswith('բաժիններ') and  # Skip "sections"
                    not text.lower().startswith('կատեգորիաներ') and  # Skip "categories"
                    not text.lower().startswith('tags') and  # Skip tags
                    not text.lower().startswith('թեգեր') and  # Skip tags
                    not text.lower().startswith('home') and  # Skip home links
                    not text.lower().startswith('գլխավոր') and  # Skip home links
                    not text.lower().startswith('back to') and  # Skip back links
                    not text.lower().startswith('վերադարձ') and  # Skip back links
                    not text.lower().startswith('more') and  # Skip more links
                    not text.lower().startswith('ավելին') and  # Skip more links
                    not text.lower().startswith('all') and  # Skip all links
                    not text.lower().startswith('բոլոր') and  # Skip all links
                    'facebook' not in text.lower() and  # Skip social media
                    'twitter' not in text.lower() and
                    'instagram' not in text.lower() and
                    'telegram' not in text.lower() and
                    'youtube' not in text.lower() and
                    'whatsapp' not in text.lower() and
                    'linkedin' not in text.lower() and
                    'tiktok' not in text.lower() and
                    'viber' not in text.lower() and
                    'messenger' not in text.lower() and
                    'copyright' not in text.lower() and  # Skip copyright
                    '©' not in text and  # Skip copyright symbol
                    'terms' not in text.lower() and  # Skip terms
                    'privacy' not in text.lower() and  # Skip privacy policy
                    'cookie' not in text.lower() and  # Skip cookie policy
                    'subscribe' not in text.lower() and  # Skip subscription calls
                    'newsletter' not in text.lower() and  # Skip newsletter signup
                    'source:' not in text.lower() and  # Skip source attribution
                    'photo:' not in text.lower() and  # Skip photo credits
                    'image:' not in text.lower()):  # Skip image credits
                    filtered_content.append(text)
            
            content = "\n".join(filtered_content)
        else:
            content = ""
        
        # Clean title for display
        display_title = title[:60] + "..." if title and len(title) > 60 else title or "Անանուն հոդված"
        
        # Check if article contains keywords
        full_text = f"{title} {content}"
        if self.article_contains_keyword(full_text):
            self.logger.info(f"✅ Բանալի բառ գտնվեց: {display_title}")
            # Mark as processed only after successful keyword match
            self.mark_article_processed(source_url, title)
            self.new_articles += 1
            
            # Create item
            item = NewsScraperItem()
            item['title'] = title or f'Article from {source_url.split("/")[-1]}'
            item['content'] = content
            item['source_url'] = source_url
            item['link'] = source_url
            item['scraped_time'] = datetime.now().isoformat()
            
            yield item
        else:
            self.logger.info(f"❌ Բանալի բառ չգտնվեց: {display_title}")
            # Mark as processed even if no keyword match to avoid re-checking
            self.mark_article_processed(source_url, title)

    def closed(self, reason):
        self.logger.info(f"""
📊 ԱՄՓՈՓՈՒՄ HAYACQ.COM (օպտիմիզացված - միայն 10 հոդված):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Կրկնություններ: {self.duplicate_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip()) 