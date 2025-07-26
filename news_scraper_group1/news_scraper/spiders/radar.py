import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import redis
from datetime import datetime
import requests


class RadarSpider(scrapy.Spider):
    name = "radar"
    allowed_domains = ["radar.am"]
    start_urls = [
        "https://www.radar.am/hy/",
        "https://www.radar.am/hy/feed/",
        "https://www.radar.am/hy/news/politics/",
        "https://www.radar.am/hy/news/social/",
        "https://www.radar.am/hy/news/world/",
        "https://www.radar.am/hy/news/karabagh/"
    ]
    
    # Add custom headers to bypass potential blocking
    custom_settings = {
        'USER_AGENT': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'DEFAULT_REQUEST_HEADERS': {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'hy-AM,hy;q=0.9,en-US;q=0.8,en;q=0.7,ru;q=0.6',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Cache-Control': 'max-age=0',
            'Referer': 'https://www.radar.am/'
        },
        'DOWNLOAD_DELAY': 1,
        'RANDOMIZE_DOWNLOAD_DELAY': True,
    }

    def __init__(self, *args, **kwargs):
        super(RadarSpider, self).__init__(*args, **kwargs)
        
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
        cache_key = f"processed_radar:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_radar:{article_hash}"
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
        # Extract articles using the radar.am structure
        # Main structure: div.news-section__newsblog > div.news_feed_item.news-section__filter-item
        articles = response.css("div.news_feed_item.news-section__filter-item")
        
        if not articles:
            # Fallback: try other news structures
            articles = (response.css("div.news_feed_item") or
                       response.css(".news-section__filter-item") or
                       response.css("article") or 
                       response.css(".news-item"))
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված ({response.url}) (օպտիմիզացված - սահմանափակված 10-ով)")
        
        # Limit to latest 10 articles only for optimization (running every 10 minutes)
        articles = articles[:10]

        for article in articles:
            # Extract link from the article
            link = article.css("a::attr(href)").get()
            
            # Extract title from div.wrap-context > p
            title = (article.css("div.wrap-context p::text").get() or
                    article.css("p::text").get() or
                    article.css(".wrap-context::text").get() or
                    article.css("h3::text").get() or
                    article.css("h2::text").get())
            
            # Extract date/time
            date_time = article.css("span::text").get()
            
            if link and title:
                full_url = response.urljoin(link)
                title = title.strip()
                
                # Check Redis cache first
                if self.is_article_processed(full_url, title):
                    self.cached_skips += 1
                    continue
                    
                yield scrapy.Request(full_url, callback=self.parse_article, 
                                   meta={'article_date': date_time})

        # Skip pagination for optimization (only process first page with latest 10 articles)
        self.logger.info("📄 Pagination բաց թողնվում է օպտիմիզացիայի համար (միայն 10 վերջին հոդվածներ)")

    def parse_article(self, response):
        self.processed_articles += 1

        # Try multiple title selectors for radar.am
        title = (response.css("article h1::text").get() or
                response.css("main h1::text").get() or
                response.css("div.content h1::text").get() or
                response.css("div.article h1::text").get() or
                response.css("div.post h1::text").get() or
                response.css("h1.title::text").get() or
                response.css("h1.article-title::text").get() or
                response.css("h1.entry-title::text").get() or
                response.css("h1.post-title::text").get() or
                response.css("h1::text").get() or
                response.css("meta[property='og:title']::attr(content)").get() or
                response.css("title::text").get())
        
        # Store original title for debugging
        original_title = title
        
        # Clean title (remove site name if present)
        if title:
            # Remove various site prefixes and suffixes
            title = title.replace("Radar.am | ", "").replace("RADAR.AM | ", "").strip()
            title = title.replace("Radar.am", "").replace("RADAR.AM", "").strip()
            title = title.replace("Հայաստանի նորություններ", "").replace("Armenian News", "").strip()
            title = title.replace("Radar - ", "").replace(" - Radar", "").strip()
            
            # Remove leading/trailing dashes and pipes
            title = title.strip(" -|")
            
            # Check if title is just site name variations
            if title in ["Radar.am", "RADAR.AM", "Radar", "RADAR", "Հայաստանի նորություններ", "Armenian News", "", "-", "|"]:
                title = None
        
        # Try multiple content selectors for radar.am - focus on paragraphs to avoid header/footer
        content_parts = (response.css("div.article-content p::text").getall() or
                        response.css(".entry-content p::text").getall() or
                        response.css(".post-content p::text").getall() or
                        response.css(".content p::text").getall() or
                        response.css("article p::text").getall() or
                        response.css(".main-content p::text").getall() or
                        response.css(".article-body p::text").getall() or
                        response.css(".news-content p::text").getall() or
                        response.css("div.post-body p::text").getall() or
                        response.css("div.news-text p::text").getall() or
                        response.css("main p::text").getall() or
                        response.css("p::text").getall())
        
        # Filter out very short text snippets (likely navigation/header elements)
        filtered_content = []
        for text in content_parts:
            text = text.strip()
            # Only keep text that's longer than 30 characters (real article content)
            if text and len(text) > 30:
                filtered_content.append(text)
        
        content = "\n".join(filtered_content)

        # Extract scraped time - try multiple selectors
        scraped_time = (response.css('time::attr(datetime)').get() or
                       response.css('time::text').get() or 
                       response.css('.date::text').get() or 
                       response.css('.publish-date::text').get() or
                       response.css('.post-date::text').get() or
                       response.css('.article-date::text').get() or
                       response.meta.get('article_date') or
                       datetime.now().isoformat())

        # Clean title for display and debugging
        display_title = title[:60] + "..." if title and len(title) > 60 else title or "Անանուն հոդված"
        
        # Debug: Show original title if it was filtered out
        if not title and original_title:
            self.logger.warning(f"🔍 Ֆիլտրված title (site title): {original_title}")
        
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
📊 ԱՄՓՈՓՈՒՄ RADAR.AM (օպտիմիզացված - միայն 10 հոդված):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip())