import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import redis
from datetime import datetime
import requests

class AnkakhSpider(scrapy.Spider):
    name = "ankakh"
    allowed_domains = ["ankakh.com"]
    start_urls = ["http://ankakh.com/"]
    
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
        super(AnkakhSpider, self).__init__(*args, **kwargs)
        
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
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_ankakh:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_ankakh:{article_hash}"
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
        # Extract articles from ankakh.com newsline structure
        articles = response.css("div.newsline div.article a")
        
        # Optimize: limit to latest 15 articles
        articles = articles[:15]
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված (սահմանափակված 15-ով)")

        for article in articles:
            link = article.css("::attr(href)").get()
            title = article.css("::text").get()
            
            if link and title:
                full_url = response.urljoin(link)
                
                # Check Redis cache first
                if self.is_article_processed(full_url, title):
                    self.cached_skips += 1
                    continue
                
                yield scrapy.Request(
                    full_url, 
                    callback=self.parse_article,
                    meta={'preview_title': title}
                )

    def clean_text_fragments(self, text_fragments):
        """Clean text fragments by removing unwanted navigation and UI elements"""
        if not text_fragments:
            return []
        
        # Very aggressive unwanted fragments list for ankakh.com
        unwanted_fragments = [
            # Navigation and UI elements
            "Հարցազրույցներ", "Նորություններ", "Գլխավոր", "Տնային", "Մենյու", "Փնտրում",
            "Այլ նորություններ", "Հետաքրքիր", "Կարևոր", "Անցյալ", "Ցանկ", "Բաժիններ",
            "Կատեգորիաներ", "Թեմներ", "Ավտոր", "Հեղինակ", "Լրագրող",
            
            # ankakh.com specific
            "ankakh.com", "Անկախ", "ANKAKH", "ankakh", "Ankakh",
            
            # Date and time stamps
            "13.07.25", "12.07.25", "11.07.25", "10.07.25", "09.07.25",
            "2025", "2024", "2023", "ժամ", "րոպե", "վայրկյան",
            
            # Social media and sharing
            "Կիսվել", "Տարածել", "Հղում", "Լայք", "Տարածել սոցիալական ցանցերում",
            "Facebook", "Twitter", "Instagram", "Telegram", "Youtube",
            "share", "facebook", "twitter", "instagram", "telegram", "youtube",
            
            # Footer and copyright
            "© 2025", "© 2024", "Copyright", "Բոլոր իրավունքները", "Հեղինակային իրավունք",
            "Պաշտպանված է", "Հեղինակային իրավունք", "Վերապրոդուկցիա",
            
            # Navigation bullets and arrows
            "►", "▼", "▲", "◄", "→", "←", "↑", "↓", "•", "·", "‣", "⚫", "⚪",
            
            # Comments and interaction
            "Մեկնաբանություններ", "Գրել մեկնաբանություն", "Կարծիք", "Մեկնաբանել",
            "Հավանել", "Չհավանել", "Գնահատել", "Պատասխանել", "Գրանցվել", "Մուտք",
            
            # Advertisement and promo
            "Գովազդ", "Ռեկլամ", "Promo", "Sponsored", "Մասնագետի կարծիք",
            "Ծանուցում", "Գովազդային", "Ռեկլամային",
            
            # Page structure and navigation
            "Անցնել բովանդակությանը", "Փակել", "Բացել", "Ցույց տալ", "Ցույց տալ ավելին",
            "Կարդալ ավելին", "Կարդալ", "Ավելին", "Դիտել", "Տեսնել", "Կարդացեք նաև",
            
            # News feed indicators
            "Վերջին նորություններ", "Համանման նորություններ", "Առաջարկվող",
            "Այլ կարծիքներ", "Նմանատիպ նյութեր", "Գիտեք նաև",
            
            # Technical elements
            "JavaScript", "CSS", "HTML", "404", "Error", "Page not found",
            "Loading", "Բեռնում", "Սպասեք", "Ներբեռնում",
            
            # End of article indicators
            "Գիտեք նաև", "Կարծիք", "Անդրադարձ", "Մեկնաբանություն",
            "Նմանատիպ նյութեր", "Այլ նյութեր", "Հետաքրքիր կարող է լինել"
        ]
        
        # End of article indicators - stop processing after these
        end_indicators = [
            "Կարդալ ավելին", "Կարդացեք նաև", "Նմանատիպ նյութեր",
            "Գիտեք նաև", "Մեկնաբանություններ", "Գրանցվել", "Մուտք"
        ]
        
        cleaned_fragments = []
        for fragment in text_fragments:
            if not fragment or not fragment.strip():
                continue
            
            fragment = fragment.strip()
            
            # Check for end indicators
            if any(end_indicator in fragment for end_indicator in end_indicators):
                break
            
            # Skip unwanted fragments
            if any(unwanted in fragment for unwanted in unwanted_fragments):
                continue
            
            # Skip very short fragments (likely UI elements)
            if len(fragment) < 15:
                continue
            
            # Skip fragments that are mostly digits and periods (dates/times)
            if len(fragment) < 30 and any(char.isdigit() or char in '.:-' for char in fragment):
                continue
            
            cleaned_fragments.append(fragment)
        
        return cleaned_fragments

    def parse_article(self, response):
        self.processed_articles += 1
        preview_title = response.meta.get('preview_title', '')

        # Hierarchical title extraction with fallback
        title = None
        title_selectors = [
            "h1.entry-title::text",
            "h1.post-title::text",
            "h1.article-title::text",
            "h1.news-title::text",
            "h1.title::text",
            "h1::text",
            ".entry-title::text",
            ".post-title::text",
            ".article-title::text",
            ".news-title::text",
            ".title::text",
            "meta[property='og:title']::attr(content)",
            "title::text"
        ]
        
        for selector in title_selectors:
            title = response.css(selector).get()
            if title:
                title = title.strip()
                # Remove site name from title
                title = title.replace("ankakh.com", "").replace("Ankakh", "").replace("ANKAKH", "").strip()
                # Remove common separators
                title = title.replace(" | ", "").replace(" - ", "").replace(" :: ", "").strip()
                break
        
        # Fallback to preview title if no title found
        if not title:
            title = preview_title
        
        # Very specific content extraction for ankakh.com
        content_parts = []
        
        # Try to find the main article container first
        main_article_container = response.css("article") or response.css(".entry-content") or response.css(".post-content")
        
        if main_article_container:
            # Extract ONLY paragraph content from the main article container
            content_parts = main_article_container.css("p::text").getall()
            
            # If no paragraphs found, try div text but be very selective
            if not content_parts or len(content_parts) < 2:
                content_parts = main_article_container.css("div::text").getall()
        
        # Fallback: try more specific selectors if main container approach fails
        if not content_parts or len(content_parts) < 2:
            # Use very specific selectors that target only the main article content
            content_selectors = [
                "article p::text",
                ".entry-content p::text",
                ".post-content p::text",
                ".article-content p::text",
                ".content p::text",
                ".main-content p::text",
                ".news-content p::text",
                ".article-body p::text",
                ".post-body p::text",
                ".text p::text",
                ".article_text p::text",
                ".description p::text"
            ]
            
            for selector in content_selectors:
                content_parts = response.css(selector).getall()
                if content_parts and len(content_parts) >= 2:
                    break
        
        # Final fallback: get paragraphs but filter very heavily
        if not content_parts or len(content_parts) < 2:
            content_parts = response.css("p::text").getall()
        
        # Clean the content fragments
        cleaned_content = self.clean_text_fragments(content_parts)
        
        # Join cleaned content
        content = "\n".join(cleaned_content)
        
        # Content validation
        if not content or len(content) < 100:
            content = f"[Կարճ բովանդակություն] {' '.join(cleaned_content[:3])}"
        
        # Check for meaningful content
        words = content.split()
        meaningful_words = [word for word in words if len(word) > 2 and not word.isdigit()]
        
        if len(words) < 15 or len(meaningful_words) < 8:
            content = f"[Վավերացում չանցած] {content[:150]}..."

        # Extract scraped time
        scraped_time = (response.css('time::attr(datetime)').get() or
                       response.css('time::text').get() or 
                       response.css('.date::text').get() or 
                       response.css('.publish-date::text').get() or
                       response.css('.post-date::text').get() or
                       response.css('.entry-date::text').get() or
                       datetime.now().isoformat())

        # Clean title for display
        display_title = title[:60] + "..." if title and len(title) > 60 else title or "Անանուն հոդված"
        
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
📊 ԱՄՓՈՓՈՒՄ ANKAKH.COM (օպտիմիզացված - միայն 15 հոդված):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Կրկնություններ: {self.duplicate_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip()) 