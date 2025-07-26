import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import requests
import redis
from datetime import datetime

    

class PastinfoSpider(scrapy.Spider):
    name = "pastinfo"
    allowed_domains = ["pastinfo.am"]
    start_urls = ["https://pastinfo.am/hy"]
    
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
        super(PastinfoSpider, self).__init__(*args, **kwargs)
        
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
        cache_key = f"processed_pastinfo:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_pastinfo:{article_hash}"
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

    def clean_text_fragments(self, text_fragments):
        """Clean text fragments by removing unwanted elements"""
        if not text_fragments:
            return []
        
        # Unwanted fragments to filter out
        unwanted_fragments = [
            # Navigation elements
            "•", "›", "→", "←", "↑", "↓", "▪", "▫", "■", "□", "◆", "◇", "○", "●",
            # Social media and sharing
            "share", "facebook", "twitter", "instagram", "youtube", "telegram", "whatsapp", "viber",
            "like", "comment", "follow", "subscribe", "պահանջել", "կիսվել", "նշել", "հավանել",
            # Armenian navigation terms
            "գլխավոր", "մենյու", "կարդալ ավելին", "ավել", "բաց", "փակ", "վերադառնալ",
            "հաջորդ", "նախորդ", "ցույց տալ", "թաքցնել", "բացել", "տեղեկատվություն",
            # Pastinfo.am specific elements
            "pastinfo.am", "pastinfo", "կայք", "կապ", "հեռախոս", "հասցե", "էլ-փոստ",
            "հաղորդագրություն", "գրանցվել", "մուտք", "ծանուցում", "գովազդ", "պատուհան",
            # Copyright and legal
            "© 2024", "© 2023", "© pastinfo", "copyright", "հեղինակային", "իրավունք",
            "terms", "privacy", "policy", "կանոն", "գաղտնիություն", "պայմաններ",
            # Ad-related terms
            "advertisement", "sponsored", "ad", "ads", "գովազդ", "ծանուցումներ",
            # Common website elements
            "read more", "see more", "show more", "load more", "կարդալ ավելին",
            "մանրամասն", "տեսնել", "ցույց տալ", "բեռնել", "ավելացնել", "փնտրել",
            # Date/time navigation
            "today", "yesterday", "tomorrow", "week", "month", "year", "այսօր", "երեկ", "վաղը",
            # Click baits
            "click here", "այստեղ սեղմել", "սեղմել", "մուտք գործել", "անցնել",
            # Archive and categories
            "archive", "category", "tag", "արխիվ", "կատեգորիա", "պիտակ",
            # Comments and user interactions
            "comments", "reply", "post", "մեկնաբանություն", "պատասխանել", "գրառում",
            # Search and filtering
            "search", "filter", "sort", "փնտրել", "ֆիլտր", "դասակարգել",
            # Newsletter and subscriptions
            "newsletter", "subscription", "բաժանորդագրություն", "ծանուցում", "տեղեկագիր",
            # Contact information
            "contact", "phone", "email", "address", "կապ", "հեռախոս", "էլ-փոստ", "հասցե",
            # Other unwanted elements
            "loading", "error", "404", "500", "բեռնում", "սխալ", "տեխնիկական",
            # Empty or single character elements
            " ", "\n", "\t", "\r", ".", ",", ":", ";", "!", "?", "-", "_", "|", "/", "\\",
            # Browser and technical
            "javascript", "css", "html", "enable", "disable", "միացնել", "անջատել",
            # Weather/external widgets
            "weather", "temperature", "forecast", "եղանակ", "ջերմաստիճան",
            # URLs and links
            "http", "https", "www", ".com", ".am", ".org", ".net", "url", "link",
            # Time stamps without context
            "am", "pm", "գիշեր", "ցերեկ", "առավոտ", "երեկո",
            # Single letters or numbers
            "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m", "n", "o", "p",
            "q", "r", "s", "t", "u", "v", "w", "x", "y", "z", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0"
        ]
        
        cleaned_fragments = []
        for fragment in text_fragments:
            if not fragment or not fragment.strip():
                continue
                
            cleaned_fragment = fragment.strip()
            
            # Skip if fragment is too short (less than 3 characters)
            if len(cleaned_fragment) < 3:
                continue
                
            # Skip if fragment matches unwanted patterns
            if any(unwanted.lower() in cleaned_fragment.lower() for unwanted in unwanted_fragments):
                continue
                
            # Skip if fragment is mostly punctuation
            if len([c for c in cleaned_fragment if c.isalnum()]) < len(cleaned_fragment) * 0.5:
                continue
                
            # Skip if fragment doesn't contain any Armenian or English letters
            if not any(c.isalpha() for c in cleaned_fragment):
                continue
                
            # Skip if fragment is just numbers or dates without context
            if cleaned_fragment.replace(" ", "").replace(".", "").replace("-", "").replace("/", "").isdigit():
                continue
                
            # Skip very short fragments that are likely navigation
            if len(cleaned_fragment.split()) < 2 and len(cleaned_fragment) < 10:
                continue
                
            cleaned_fragments.append(cleaned_fragment)
        
        return cleaned_fragments

    def parse(self, response):
        # Extract articles from the news_block structure
        articles = response.css("div.news_block")
        
        # Also try alternative selectors if not found
        if not articles:
            articles = (response.css("div.news-block") or
                       response.css("article") or 
                       response.css("div.news-item") or
                       response.css("div[class*='news']"))
        
        # Optimize: limit to only latest 10 articles
        articles = articles[:10]
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված (սահմանափակված 10-ով)")

        for article in articles:
            # Extract link from the news_title structure
            link = article.css("a.news_title::attr(href)").get()
            title_preview = article.css("a.news_title::text").get()
            
            # If not found in news_title, try alternative selectors
            if not link:
                link = article.css("a::attr(href)").get()
            if not title_preview:
                title_preview = article.css("a::text").get()
            
            if link and title_preview:
                full_url = response.urljoin(link)
                
                # Check Redis cache first
                if self.is_article_processed(full_url, title_preview):
                    self.cached_skips += 1
                    continue
                    
                yield scrapy.Request(full_url, callback=self.parse_article)

    def parse_article(self, response):
        self.processed_articles += 1

        # Hierarchical title extraction with fallback
        title = None
        title_selectors = [
            "h1.entry-title::text",
            "h1.post-title::text", 
            "h1.article-title::text",
            "h1.title::text",
            "h1::text",
            ".entry-title::text",
            ".post-title::text",
            ".article-title::text",
            ".title::text",
            "meta[property='og:title']::attr(content)",
            "title::text"
        ]
        
        for selector in title_selectors:
            title = response.css(selector).get()
            if title and title.strip() and len(title.strip()) > 5:
                title = title.strip()
                break
        
        # If no proper title found, generate one
        if not title:
            title = f"Article from {response.url.split('/')[-1]}"

        # Article-specific content extraction - target only paragraph content
        content_parts = []
        
        # Primary content selectors targeting article paragraphs
        content_selectors = [
            "div.entry-content p::text",
            "div.post-content p::text",
            "div.article-content p::text",
            "div.content p::text",
            "article p::text",
            "div.main-content p::text",
            ".entry-content p::text",
            ".post-content p::text",
            ".article-content p::text",
            ".content p::text",
            "article p::text",
            ".main-content p::text"
        ]
        
        # Try each selector until content is found
        for selector in content_selectors:
            content_parts = response.css(selector).getall()
            if content_parts:
                break
        
        # If no content found with specific selectors, try more general ones
        if not content_parts:
            content_parts = response.css("p::text").getall()
        
        # Clean the extracted content
        cleaned_content = self.clean_text_fragments(content_parts)
        content = "\n".join(cleaned_content)

        # Skip if content is too short or empty
        if not content or len(content.strip()) < 50:
            self.logger.info(f"❌ Շատ կարճ բովանդակություն: {title[:50]}...")
            return

        # Extract scraped time - try multiple selectors
        scraped_time = (response.css('time::attr(datetime)').get() or
                       response.css('time::text').get() or 
                       response.css('.date::text').get() or 
                       response.css('.news_date::text').get() or 
                       response.css('.publish-date::text').get() or 
                       response.css('.post-date::text').get() or
                       datetime.now().isoformat())

        # Clean title for display
        display_title = title[:60] + "..." if title and len(title) > 60 else title or "Անանուն հոդված"
        
        if self.article_contains_keyword(title) or self.article_contains_keyword(content):
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
        """Called when spider finishes"""
        self.logger.info(f"""
📊 ԱՄՓՈՓՈՒՄ PASTINFO.AM (օպտիմիզացված - միայն 10 հոդված):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Կրկնվող հոդվածներ: {self.duplicate_articles}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip()) 