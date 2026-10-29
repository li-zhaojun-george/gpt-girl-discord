"""Fetch public source metadata and bounded meme media; no arbitrary file access."""
import ipaddress
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import aiohttp

def public_url(value, domains):
    if not isinstance(value, str) or len(value) > 2000 or re.search(r'[\s<>\\]', value):
        raise ValueError('Invalid URL')
    parts = urlsplit(value)
    host = (parts.hostname or '').lower()
    if (parts.scheme != 'https' or parts.username or parts.password or parts.port not in (None,443)
            or not any(host == d or host.endswith('.'+d) for d in domains)):
        raise ValueError('Source outside configured public domains')
    return value

def canonical_url(value):
    parts = urlsplit(value)
    host, path = parts.hostname.lower(), parts.path.rstrip('/') or '/'
    query = dict(parse_qsl(parts.query))
    if host in {'youtu.be','www.youtu.be'}:
        host, path, query = 'www.youtube.com', '/watch', {'v':path.strip('/')}
    elif host in {'youtube.com','www.youtube.com','m.youtube.com'}:
        host = 'www.youtube.com'
        if path.startswith(('/shorts/','/embed/')):
            query, path = {'v':path.split('/')[2]}, '/watch'
        else:
            query = {'v':query['v']} if 'v' in query else {}
    elif host.endswith('bilibili.com') and '/video/' in path:
        host, query = 'www.bilibili.com', {}
    else:
        query = {k:v for k,v in query.items() if not k.lower().startswith('utm_') and k.lower() not in {'spm_id_from','share_source','fbclid','gclid','feature'}}
    return urlunsplit(('https',host,path,urlencode(sorted(query.items())),''))

class PublicResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):
        self.base = aiohttp.resolver.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        results = await self.base.resolve(host, port, family)
        if not results or any(not ipaddress.ip_address(r['host']).is_global for r in results):
            raise ValueError('Non-public destination')
        return results

    async def close(self):
        await self.base.close()

class Metadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values, self.title, self.in_title = {}, '', False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            name = attrs.get('property', attrs.get('name','')).lower()
            if name in {'og:image','og:title','og:description','description'}:
                self.values.setdefault(name,attrs.get('content','')[:2000])
        elif tag == 'title':
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title = (self.title+data)[:300]

@dataclass
class Media:
    url: str
    title: str
    description: str
    image: bytes | None = None
    filename: str | None = None

def image_extension(data):
    if data.startswith(b'GIF87a') or data.startswith(b'GIF89a'):
        return 'gif'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'webp'
    raise ValueError('Unsupported image bytes')

class MediaFetcher:
    def __init__(self, sources, images):
        self.sources, self.images = tuple(sources), tuple(images)

    async def _get(self, url, domains, limit, image=False):
        resolver = PublicResolver()
        connector = aiohttp.TCPConnector(resolver=resolver)
        try:
            async with aiohttp.ClientSession(connector=connector, trust_env=False,
                    timeout=aiohttp.ClientTimeout(total=20), headers={'User-Agent':'GPTGirlBot/2.0 (public link preview)'}) as session:
                for _ in range(4):
                    public_url(url, domains)
                    async with session.get(url, allow_redirects=False) as response:
                        if response.status in (301,302,303,307,308):
                            url = urljoin(url,response.headers.get('Location',''))
                            continue
                        response.raise_for_status()
                        kind = response.headers.get('Content-Type','').lower()
                        if image and not kind.startswith('image/'):
                            raise ValueError('Expected image MIME type')
                        if not image and not any(t in kind for t in ('text/html','application/xhtml+xml')):
                            raise ValueError('Expected HTML source page')
                        if image and response.content_length and response.content_length > limit:
                            raise ValueError('Media too large')
                        data = bytearray()
                        async for chunk in response.content.iter_chunked(16384):
                            data.extend(chunk)
                            if len(data) > limit:
                                if image:
                                    raise ValueError('Media too large')
                                del data[limit:]
                                break  # Metadata lives in the HTML head; never read an unbounded page.
                        return url, bytes(data)
                raise ValueError('Too many redirects')
        finally:
            await resolver.close()

    async def fetch(self, url, kind):
        source, data = await self._get(url, self.sources, 1500000)
        parser = Metadata()
        parser.feed(data.decode('utf-8', errors='replace'))
        title = parser.values.get('og:title', parser.title).strip()
        if not title or any(s in title.lower() for s in ('access denied','captcha','just a moment')):
            raise ValueError('No usable source metadata')
        media = Media(canonical_url(source),title[:200],parser.values.get('og:description',parser.values.get('description',''))[:500])
        if kind == 'meme':
            image_url = urljoin(source,parser.values.get('og:image',''))
            _, image = await self._get(image_url,self.images,4*1024*1024,image=True)
            media.image, media.filename = image, 'gpt-girl-share.'+image_extension(image)
        return media
