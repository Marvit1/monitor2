import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import requests
import redis
from datetime import datetime

    

class ShantnewsSpider(scrapy.Spider):
    name = "shantnews"
    allowed_domains = ["shantnews.am"]
    start_urls = ["https://www.shantnews.am/"]
    
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
        super(ShantnewsSpider, self).__init__(*args, **kwargs)
        
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
        cache_key = f"processed_shantnews:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        # Create unique hash for article
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_shantnews:{article_hash}"
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
        # Extract articles using the shantnews.am structure
        # Look for article links in the news feed structure
        articles = (response.css("a[href*='/news/view/']") or
                   response.css(".newsfeed_title") or
                   response.css(".newsfeed_block a[href*='/news/view/']") or
                   response.css("li.newsfeed_block a"))
        
        # Optimize: limit to only latest 10 articles
        articles = articles[:10]
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված (սահմանափակված 10-ով)")

        for article in articles:
            # Extract link
            link = article.css("::attr(href)").get()
            
            if link:
                full_url = response.urljoin(link)
                
                # Get title from the article structure - specific to shantnews.am
                title_preview = (article.css("::text").get() or
                               article.css("::attr(title)").get() or
                               article.css("::attr(alt)").get() or
                               "Article")
                
                # Check Redis cache first
                if self.is_article_processed(full_url, title_preview):
                    self.cached_skips += 1
                    continue
                    
                yield scrapy.Request(full_url, callback=self.parse_article)

    def clean_text_fragments(self, text_fragments):
        """Clean text fragments by removing unwanted elements - very aggressive filtering"""
        if not text_fragments:
            return []
        
        # Unwanted fragments to filter out - comprehensive list
        unwanted_fragments = [
            # Navigation bullets and arrows
            "•", "›", "→", "←", "↑", "↓", "▪", "▫", "■", "□", "◆", "◇", "○", "●", "⭐", "★", "☆",
            # Social media sharing terms
            "share", "facebook", "twitter", "instagram", "youtube", "telegram", "whatsapp", "viber", "tiktok",
            "like", "comment", "follow", "subscribe", "կիսվել", "հավանել", "տարածել", "հետևել", "ուղարկել",
            # Armenian navigation and UI elements
            "կարդալ ավելին", "ցույց տալ", "բացել", "սեղմել", "մուտք", "ելք", "գնալ", "տեսնել ավելին",
            "անցնել", "շարունակել", "ավելին", "բոլոր", "նույն", "այստեղ", "դիտել", "ներբեռնել",
            # Shantnews specific elements
            "shantnews.am", "shantnews", "shant tv", "շանթ", "shant", "www.shantnews", "shant.am",
            # Copyright and legal
            "© 2024", "© 2023", "© shant", "copyright", "հեղինակային", "իրավունք", "պաշտպանված", "արգելված",
            "բոլոր իրավունքները պաշտպանված են", "all rights reserved", "կազմակերպության հեղինակային",
            # Advertisement and promotion
            "advertisement", "sponsored", "ad", "ads", "banner", "գովազդ", "ծանուցումներ", "առաջարկ", "գնել",
            # Click baits and calls to action
            "click here", "այստեղ սեղմել", "կարդալ ամբողջը", "continue reading", "շարունակել", "more info",
            # Comments and user interactions
            "comments", "reply", "post", "user", "admin", "մեկնաբանություն", "պատասխանել", "գրառում", "օգտատեր",
            # Contact and footer information
            "contact", "phone", "email", "address", "կապ", "հեռախոս", "էլ-փոստ", "հասցե", "մեզ գրել",
            # Technical and system elements
            "loading", "error", "404", "500", "timeout", "բեռնում", "սխալ", "տեխնիկական", "սպասել",
            # Page structure elements
            "footer", "header", "sidebar", "navigation", "nav", "menu", "մենյու", "նավիգացիա", "բաժին",
            "search", "որոնում", "կեղտոտագիր", "ցանկ", "կատեգորիա", "թեմա", "էջ", "հոդված",
            # Date and time patterns that are likely not content
            "2024", "2023", "minutes ago", "hours ago", "days ago", "ժամ առաջ", "րոպե առաջ", "օր առաջ",
            # Empty or meaningless content
            "read more", "see more", "more info", "details", "ավելին կարդալ", "տեսնել ավելին", "մանրամասն",
            # Print and share options
            "print", "save", "bookmark", "տպել", "պահպանել", "գրանցել", "ընտրել", "ներբեռնել",
            # Single characters and short meaningless strings
            "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m", "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z",
            "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", ":", ";", ".", ",", "!", "?", "(", ")", "[", "]", "{", "}", "-", "_", "+", "=",
            # Common website elements
            "home", "about", "privacy", "terms", "policy", "գլխավոր", "մեր մասին", "գաղտնիություն", "կանոններ",
            # Related articles and suggestions
            "related", "similar", "recommended", "նման", "առնչվող", "խորհուրդ", "առաջարկություն",
            # Tags and categories
            "tags", "category", "categories", "թեգեր", "կատեգորիա", "կատեգորիաներ", "պիտակներ"
        ]
        
        # Words that indicate end of article or non-content
        end_of_article_indicators = [
            "source", "աղբյուր", "հղում", "link", "more", "ավելին", "continue", "շարունակել",
            "related articles", "այլ հոդվածներ", "other news", "այլ լուրեր", "see also", "տեսեք նաև"
        ]
        
        cleaned_fragments = []
        for fragment in text_fragments:
            if not fragment or not fragment.strip():
                continue
                
            cleaned_fragment = fragment.strip()
            
            # Skip if fragment is too short
            if len(cleaned_fragment) < 4:
                continue
                
            # Skip if fragment matches unwanted patterns exactly
            if cleaned_fragment.lower() in unwanted_fragments:
                continue
                
            # Skip if fragment contains end-of-article indicators
            if any(indicator in cleaned_fragment.lower() for indicator in end_of_article_indicators):
                continue
                
            # Skip if fragment is mostly punctuation (more than 80% punctuation)
            if len([c for c in cleaned_fragment if c.isalnum()]) < len(cleaned_fragment) * 0.2:
                continue
                
            # Skip if fragment doesn't contain any letters
            if not any(c.isalpha() for c in cleaned_fragment):
                continue
                
            # Skip if fragment is just numbers
            if cleaned_fragment.replace(" ", "").replace(".", "").replace("-", "").replace("/", "").replace(":", "").replace(",", "").isdigit():
                continue
                
            # Skip fragments that are likely navigation or UI elements
            if any(unwanted in cleaned_fragment.lower() for unwanted in ["click", "more", "see", "read", "share", "like", "comment", "follow", "subscribe"]):
                continue
                
            # Skip very short fragments that are likely not meaningful content
            if len(cleaned_fragment.split()) < 3:  # Less than 3 words
                continue
                
            # Skip fragments that are mostly Armenian navigation terms
            armenian_nav_terms = ["կարդալ", "ցույց", "բացել", "տեսնել", "սեղմել", "մուտք", "ելք", "գնալ", "դիտել", "ներբեռնել"]
            if any(nav_term in cleaned_fragment.lower() for nav_term in armenian_nav_terms) and len(cleaned_fragment.split()) < 5:
                continue
                
            # Add the fragment if it passes all filters
            cleaned_fragments.append(cleaned_fragment)
        
        return cleaned_fragments

    def parse_article(self, response):
        self.processed_articles += 1

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
            if title and title.strip() and len(title.strip()) > 5:
                title = title.strip()
                # Remove site name from title if present
                if " - Shantnews.am" in title:
                    title = title.replace(" - Shantnews.am", "")
                if " | Shantnews.am" in title:
                    title = title.replace(" | Shantnews.am", "")
                if " - Shant TV" in title:
                    title = title.replace(" - Shant TV", "")
                if " | Shant TV" in title:
                    title = title.replace(" | Shant TV", "")
                break
        
        # If no proper title found, generate one
        if not title:
            title = f"Article from {response.url.split('/')[-1] or response.url.split('/')[-2]}"

        # COMPLETELY REWRITTEN: Much more specific content extraction
        content_parts = []
        
        # First, try to find the main article container
        main_article_container = None
        article_container_selectors = [
            "div.article-content",
            "div.post-content", 
            "div.entry-content",
            "div.news-content",
            "div.content",
            "article",
            "main",
            ".main-content"
        ]
        
        for selector in article_container_selectors:
            container = response.css(selector).get()
            if container and len(container) > 200:  # Must be substantial content
                main_article_container = response.css(selector)
                break
        
        if main_article_container:
            # Extract ONLY paragraph content from the main article container
            # Use simpler approach without complex :not() selectors
            content_parts = main_article_container.css("p::text").getall()
            
            # If no paragraphs found, try div text but be very selective
            if not content_parts or len(content_parts) < 2:
                content_parts = main_article_container.css("div > *::text").getall()
        
        # Fallback: try more specific selectors if main container approach fails
        if not content_parts or len(content_parts) < 2:
            # Use very specific selectors that target only the main article content
            content_selectors = [
                # Target only the first article content block
                "div.article-content:first-of-type p::text",
                "div.post-content:first-of-type p::text",
                "div.entry-content:first-of-type p::text",
                "div.news-content:first-of-type p::text",
                "div.content:first-of-type p::text",
                "article:first-of-type p::text",
                # Target direct children only
                "div.article-content > p::text",
                "div.post-content > p::text",
                "div.entry-content > p::text",
                "div.news-content > p::text",
                "div.content > p::text",
                "article > p::text",
                # Last resort: any p tags but limit to first 5
                "p::text"
            ]
            
            for selector in content_selectors:
                content_parts = response.css(selector).getall()
                if content_parts and len(content_parts) >= 2:
                    # Limit to first 5 paragraphs to avoid picking up other articles
                    content_parts = content_parts[:5]
                    break
        
        # Enhanced content cleaning - remove any text that appears to be from other articles
        cleaned_content = []
        for i, part in enumerate(content_parts):
            if not part or not part.strip():
                continue
                
            cleaned_part = part.strip()
            
            # Skip if too short
            if len(cleaned_part) < 10:
                continue
                
            # Skip if starts with common news feed indicators
            if any(cleaned_part.startswith(prefix) for prefix in [
                "Քրեակատարողական", "Թուրքիան", "Գերմանիայի", "ՊԵԿ", "Հետևեք",
                "Կայքէջի", "Ռուսաստանի", "Չինաստանի", "Լավրովը", "Վան Ին"
            ]):
                continue
                
            # Skip if contains copyright or social media text
            if any(term in cleaned_part.lower() for term in [
                "հեղինակային", "պաշտպանված", "śант հը", "shantnews.am", "shant tv",
                "հետևեք մեզ", "սոց․ ցանցերում", "կայքէջի նյութերի", "վերարտադրումը",
                "արգելվում է", "համաձայնության", "share", "facebook", "twitter"
            ]):
                continue
                
            # Skip if appears to be from a different article (contains ellipsis indicating truncation)
            if "[…]" in cleaned_part or "..." in cleaned_part:
                continue
                
            # Skip if contains patterns that indicate other news items
            if any(pattern in cleaned_part for pattern in [
                "այս մասին ասել է", "հաղորդում է", "նշել են գերատեսչությունում",
                "ընդգծել է", "հավելել", "նկատելով"
            ]) and i > 0:  # Allow first paragraph to have these patterns
                continue
                
            cleaned_content.append(cleaned_part)
            
            # Stop after collecting enough content from the main article
            if len(cleaned_content) >= 3 and len("\n".join(cleaned_content)) > 300:
                break
        
        content = "\n".join(cleaned_content)
        
        # Additional filtering: remove any remaining unwanted text
        unwanted_patterns = [
            "Քրեակատարողական ծառահությունը տեղադրել է",
            "Թուրքիան ուշադիր հետևում է",
            "Գերմանիայի փոխկանցլեր",
            "ՊԵԿ ռազմավարական նախագծերին",
            "Հետևեք մեզ սոց․ ցանցերում",
            "Կայքէջի նյութերի վերարտադրումը",
            "արգելվում է առանց"
        ]
        
        for pattern in unwanted_patterns:
            if pattern in content:
                content = content.split(pattern)[0].strip()
                break

        # Skip if content is too short or empty - increased minimum length
        if not content or len(content.strip()) < 100:
            self.logger.info(f"❌ Շատ կարճ կամ անարժեք բովանդակություն ({len(content.strip()) if content else 0} նիշ): {title[:50]}...")
            return
            
        # Additional validation - ensure we have meaningful content
        content_words = content.split()
        if len(content_words) < 15:  # Less than 15 words is likely not real article content
            self.logger.info(f"❌ Շատ քիչ բառեր ({len(content_words)} բառ): {title[:50]}...")
            return
            
        # Check if content looks like real article text
        meaningful_words = [word for word in content_words if len(word) > 3 and word.isalpha()]
        if len(meaningful_words) < 8:  # Less than 8 meaningful words
            self.logger.info(f"❌ Շատ քիչ իմաստալի բառեր ({len(meaningful_words)} բառ): {title[:50]}...")
            return

        # Extract scraped time
        scraped_time = (response.css('time::attr(datetime)').get() or
                       response.css('time::text').get() or 
                       response.css('.newsfeed_date::text').get() or
                       response.css('.date::text').get() or 
                       response.css('.publish-date::text').get() or 
                       response.css('.post-date::text').get() or 
                       response.css('.article-date::text').get() or 
                       response.css('.news-date::text').get() or
                       response.css('[class*="date"]::text').get() or
                       datetime.now().isoformat())

        # Clean title for display
        display_title = title[:60] + "..." if title and len(title) > 60 else title or "Անանուն հոդված"
        
        # Check for keywords
        full_text = f"{title or ''} {content}".strip()
        
        if self.article_contains_keyword(full_text):
            self.logger.info(f"✅ Բանալի բառ գտնվեց: {display_title}")
            self.logger.info(f"📄 Բովանդակության երկարություն: {len(content)} նիշ")
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
📊 ԱՄՓՈՓՈՒՄ SHANTNEWS.AM (օպտիմիզացված - միայն 10 հոդված):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Կրկնվող հոդվածներ: {self.duplicate_articles}
   • Ավարտման պատճառ: {reason}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip()) 