import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import redis
from datetime import datetime, timedelta

import requests

class FiveTvSpider(scrapy.Spider):
    name = "5tv"
    allowed_domains = ["5tv.am", "news.5tv.am"]
    start_urls = ["https://news.5tv.am/"]
    
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
        super(FiveTvSpider, self).__init__(*args, **kwargs)
        
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
        self.cached_skips = 0

    def is_article_processed(self, url, title):
        """Check if article was already processed using Redis cache"""
        if not self.redis_client:
            return False
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_5tv:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_5tv:{article_hash}"
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
        # Extract articles using the 5tv.am specific structure
        # Based on the HTML: ul.feed_list > li > div.block_feed > a.info_block
        articles = response.css("ul.feed_list li div.block_feed")
        
        # Optimize: limit to only latest 15 articles for 5tv
        articles = articles[:15]
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված (սահմանափակված 15-ով)")

        for article in articles:
            # Extract link and title using 5tv.am structure
            link = article.css("a.info_block::attr(href)").get()
            title_preview = article.css("a.info_block span.news_title::text").get()
            category = article.css("div.category_block::text").get()
            
            if link and title_preview:
                full_url = response.urljoin(link)
                
                # Check Redis cache first
                if self.is_article_processed(full_url, title_preview):
                    self.cached_skips += 1
                    continue
                    
                # Pass category as meta data
                yield scrapy.Request(full_url, callback=self.parse_article, 
                                   meta={'category': category, 'preview_title': title_preview})

    def clean_text_fragments(self, text_fragments):
        """Clean text fragments by removing unwanted navigation and UI elements"""
        if not text_fragments:
            return []
        
        # Very aggressive unwanted fragments list for 5tv.am
        unwanted_fragments = [
            # Navigation and UI elements
            "Լրահոս", "Տնտեսական", "Հասարակական", "Քաղաքական", "Իրավական", 
            "Տարածաշրջան", "Միջազգային", "Մշակույթ", "Հայաստան", "Սպորտ",
            "Կարդացեք նաև", "Կարդալ ավելին", "Ավելին", "Կարդալ",
            "news.5tv.am", "5tv.am", "5TV", "5tv", "5 TV",
            
            # Time stamps and dates
            "21:00", "20:29", "20:10", "19:00", "18:30", "18:09", "16:34", "16:19",
            "15:41", "15:00", "14:30", "14:01", "13:49", "13:30", "13:11", "12:49",
            "12:34", "11:51", "11:30", "11:23", "10:58", "10:29", "10:00", "00:01",
            "23:58", "23:45", "23:30", "23:15", "23:00", "22:45", "22:30", "22:15",
            "22:00", "21:45", "21:30",
            
            # Date formats
            "13.07.25", "12.07.25", "11.07.25", "10.07.25", "09.07.25",
            ".07.25", ".07.2025", "/07/25", "/07/2025",
            
            # Social media and sharing
            "share", "facebook", "twitter", "instagram", "telegram", "youtube",
            "Կիսվել", "Տարածել", "Հղում", "Տարածել սոցիալական ցանցերում",
            
            # Footer and copyright
            "© 2025", "© 2024", "Copyright", "Բոլոր իրավունքները", "Հեղինակային իրավունք",
            
            # Advertisement and promo
            "Գովազդ", "Ռեկլամ", "Promo", "Sponsored", "Մասնագետի կարծիք",
            
            # Navigation bullets and arrows
            "►", "▼", "▲", "◄", "→", "←", "↑", "↓", "•", "·", "‣", "⚫", "⚪",
            
            # Comments and interaction
            "Մեկնաբանություններ", "Գրել մեկնաբանություն", "Հավանել", "Չհավանել",
            "Գնահատել", "Պատասխանել", "Մեկնաբանել",
            
            # Page structure
            "Գլխավոր", "Տնային", "Անցնել բովանդակությանը", "Մենյու", "Փնտրում",
            "Ցանկ", "Թեմներ", "Բաժիններ", "Ավտոր", "Հեղինակ",
            
            # News feed indicators
            "Վերջին նորություններ", "Այլ նորություններ", "Համանման նորություններ",
            "Առաջարկվող", "Պատկեր", "Ֆոտո", "Տեսանյութ", "Վիդեո",
            
            # Article count indicators
            "47", "4 475", "146", "27", # These are view counts that appear in articles
            
            # End of article indicators
            "Կարդացեք նաև", "Նմանատիպ նյութեր", "Գիտեք նաև", "Կարծիք",
            "Անդրադարձ", "Մեկնաբանություն", "Գրանցվել", "Մուտք",
            
            # Technical elements
            "JavaScript", "CSS", "HTML", "404", "Error", "Page not found"
        ]
        
        # End of article indicators - stop processing after these
        end_indicators = [
            "Կարդացեք նաև", "Կարդալ ավելին", "Նմանատիպ նյութեր",
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
            if len(fragment) < 10:
                continue
            
            # Skip fragments that are mostly digits and periods (dates/times)
            if len(fragment) < 20 and any(char.isdigit() or char in '.:-' for char in fragment):
                continue
            
            cleaned_fragments.append(fragment)
        
        return cleaned_fragments

    def parse_article(self, response):
        self.processed_articles += 1

        # Get category and preview title from meta
        category = response.meta.get('category', '')
        preview_title = response.meta.get('preview_title', '')

        # Hierarchical title extraction with fallback
        title = None
        title_selectors = [
            "h1.article-title::text",
            "h1.post-title::text", 
            "h1.entry-title::text",
            "h1.news-title::text",
            "h1.title::text",
            "h1::text",
            ".article-title::text",
            ".post-title::text",
            ".entry-title::text", 
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
                title = title.replace("5TV.AM", "").replace("5tv.am", "").replace("5TV", "").strip()
                # Remove common separators
                title = title.replace(" | ", "").replace(" - ", "").replace(" :: ", "").strip()
                break
        
        # Fallback to preview title if no title found
        if not title:
            title = preview_title
        
        # Very specific content extraction for 5tv.am
        content_parts = []
        
        # Try to find the main article container first
        main_article_container = response.css("article") or response.css(".article-content") or response.css(".post-content")
        
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
                ".article-content p::text",
                ".post-content p::text",
                ".entry-content p::text",
                ".content p::text",
                ".main-content p::text",
                ".news-content p::text",
                ".article-body p::text",
                ".post-body p::text",
                ".text p::text",
                ".description p::text"
            ]
            
            for selector in content_selectors:
                content_parts = response.css(selector).getall()
                if content_parts and len(content_parts) >= 2:
                    break
        
        # Final fallback: get all paragraphs but filter heavily
        if not content_parts or len(content_parts) < 2:
            content_parts = response.css("p::text").getall()
        
        # Clean the content fragments
        cleaned_content = self.clean_text_fragments(content_parts)
        
        # Join cleaned content
        content = "\n".join(cleaned_content)
        
        # Content validation
        if not content or len(content) < 50:
            content = f"[Կարճ բովանդակություն] {' '.join(cleaned_content[:3])}"
        
        # Check for meaningful content
        words = content.split()
        meaningful_words = [word for word in words if len(word) > 2 and not word.isdigit()]
        
        if len(words) < 10 or len(meaningful_words) < 5:
            content = f"[Վավերացում չանցած] {content[:100]}..."
        
        # Include category in content for keyword matching
        full_content_for_keywords = f"{title} {content} {category}".strip()

        # Extract scraped time - try multiple selectors for 5tv.am
        scraped_time = (response.css('time::attr(datetime)').get() or
                       response.css('time::text').get() or 
                       response.css('.date::text').get() or
                       response.css('.news_date .date_block::text').get() or
                       response.css('.news_date .hour_block::text').get() or
                       response.css('.publish-date::text').get() or
                       response.css('.post-date::text').get() or
                       response.css('.entry-date::text').get() or
                       datetime.now().isoformat())

        # Clean title for display
        display_title = title[:60] + "..." if title and len(title) > 60 else title or "Անանուն հոդված"
        
        if self.article_contains_keyword(full_content_for_keywords):
            self.logger.info(f"✅ Բանալի բառ գտնվեց [{category}]: {display_title}")
            # Mark as processed only after successful keyword match
            self.mark_article_processed(response.url, title)
            self.new_articles += 1
            
            item = NewsScraperItem()
            item['title'] = title or f'5TV Article from {response.url.split("/")[-1]}'
            item['link'] = response.url
            item['source_url'] = response.url
            item['content'] = f"[{category}] {content}" if category else content
            item['scraped_time'] = scraped_time
            yield item
        else:
            self.logger.info(f"❌ Բանալի բառ չգտնվեց [{category}]: {display_title}")
            # Mark as processed even if no keyword match to avoid re-checking
            self.mark_article_processed(response.url, title)

    def closed(self, reason):
        """Called when spider finishes"""
        self.logger.info(f"""
📊 ԱՄՓՈՓՈՒՄ 5TV.AM (ԽՈՒՄԲ 3 - Մասնագիտացված սայտեր):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Խումբ: 3 (Մասնագիտացված)
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip()) 