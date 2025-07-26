import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import requests
import redis
from datetime import datetime

    

class MamulSpider(scrapy.Spider):
    name = "mamul"
    allowed_domains = ["mamul.am"]
    start_urls = [
        "https://mamul.am/am/news",
        "https://mamul.am/"
    ]
    
    # Add duplicate_articles attribute
    duplicate_articles = 0
    
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
        super(MamulSpider, self).__init__(*args, **kwargs)
        
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

    def is_article_processed(self, url):
        """Check if article was already processed using Redis cache"""
        if not self.redis_client:
            return False
        cache_key = f"processed_mamul:{hashlib.md5(url.encode()).hexdigest()}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        cache_key = f"processed_mamul:{hashlib.md5(url.encode()).hexdigest()}"
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
        # Extract article links from mamul.am based on the actual URL structure
        article_links = []
        
        # Based on actual mamul.am structure, news articles have URLs like /en/news/ID, /am/news/ID, /ru/news/ID
        # Try to find all links that match the news article URL pattern
        selectors = [
            # Direct URL pattern matching - this is the most reliable
            "a[href*='/en/news/']",
            "a[href*='/am/news/']", 
            "a[href*='/ru/news/']",
            # Also try to find any links that contain numbers (article IDs)
            "a[href*='/news/'][href*='317']",  # Recent articles have IDs in 317xxx range
            "a[href*='/news/'][href*='316']",
            "a[href*='/news/'][href*='315']",
            # General news links
            "a[href*='/news/']"
        ]
        
        for selector in selectors:
            try:
                links = response.css(selector)
                if links:
                    self.logger.info(f"🔍 Selector '{selector}' գտավ {len(links)} հղում")
                    
                    for link in links:
                        href = link.css("::attr(href)").get()
                        title = (link.css("::text").get() or 
                               link.css("::attr(title)").get() or 
                               link.css("::attr(alt)").get())
                        
                        if href and title:
                            title = title.strip()
                            # Only include links that look like real articles
                            if (len(title) > 15 and  # Articles have substantial titles
                                title not in ['Իրադարձություններ', 'Միջազգային', 'Համացանց', 'Քաղաքականություն', 'Տնտեսություն', 'Հասարակություն'] and
                                title not in ['Events', 'International', 'Internet', 'Politics', 'Economy', 'Society'] and
                                not title.lower().startswith(('բաժին', 'մենյու', 'section', 'menu', 'read', '»', 'more')) and
                                not title.endswith(('...', '→', '»', '›', 'more'))):
                                article_links.append(link)
                    
                    if article_links:
                        break
            except Exception as e:
                self.logger.debug(f"Selector {selector} failed: {e}")
                continue
        
        # If we still can't find articles with specific selectors, try a broader approach
        if not article_links:
            self.logger.info("🔍 Փորձում ենք ավելի լայն որոնում...")
            # Look for any link that has the news URL pattern
            all_links = response.css("a[href]")
            for link in all_links:
                href = link.css("::attr(href)").get()
                if href and '/news/' in href and any(lang in href for lang in ['/en/', '/am/', '/ru/']):
                    title = (link.css("::text").get() or 
                           link.css("::attr(title)").get() or 
                           "Article from " + href.split('/')[-1])
                    if title:
                        title = title.strip()
                        if len(title) > 10:
                            article_links.append(link)
        
        # Remove duplicates by href
        seen_urls = set()
        unique_links = []
        for link in article_links:
            href = link.css("::attr(href)").get()
            if href and href not in seen_urls:
                seen_urls.add(href)
                unique_links.append(link)
        
        # Optimize: limit to only latest 10 articles
        unique_links = unique_links[:10]
        
        self.logger.info(f"📰 Ընդամենը գտնվել է {len(unique_links)} հոդված (սահմանափակված 10-ով)")

        # Debug: Log the URLs and titles we found
        for i, link in enumerate(unique_links):
            href = link.css("::attr(href)").get()
            title = link.css("::text").get() or "No title"
            self.logger.info(f"🔗 [{i+1}] {title[:50]}... -> {href}")

        for link in unique_links:
            href = link.css("::attr(href)").get()
            if href:
                full_url = response.urljoin(href)
                
                # Check Redis cache first
                if self.is_article_processed(full_url):
                    self.cached_skips += 1
                    continue
                    
                yield scrapy.Request(full_url, callback=self.parse_article)

    def parse_article(self, response):
        self.processed_articles += 1

        # Extract title from mamul.am article page structure
        title = None
        title_selectors = [
            "h1.article-title::text",
            "h1.post-title::text", 
            "h1.entry-title::text",
            "h1.news-title::text",
            "h1.main-title::text",
            ".article-header h1::text",
            ".post-header h1::text",
            ".content-header h1::text",
            "article h1::text",
            ".main h1::text",
            ".content h1::text",
            "h1::text",
            "h2.article-title::text",
            "h2.post-title::text",
            "h2::text",
            "meta[property='og:title']::attr(content)",
            "meta[name='title']::attr(content)",
            "title::text"
        ]
        
        for selector in title_selectors:
            title = response.css(selector).get()
            if title and title.strip():
                title = title.strip()
                # Skip if it's a category name or navigation element
                if (title not in ['Իրադարձություններ', 'Միջազգային', 'Համացանց', 'Քաղաքականություն', 'Տնտեսություն', 'Հասարակություն'] and
                    title not in ['Events', 'International', 'Internet', 'Politics', 'Economy', 'Society'] and
                    len(title) > 10 and
                    not title.lower().startswith(('բաժին', 'մենյու', 'section', 'menu'))):
                    break
        
        # If no good title found, create one from URL
        if not title or title in ['Իրադարձություններ', 'Միջազգային', 'Համացանց']:
            title = f"Article {response.url.split('/')[-1]}"

        # Extract content from mamul.am article page structure
        content_parts = []
        
        # Try mamul.am specific content selectors
        content_selectors = [
            "div.article-content p::text",
            "div.post-content p::text", 
            "div.entry-content p::text",
            "div.news-content p::text",
            "div.main-content p::text",
            ".article-body p::text",
            ".post-body p::text",
            ".news-body p::text",
            ".content-area p::text",
            ".article-text p::text",
            ".news-text p::text",
            "article .content p::text",
            "article .body p::text",
            "article p::text",
            ".main .content p::text",
            ".main p::text",
            ".content p::text",
            "p::text"  # Last resort
        ]
        
        for selector in content_selectors:
            content_parts = response.css(selector).getall()
            if content_parts and len(content_parts) > 2:  # Need substantial content
                break
        
        # If still no content, try different approach
        if not content_parts:
            # Try to get any meaningful text content
            all_text = response.css("div::text, p::text, span::text").getall()
            content_parts = [text.strip() for text in all_text if text.strip() and len(text.strip()) > 20]
        
        # Clean and filter content
        cleaned_content = []
        unwanted_fragments = [
            # Navigation and UI elements
            '•', '›', '»', '«', '‹', '→', '←', '↑', '↓',
            # Social media and sharing
            'share', 'facebook', 'twitter', 'whatsapp', 'telegram', 'viber',
            'կիսվել', 'տարածել', 'ուղարկել', 'պահպանել',
            # Navigation terms
            'գլխավոր', 'մենյու', 'բաժին', 'կատեգորիա', 'ետ', 'առաջ',
            'home', 'menu', 'back', 'next', 'previous', 'continue',
            # Footer elements
            'copyright', '©', 'բոլոր իրավունքները', 'all rights reserved',
            'կայքի իրավունքները', 'terms of use', 'privacy policy',
            # Ads and sponsored content
            'advertisement', 'sponsored', 'реклама', 'գովազդ',
            # Common website elements
            'read more', 'ավելին', 'continue reading', 'բացել',
            'search', 'որոնում', 'login', 'մուտք',
            # Subscription and newsletter
            'subscribe', 'բաժանորդագրություն', 'newsletter',
            # Comments and feedback
            'comment', 'մեկնաբանություն', 'feedback',
            # Source attribution
            'source:', 'աղբյուր:', 'منبع:', 'источник:',
            # Armenian navigation terms
            'կարդալ ավելին', 'շարունակել', 'կարդալ',
            'բոլոր նորությունները', 'այլ նորություններ',
            # Time/date fragments that are not content
            'ժամ', 'րոպե', 'օր', 'ամիս',
            # Website specific elements
            'mamul.am', 'mamul',
            # Category names that might appear in content
            'իրադարձություններ', 'միջազգային', 'համացանց',
            'քաղաքականություն', 'տնտեսություն', 'հասարակություն'
        ]
        
        for part in content_parts:
            if part and part.strip():
                text = part.strip()
                # Filter out unwanted content
                if (len(text) > 10 and  # Minimum length
                    not any(unwanted in text.lower() for unwanted in unwanted_fragments) and
                    not text.isdigit() and  # Not just numbers
                    not text.lower().startswith(('բաժին', 'մենյու', 'navigation', 'menu')) and
                    not text.endswith(('...', '→', '»', '›')) and
                    not text.strip() in ['', '\n', '\t', '\r'] and
                    text not in ['Իրադարձություններ', 'Միջազգային', 'Համացանց']):
                    cleaned_content.append(text)
        
        content = "\n".join(cleaned_content[:20])  # Limit to first 20 paragraphs

        # Extract time with mamul.am specific selectors
        scraped_time = (response.css('time::attr(datetime)').get() or
                       response.css('time::text').get() or 
                       response.css('.article-date::text').get() or
                       response.css('.post-date::text').get() or
                       response.css('.publish-date::text').get() or
                       response.css('.date::text').get() or 
                       response.css('.dater::text').get() or
                       response.css('[class*="date"]::text').get() or
                       datetime.now().isoformat())

        # Clean title for display
        display_title = title[:60] + "..." if title and len(title) > 60 else title or "Անանուն հոդված"
        
        # Check for keywords in both title and content
        if self.article_contains_keyword(title) or self.article_contains_keyword(content):
            self.logger.info(f"✅ Բանալի բառ գտնվեց: {display_title}")
            # Mark as processed only after successful keyword match
            self.mark_article_processed(response.url)
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
            self.mark_article_processed(response.url)

    def closed(self, reason):
        """Called when spider finishes"""
        self.logger.info(f"""
📊 ԱՄՓՈՓՈՒՄ MAMUL.AM (օպտիմիզացված - միայն 10 հոդված):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Դուպլիկատներ: {self.duplicate_articles}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip())