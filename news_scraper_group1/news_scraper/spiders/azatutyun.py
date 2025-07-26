import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import redis
from datetime import datetime, timedelta
import re

import requests

class AzatutyunSpider(scrapy.Spider):
    name = "azatutyun"
    allowed_domains = ["azatutyun.am"]
    start_urls = ["https://www.azatutyun.am/"]
    
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
        super(AzatutyunSpider, self).__init__(*args, **kwargs)
        
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
        self.duplicate_articles = 0

    def clean_text_fragments(self, text_fragments):
        """Clean text fragments by removing unwanted content"""
        if not text_fragments:
            return []
        
        # Comprehensive list of unwanted fragments
        unwanted_fragments = [
            # Navigation and UI elements
            'տպել', 'տպել էջը', 'տպել', 'տպել հոդվածը', 'print', 'share', 'share this article',
            'կիսվել', 'կիսվել սա', 'կիսվել facebook', 'կիսվել twitter', 'կիսվել telegram',
            'facebook', 'twitter', 'telegram', 'whatsapp', 'linkedin', 'instagram',
            
            # Footer and copyright
            'copyright', 'հեղինակային իրավունք', 'բոլոր իրավունքները պաշտպանված են',
            'all rights reserved', '© azatutyun', 'azatutyun.am', 'azatutyun',
            'ազատություն', 'ազատությունը', 'ազատ եվրոպա', 'rfe/rl',
            
            # Technical and metadata
            'html', 'css', 'javascript', 'jquery', 'bootstrap', 'meta', 'script',
            'գովազդ', 'advertisement', 'ads', 'sponsored', 'sponsored content',
            
            # Comments and social elements
            'մեկնաբանություններ', 'comments', 'comment', 'reply', 'replies',
            'like', 'dislike', 'հավանել', 'չհավանել', 'rating', 'rate',
            
            # Navigation terms
            'գլխավոր', 'մեկանավոր', 'home', 'main', 'menu', 'navigation',
            'բաժին', 'բաժիններ', 'category', 'categories', 'section', 'sections',
            'պիտակներ', 'tags', 'tag', 'archive', 'archives',
            
            # Common UI elements
            'ավելի', 'ավելի շատ', 'կարդալ ավելի', 'more', 'read more', 'continue reading',
            'նախորդ', 'հաջորդ', 'previous', 'next', 'back', 'forward',
            'վերադառնալ', 'վերցնել', 'return', 'go back', 'back to',
            
            # Date and time patterns that are standalone
            'ամսաթիվ', 'date', 'time', 'ժամ', 'րոպե', 'վայրկյան',
            'փետրվար', 'մարտ', 'ապրիլ', 'մայիս', 'հունիս', 'հուլիս',
            'օգոստոս', 'սեպտեմբեր', 'հոկտեմբեր', 'նոյեմբեր', 'դեկտեմբեր',
            'հունվար', 'january', 'february', 'march', 'april', 'may', 'june',
            'july', 'august', 'september', 'october', 'november', 'december',
            
            # Single letters or numbers
            'ա', 'բ', 'գ', 'դ', 'ե', 'զ', 'է', 'ը', 'թ', 'ժ',
            'ի', 'լ', 'խ', 'ծ', 'կ', 'հ', 'ձ', 'ղ', 'ճ', 'մ',
            'յ', 'ն', 'շ', 'ո', 'չ', 'պ', 'ջ', 'ռ', 'ս', 'վ',
            'տ', 'ր', 'ց', 'ւ', 'փ', 'ք', 'օ', 'ֆ',
            
            # Advertisement and promotional
            'գովազդ', 'անվճար', 'զեղչ', 'առաջարկ', 'ակցիա', 'special offer',
            'free', 'discount', 'sale', 'promo', 'promotion', 'advertisement',
            
            # Contact and subscription
            'կապ', 'կապնվել', 'գրանցվել', 'բաժանորդագրվել', 'contact', 'subscribe',
            'subscription', 'newsletter', 'email', 'էլ. փոստ', 'հասցե', 'address',
            
            # Search and filters
            'փնտրել', 'search', 'filter', 'sort', 'տեսակավորել', 'ֆիլտր',
            
            # Weather and irrelevant content
            'եղանակ', 'weather', 'temperature', 'ջերմություն', 'անձրև', 'rain',
            'արև', 'sun', 'wind', 'քամի', 'snow', 'ձյուն',
            
            # Empty or whitespace-only
            '', ' ', '\n', '\t', '\r',
            
            # Short technical strings
            'js', 'api', 'url', 'http', 'https', 'www', 'com', 'org', 'am',
            
            # Common form elements
            'ուղարկել', 'submit', 'send', 'save', 'պահպանել', 'cancel', 'չեղարկել',
            
            # Site branding for azatutyun
            'azatutyun', 'rfe', 'rl', 'radio free europe', 'radio liberty',
            'ռադիո ազատություն', 'ռադիո ազատ եվրոպա',
            
            # Miscellaneous UI
            'loading', 'բեռնվում', 'please wait', 'սպասել', 'error', 'սխալ',
            'success', 'հաջողություն', 'warning', 'նախազգուշացում',
            
            # Single words that are often navigation
            'հատված', 'բաժին', 'մասնակ', 'սկիզբ', 'վերջ', 'մեջ',
            'part', 'section', 'segment', 'start', 'end', 'middle',
            
            # Copyright and legal
            'terms', 'conditions', 'privacy', 'policy', 'legal', 'disclaimer',
            'պայմաններ', 'գաղտնիություն', 'քաղաքականություն', 'իրավական',
            
            # Video and media controls
            'play', 'pause', 'stop', 'volume', 'mute', 'fullscreen',
            'նվագարկել', 'դադարեցնել', 'կանգնել', 'ձայն', 'լռել',
            
            # Pagination
            'page', 'pages', 'էջ', 'էջեր', 'next page', 'previous page',
            'հաջորդ էջ', 'նախորդ էջ', 'first', 'last', 'առաջին', 'վերջին',
            
            # Azatutyun-specific patterns
            'ազատությունը', 'ազատ եվրոպա', 'ռադիո ազատություն',
            'ազատական', 'ազատ', 'եվրոպա', 'ռադիո', 'liberty',
            'rfe/rl', 'radio free', 'radio liberty'
        ]
        
        cleaned_fragments = []
        for fragment in text_fragments:
            if not fragment:
                continue
            
            # Clean the fragment
            fragment_clean = fragment.strip()
            if not fragment_clean:
                continue
            
            # Convert to lowercase for comparison
            fragment_lower = fragment_clean.lower()
            
            # Skip if it's an unwanted fragment
            if fragment_lower in unwanted_fragments:
                continue
            
            # Skip very short fragments (less than 3 characters)
            if len(fragment_clean) < 3:
                continue
            
            # Skip fragments that are mostly numbers or punctuation
            if re.match(r'^[0-9\s\-\.\,\:\;]+$', fragment_clean):
                continue
            
            # Skip fragments with too many special characters
            special_char_count = len(re.findall(r'[^\w\s]', fragment_clean))
            if special_char_count > len(fragment_clean) * 0.5:
                continue
            
            # Skip fragments that are all uppercase and short (likely headings/navigation)
            if fragment_clean.isupper() and len(fragment_clean) < 20:
                continue
            
            # Skip fragments containing only Armenian letters repeated
            if re.match(r'^[ա-ֆ]{1,2}$', fragment_clean):
                continue
            
            cleaned_fragments.append(fragment_clean)
        
        return cleaned_fragments

    def extract_clean_title(self, response):
        """Extract clean title using hierarchical approach"""
        
        # Remove site name variations
        site_names = ['azatutyun.am', 'azatutyun', 'ազատություն', 'ազատ եվրոպա', 'rfe/rl', 'radio free europe']
        
        title = None
        
        # Try different title selectors in order of preference
        title_selectors = [
            "h1",
            ".wsw h1",
            ".content-header h1",
            ".article-title",
            ".entry-title",
            "meta[property='og:title']",
            "title"
        ]
        
        for selector in title_selectors:
            try:
                if selector.startswith('meta'):
                    title = response.css(f"{selector}::attr(content)").get()
                else:
                    title = response.css(f"{selector}::text").get()
                
                if title:
                    break
            except:
                continue
        
        if not title:
            # Fallback to URL-based title
            title = response.url.split("/")[-1].replace("-", " ").replace("_", " ").title()
        
        # Clean title
        title = title.strip()
        
        # Remove site names from title
        for site_name in site_names:
            if title.lower().endswith(site_name.lower()):
                title = title[:-len(site_name)].strip()
            if title.lower().startswith(site_name.lower()):
                title = title[len(site_name):].strip()
        
        # Remove common separators at the end
        separators = ['-', '|', ':', '–', '—', '»', '«']
        for sep in separators:
            if title.endswith(sep):
                title = title[:-1].strip()
            if title.startswith(sep):
                title = title[1:].strip()
        
        return title if title else "Անանուն հոդված"

    def extract_clean_content(self, response):
        """Extract clean content using comprehensive filtering"""
        
        # Primary content selectors for azatutyun.am
        content_selectors = [
            "div.wsw",
            "div.content", 
            "div.article-content",
            "div.entry-content",
            "article",
            "div.main-content",
            "div.post-content",
            "div[class*='content']",
            "div[id*='content']",
            "main",
            "section.content"
        ]
        
        content_elements = []
        
        for selector in content_selectors:
            try:
                elements = response.css(f"{selector} ::text").getall()
                if elements:
                    content_elements = elements
                    break
            except:
                continue
        
        if not content_elements:
            # Fallback to all paragraphs
            content_elements = response.css("p::text").getall()
        
        # Clean the fragments
        cleaned_fragments = self.clean_text_fragments(content_elements)
        
        # Join cleaned fragments
        content = "\n".join(cleaned_fragments)
        
        # Additional content validation
        if len(content) < 50:  # Too short
            return ""
        
        if len(content.split()) < 10:  # Too few words
            return ""
        
        # Remove excessive newlines
        content = re.sub(r'\n{3,}', '\n\n', content)
        
        return content.strip()

    def validate_article_content(self, title, content):
        """Validate that the article content meets minimum requirements"""
        
        if not title or not content:
            return False
        
        # Title validation
        if len(title) < 5:
            return False
        
        # Content validation
        if len(content) < 50:
            return False
        
        word_count = len(content.split())
        if word_count < 10:
            return False
        
        # Check for reasonable content structure
        if content.count('\n') > len(content) * 0.1:  # Too many line breaks
            return False
        
        return True

    def is_article_processed(self, url, title):
        """Check if article was already processed using Redis cache"""
        if not self.redis_client:
            return False
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_azatutyun:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_azatutyun:{article_hash}"
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
        # Extract articles using azatutyun.am structure
        # Articles are in li elements with media-block divs
        articles = response.css("div.row ul li div.media-block")
        
        # Optimize: limit to only latest 15 articles
        articles = articles[:15]
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված (սահմանափակված 15-ով)")

        for article in articles:
            # Extract link and title using azatutyun.am structure
            link = article.css("a::attr(href)").get()
            title = article.css("h4.media-block__title::text").get()
            
            if link and title:
                # Handle relative URLs
                if link.startswith('/'):
                    full_url = f"https://www.azatutyun.am{link}"
                else:
                    full_url = response.urljoin(link)
                
                # Check Redis cache first
                if self.is_article_processed(full_url, title):
                    self.cached_skips += 1
                    continue
                    
                yield scrapy.Request(full_url, callback=self.parse_article)

    def parse_article(self, response):
        self.processed_articles += 1

        # Extract clean title
        title = self.extract_clean_title(response)
        
        # Extract clean content  
        content = self.extract_clean_content(response)
        
        # Validate content
        if not self.validate_article_content(title, content):
            self.logger.warning(f"❌ Անվավեր բովանդակություն: {title[:50]}...")
            return

        # Extract scraped time - try multiple selectors
        scraped_time = (response.css('time::attr(datetime)').get() or
                       response.css('time::text').get() or 
                       response.css('.date::text').get() or 
                       response.css('.publish-date::text').get() or
                       response.css('.post-date::text').get() or
                       response.css('.entry-date::text').get() or
                       response.css('.media-block__date::text').get() or
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
📊 ԱՄՓՈՓՈՒՄ AZATUTYUN.AM (միայն մաքուր բովանդակություն):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Կրկնություններ: {self.duplicate_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip()) 