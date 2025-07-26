import scrapy
from news_scraper.items import NewsScraperItem
import sys
import os
import hashlib
import redis
from datetime import datetime, timedelta
import re

import requests

class OnetvSpider(scrapy.Spider):
    name = "onetv"
    allowed_domains = ["1lurer.am"]
    start_urls = ["https://www.1lurer.am/hy"]
    
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
        super(OnetvSpider, self).__init__(*args, **kwargs)
        
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
        """Clean text fragments by removing unwanted content including programming code"""
        if not text_fragments:
            return []
        
        # Comprehensive list of unwanted fragments including programming code
        unwanted_fragments = [
            # Navigation and UI elements
            'տպել', 'տպել էջը', 'տպել', 'տպել հոդվածը', 'print', 'share', 'share this article',
            'կիսվել', 'կիսվել սա', 'կիսվել facebook', 'կիսվել twitter', 'կիսվել telegram',
            'facebook', 'twitter', 'telegram', 'whatsapp', 'linkedin', 'instagram',
            
            # Footer and copyright
            'copyright', 'հեղինակային իրավունք', 'բոլոր իրավունքները պաշտպանված են',
            'all rights reserved', '© 1lurer', '1lurer.am', '1lurer',
            'առաջին լուրեր', 'նորություններ', 'հեռուստատեսություն', 'tv',
            
            # Programming code and technical elements
            'javascript', 'js', 'jquery', 'function', 'var ', 'let ', 'const ', 'return',
            'html', 'css', 'bootstrap', 'meta', 'script', 'div', 'span', 'class',
            'getElementById', 'getElementsByClassName', 'querySelector', 'addEventListener',
            'document', 'window', 'console', 'log', 'error', 'warn', 'info',
            'async', 'await', 'promise', 'then', 'catch', 'try', 'catch',
            'if(', 'else', 'for(', 'while(', 'switch(', 'case', 'break', 'continue',
            'array', 'object', 'string', 'number', 'boolean', 'null', 'undefined',
            'true', 'false', 'new ', 'this.', 'prototype', 'extends', 'super',
            'import', 'export', 'from', 'default', 'module', 'require',
            'onclick', 'onload', 'onchange', 'onsubmit', 'onmouseover', 'onmouseout',
            'style=', 'class=', 'id=', 'href=', 'src=', 'alt=', 'title=',
            'px', 'em', 'rem', '%', 'auto', 'none', 'block', 'inline', 'flex',
            'margin', 'padding', 'border', 'background', 'color', 'font', 'text',
            'width', 'height', 'top', 'left', 'right', 'bottom', 'position',
            'absolute', 'relative', 'fixed', 'static', 'z-index', 'opacity',
            'display', 'visibility', 'overflow', 'float', 'clear', 'vertical-align',
            'text-align', 'line-height', 'letter-spacing', 'word-spacing',
            'text-decoration', 'text-transform', 'white-space', 'word-wrap',
            'box-sizing', 'border-radius', 'box-shadow', 'transition', 'transform',
            'animation', 'keyframes', 'linear', 'ease', 'cubic-bezier',
            'rgba', 'rgb', 'hsl', 'hsla', 'hex', '#fff', '#000', '#ccc',
            'important', '!important', 'inherit', 'initial', 'unset',
            'media', 'screen', 'print', 'max-width', 'min-width', 'max-height', 'min-height',
            'hover', 'active', 'focus', 'visited', 'first-child', 'last-child',
            'nth-child', 'before', 'after', 'content', 'counter', 'quotes',
            'list-style', 'table', 'thead', 'tbody', 'tr', 'td', 'th',
            'caption', 'border-collapse', 'border-spacing', 'empty-cells',
            'table-layout', 'vertical-align', 'text-align', 'caption-side',
            
            # HTML tags and attributes
            '<div', '</div>', '<span', '</span>', '<p>', '</p>', '<a', '</a>',
            '<img', '<br>', '<hr>', '<ul>', '</ul>', '<li>', '</li>',
            '<h1>', '</h1>', '<h2>', '</h2>', '<h3>', '</h3>', '<h4>', '</h4>',
            '<h5>', '</h5>', '<h6>', '</h6>', '<strong>', '</strong>',
            '<em>', '</em>', '<b>', '</b>', '<i>', '</i>', '<u>', '</u>',
            '<small>', '</small>', '<big>', '</big>', '<sub>', '</sub>',
            '<sup>', '</sup>', '<del>', '</del>', '<ins>', '</ins>',
            '<code>', '</code>', '<pre>', '</pre>', '<blockquote>', '</blockquote>',
            '<cite>', '</cite>', '<q>', '</q>', '<abbr>', '</abbr>',
            '<acronym>', '</acronym>', '<address>', '</address>',
            '<bdo>', '</bdo>', '<kbd>', '</kbd>', '<samp>', '</samp>',
            '<var>', '</var>', '<dfn>', '</dfn>', '<caption>', '</caption>',
            
            # CSS selectors and properties
            '.class', '#id', 'nth-of-type', 'first-of-type', 'last-of-type',
            'only-child', 'only-of-type', 'empty', 'root', 'target',
            'enabled', 'disabled', 'checked', 'indeterminate', 'valid',
            'invalid', 'required', 'optional', 'read-only', 'read-write',
            'lang', 'dir', 'not', 'matches', 'is', 'where', 'has',
            
            # JavaScript frameworks and libraries
            'react', 'angular', 'vue', 'svelte', 'ember', 'backbone',
            'underscore', 'lodash', 'moment', 'axios', 'fetch', 'ajax',
            'json', 'xml', 'api', 'endpoint', 'url', 'uri', 'http', 'https',
            'get', 'post', 'put', 'delete', 'patch', 'options', 'head',
            'status', 'response', 'request', 'header', 'body', 'params',
            'query', 'path', 'route', 'router', 'middleware', 'controller',
            'model', 'view', 'template', 'component', 'directive', 'service',
            'factory', 'provider', 'injector', 'dependency', 'injection',
            'observable', 'promise', 'callback', 'event', 'listener',
            'emit', 'dispatch', 'subscribe', 'unsubscribe', 'observer',
            'state', 'props', 'context', 'ref', 'key', 'index', 'map',
            'filter', 'reduce', 'find', 'forEach', 'some', 'every',
            'includes', 'indexOf', 'lastIndexOf', 'slice', 'splice',
            'push', 'pop', 'shift', 'unshift', 'sort', 'reverse',
            'concat', 'join', 'split', 'replace', 'match', 'search',
            'test', 'exec', 'toString', 'valueOf', 'hasOwnProperty',
            'isPrototypeOf', 'propertyIsEnumerable', 'constructor',
            'prototype', 'call', 'apply', 'bind', 'arguments', 'callee',
            'caller', 'length', 'name', 'eval', 'parseFloat', 'parseInt',
            'isNaN', 'isFinite', 'encodeURI', 'decodeURI', 'encodeURIComponent',
            'decodeURIComponent', 'escape', 'unescape', 'setTimeout',
            'setInterval', 'clearTimeout', 'clearInterval', 'alert',
            'confirm', 'prompt', 'open', 'close', 'focus', 'blur',
            'scroll', 'scrollTo', 'scrollBy', 'resizeTo', 'resizeBy',
            'moveTo', 'moveBy', 'print', 'history', 'location', 'navigator',
            'screen', 'frames', 'parent', 'top', 'self', 'opener',
            'closed', 'defaultStatus', 'status', 'toolbar', 'menubar',
            'scrollbars', 'locationbar', 'statusbar', 'directories',
            'personalbar', 'innerHeight', 'innerWidth', 'outerHeight',
            'outerWidth', 'pageXOffset', 'pageYOffset', 'screenX',
            'screenY', 'clientInformation', 'clipboardData', 'external',
            
            # Advertisement and promotional
            'գովազդ', 'անվճար', 'զեղչ', 'առաջարկ', 'ակցիա', 'special offer',
            'free', 'discount', 'sale', 'promo', 'promotion', 'advertisement',
            
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
            'api', 'url', 'http', 'https', 'www', 'com', 'org', 'am',
            
            # Common form elements
            'ուղարկել', 'submit', 'send', 'save', 'պահպանել', 'cancel', 'չեղարկել',
            
            # Site branding for 1lurer
            '1lurer', '1lurer.am', 'առաջին լուրեր', 'նորություններ', 'հեռուստատեսություն',
            'tv', 'news', 'channel', 'ալիք', 'խմբագիր', 'լրագրող', 'հեղինակ',
            
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
            
            # OneTV specific patterns
            '1lurer.am', '1lurer', 'առաջին լուրեր', 'հեռուստատեսություն',
            'tv channel', 'ալիք', 'news-feed', 'feed', 'list', 'item',
            
            # Content navigation
            'news-feed__list', 'news-feed__item', 'news-feed__link', 'news-feed__title',
            'news-feed__time', 'news-feed__date'
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
            
            # Skip programming code patterns
            if any(code_pattern in fragment_clean for code_pattern in [
                'function(', 'var ', 'let ', 'const ', 'if(', 'for(', 'while(', 
                'return ', 'this.', 'new ', 'typeof', 'instanceof', 'null', 
                'undefined', 'true', 'false', 'async', 'await', 'promise',
                'document.', 'window.', 'console.', 'getElementById', 'querySelector',
                'addEventListener', 'onclick', 'onload', 'jQuery', '$(',
                
                # CSS patterns
                'color:', 'background:', 'margin:', 'padding:', 'font-', 'text-',
                'border:', 'width:', 'height:', 'display:', 'position:', 'px;',
                'em;', 'rem;', '%;', '!important', 'rgba(', 'rgb(', 'hover:',
                
                # HTML patterns  
                '<div', '</div>', '<span', '</span>', '<p>', '</p>', '<a', '</a>',
                '<img', '<br>', '<hr>', '<ul>', '</ul>', '<li>', '</li>',
                'class=', 'id=', 'href=', 'src=', 'alt=', 'title=',
                
                # Common code symbols
                '{', '}', '[];', '();', '=>', '&&', '||', '==', '!=', '===', '!==',
                '++', '--', '+=', '-=', '*=', '/=', '%=', '<<', '>>', '>>>', '&=',
                '|=', '^=', '~', '?:', 'try{', 'catch(', 'finally{', 'throw new',
                
                # SQL patterns
                'SELECT', 'FROM', 'WHERE', 'INSERT', 'UPDATE', 'DELETE', 'CREATE',
                'ALTER', 'DROP', 'INDEX', 'TABLE', 'DATABASE', 'ORDER BY', 'GROUP BY',
                
                # Other programming languages
                'import ', 'export ', 'from ', 'class ', 'extends ', 'implements ',
                'interface ', 'enum ', 'namespace ', 'module ', 'package ', 'public ',
                'private ', 'protected ', 'static ', 'final ', 'abstract ', 'override ',
                'virtual ', 'sealed ', 'partial ', 'readonly ', 'volatile ', 'extern ',
                'using ', 'namespace ', 'delegate ', 'event ', 'operator ', 'implicit ',
                'explicit ', 'params ', 'ref ', 'out ', 'in ', 'is ', 'as ', 'sizeof ',
                'stackalloc ', 'fixed ', 'lock ', 'unsafe ', 'checked ', 'unchecked ',
                'goto ', 'break ', 'continue ', 'switch ', 'case ', 'default ', 'do ',
                'while ', 'for ', 'foreach ', 'if ', 'else ', 'elseif ', 'endif ',
                'try ', 'catch ', 'finally ', 'throw ', 'throws ', 'assert ', 'with ',
                'yield ', 'async ', 'await ', 'lambda ', 'def ', 'class ', 'self ',
                'super ', 'init ', 'del ', 'repr ', 'str ', 'len ', 'iter ', 'next ',
                'property ', 'staticmethod ', 'classmethod ', 'abstractmethod ',
                'synchronized ', 'transient ', 'volatile ', 'strictfp ', 'native '
            ]):
                continue
            
            # Skip CSS code patterns
            if any(css_pattern in fragment_clean for css_pattern in [
                'color:', 'background:', 'margin:', 'padding:', 'font-', 'text-',
                'border:', 'width:', 'height:', 'display:', 'position:', 'px;',
                'em;', 'rem;', '%;', '!important', 'rgba(', 'rgb(', 'hover:',
                'active:'
            ]):
                continue
            
            # Skip HTML tag patterns
            if any(html_pattern in fragment_clean for html_pattern in [
                '<div', '</div>', '<span', '</span>', '<p>', '</p>', '<a', '</a>',
                '<img', '<br>', '<hr>', '<ul>', '</ul>', '<li>', '</li>',
                'class="', 'id="'
            ]):
                continue
            
            # Skip JavaScript keywords and patterns
            if any(js_pattern in fragment_clean for js_pattern in [
                'true', 'false', 'null', 'undefined', 'async', 'await', 'promise',
                'then', 'catch', 'try', 'switch', 'case', 'break', 'continue',
                'typeof', 'instanceof', 'delete', 'void', 'in', 'with', 'debugger',
                'export', 'import', 'from', 'default', 'extends', 'super', 'static'
            ]):
                continue
            
            cleaned_fragments.append(fragment_clean)
        
        return cleaned_fragments

    def extract_clean_title(self, response):
        """Extract clean title using hierarchical approach"""
        
        # Remove site name variations
        site_names = ['1lurer.am', '1lurer', 'առաջին լուրեր', 'հեռուստատեսություն', 'tv', 'news']
        
        # Try multiple title selectors for 1lurer.am
        title = (response.css("h1::text").get() or
                response.css(".entry-title::text").get() or
                response.css(".post-title::text").get() or
                response.css(".article-title::text").get() or
                response.css(".title::text").get() or
                response.css(".news-title::text").get() or
                response.css("meta[property='og:title']::attr(content)").get() or
                response.css("title::text").get())
        
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
        """Extract clean content using comprehensive filtering - avoiding programming code"""
        
        # More targeted selectors that avoid script and style tags
        content_selectors = [
            # Target only paragraphs within main content areas
            "article p::text",
            "div.entry-content p::text",
            ".post-content p::text",
            ".article-content p::text",
            ".content p::text",
            ".main-content p::text",
            ".news-content p::text",
            ".news-body p::text",
            ".article-body p::text",
            ".post-body p::text",
            ".text-content p::text",
            
            # Fallback to div text but more specific
            "article div.text::text",
            "div.entry-content div.text::text",
            ".post-content div.text::text",
            ".article-content div.text::text",
            
            # Last resort - all paragraphs
            "p::text"
        ]
        
        content_parts = []
        
        for selector in content_selectors:
            parts = response.css(selector).getall()
            if parts and len(parts) >= 2:  # Need at least 2 parts for meaningful content
                content_parts = parts
                break
        
        # If still no content, try more general selectors but with strict filtering
        if not content_parts:
            # Get all text but exclude script, style, and navigation areas
            excluded_areas = [
                "script", "style", "nav", "header", "footer", 
                ".menu", ".navigation", ".sidebar", ".widget", 
                ".ad", ".advertisement", ".social", ".share"
            ]
            
            all_text = response.css("body ::text").getall()
            content_parts = [text for text in all_text if text.strip()]
        
        # Clean the content parts with enhanced filtering
        cleaned_fragments = self.clean_text_fragments(content_parts)
        
        # Additional filtering for programming code
        programming_filtered = []
        for fragment in cleaned_fragments:
            # Skip fragments that look like programming code
            if self.is_programming_code(fragment):
                continue
            programming_filtered.append(fragment)
        
        # Join cleaned fragments
        content = "\n".join(programming_filtered)
        
        # Additional content validation
        if len(content) < 50:  # Too short
            return ""
        
        if len(content.split()) < 10:  # Too few words
            return ""
        
        # Remove excessive newlines
        content = re.sub(r'\n{3,}', '\n\n', content)
        
        return content.strip()
    
    def is_programming_code(self, text):
        """Check if text fragment looks like programming code"""
        if not text or len(text) < 10:
            return False
        
        # Programming code indicators
        code_indicators = [
            # JavaScript patterns
            'function', 'var ', 'let ', 'const ', 'if(', 'for(', 'while(', 
            'return ', 'this.', 'new ', 'typeof', 'instanceof', 'null', 
            'undefined', 'true', 'false', 'async', 'await', 'promise',
            'document.', 'window.', 'console.', 'getElementById', 'querySelector',
            'addEventListener', 'onclick', 'onload', 'jQuery', '$(',
            
            # CSS patterns
            'color:', 'background:', 'margin:', 'padding:', 'font-', 'text-',
            'border:', 'width:', 'height:', 'display:', 'position:', 'px;',
            'em;', 'rem;', '%;', '!important', 'rgba(', 'rgb(', 'hover:',
            
            # HTML patterns  
            '<div', '</div>', '<span', '</span>', '<p>', '</p>', '<a', '</a>',
            '<img', '<br>', '<hr>', '<ul>', '</ul>', '<li>', '</li>',
            'class=', 'id=', 'href=', 'src=', 'alt=', 'title=',
            
            # Common code symbols
            '{', '}', '[];', '();', '=>', '&&', '||', '==', '!=', '===', '!==',
            '++', '--', '+=', '-=', '*=', '/=', '%=', '<<', '>>', '>>>', '&=',
            '|=', '^=', '~', '?:', 'try{', 'catch(', 'finally{', 'throw new',
            
            # SQL patterns
            'SELECT', 'FROM', 'WHERE', 'INSERT', 'UPDATE', 'DELETE', 'CREATE',
            'ALTER', 'DROP', 'INDEX', 'TABLE', 'DATABASE', 'ORDER BY', 'GROUP BY',
            
            # Other programming languages
            'import ', 'export ', 'from ', 'class ', 'extends ', 'implements ',
            'interface ', 'enum ', 'namespace ', 'module ', 'package ', 'public ',
            'private ', 'protected ', 'static ', 'final ', 'abstract ', 'override ',
            'virtual ', 'sealed ', 'partial ', 'readonly ', 'volatile ', 'extern ',
            'using ', 'namespace ', 'delegate ', 'event ', 'operator ', 'implicit ',
            'explicit ', 'params ', 'ref ', 'out ', 'in ', 'is ', 'as ', 'sizeof ',
            'stackalloc ', 'fixed ', 'lock ', 'unsafe ', 'checked ', 'unchecked ',
            'goto ', 'break ', 'continue ', 'switch ', 'case ', 'default ', 'do ',
            'while ', 'for ', 'foreach ', 'if ', 'else ', 'elseif ', 'endif ',
            'try ', 'catch ', 'finally ', 'throw ', 'throws ', 'assert ', 'with ',
            'yield ', 'async ', 'await ', 'lambda ', 'def ', 'class ', 'self ',
            'super ', 'init ', 'del ', 'repr ', 'str ', 'len ', 'iter ', 'next ',
            'property ', 'staticmethod ', 'classmethod ', 'abstractmethod ',
            'synchronized ', 'transient ', 'volatile ', 'strictfp ', 'native '
        ]
        
        # Check for multiple code indicators
        indicator_count = sum(1 for indicator in code_indicators if indicator in text)
        
        # If more than 2 indicators, likely programming code
        if indicator_count >= 2:
            return True
        
        # Check for high ratio of special characters (common in code)
        special_chars = len(re.findall(r'[{}();,=+\-*/<>!&|^~]', text))
        if special_chars > len(text) * 0.3:
            return True
        
        # Check for bracket/parenthesis patterns common in code
        brackets = text.count('{') + text.count('}') + text.count('[') + text.count(']')
        parentheses = text.count('(') + text.count(')')
        if brackets > 3 or parentheses > 5:
            return True
        
        return False

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
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_onetv:{article_hash}"
        return self.redis_client.exists(cache_key)

    def mark_article_processed(self, url, title):
        """Mark article as processed in Redis cache"""
        if not self.redis_client:
            return
        article_hash = hashlib.md5(f"{url}:{title}".encode()).hexdigest()
        cache_key = f"processed_onetv:{article_hash}"
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
        # Extract articles from news-feed list structure
        articles = response.css("ul.news-feed__list li.news-feed__item")
        
        # Optimize: limit to latest 15 articles
        articles = articles[:15]
        
        self.logger.info(f"📰 Գտնվել է {len(articles)} հոդված (սահմանափակված 15-ով)")

        for article in articles:
            link = article.css("a.news-feed__link::attr(href)").get()
            title = article.css("span.news-feed__title::text").get()
            time_text = article.css("span.news-feed__time::text").get()
            date_text = article.css("span.news-feed__date::text").get()
            
            # Combine time and date for better timestamp
            timestamp = f"{date_text} {time_text}" if date_text and time_text else ""
            
            if link and title:
                full_url = response.urljoin(link)
                
                # Check Redis cache first
                if self.is_article_processed(full_url, title):
                    self.cached_skips += 1
                    continue
                
                # Include timestamp in meta for additional context
                yield scrapy.Request(
                    full_url, 
                    callback=self.parse_article,
                    meta={'timestamp': timestamp, 'preview_title': title}
                )

    def parse_article(self, response):
        self.processed_articles += 1
        timestamp = response.meta.get('timestamp', '')
        preview_title = response.meta.get('preview_title', '')

        # Extract clean title using new method
        title = self.extract_clean_title(response)
        
        # Extract clean content using new method
        content = self.extract_clean_content(response)
        
        # Add timestamp information to content for keyword matching
        if timestamp:
            content = f"Ժամանակ: {timestamp}\n\n{content}"

        # Validate content
        if not self.validate_article_content(title, content):
            self.logger.warning(f"❌ Անվավեր բովանդակություն: {title[:50]}...")
            return

        # Extract scraped time - use the timestamp or current time
        scraped_time = (response.css('time::attr(datetime)').get() or
                       response.css('time::text').get() or 
                       response.css('.date::text').get() or 
                       response.css('.publish-date::text').get() or
                       response.css('.post-date::text').get() or
                       response.css('.entry-date::text').get() or
                       timestamp or
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
📊 ԱՄՓՈՓՈՒՄ 1LURER.AM (օպտիմիզացված - միայն մաքուր բովանդակություն):
   • Ստուգված հոդվածներ: {self.processed_articles}
   • Նոր հոդվածներ: {self.new_articles}
   • Կրկնություններ: {self.duplicate_articles}
   • Cache-ից բաց թողնված: {self.cached_skips}
   • Սկրիպտի աշխատանքը: ✅ Ավարտված
        """.strip()) 