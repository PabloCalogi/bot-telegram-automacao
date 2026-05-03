import requests
from bs4 import BeautifulSoup

url = 'https://www.mercadolivre.com.br/relogio-orient-submariner-39-yn6ss023-d1sx-prateado-azul/p/MLB66353464'
headers = {'User-Agent': 'Mozilla/5.0'}
res = requests.get(url, headers=headers, timeout=20)
print('status', res.status_code)
soup = BeautifulSoup(res.text, 'html.parser')
selectors = [
    ('h1.ui-pdp-title', 'h1 class ui-pdp-title'),
    ('meta[property="og:title"]', 'og:title'),
    ('h1', 'all h1'),
    ('title', 'title'),
    ('meta[name="title"]', 'meta title')
]
for sel, name in selectors:
    found = soup.select_one(sel)
    if found:
        if found.name == 'meta':
            print(name, '=>', found.get('content'))
        else:
            print(name, '=>', found.get_text(strip=True))
    else:
        print(name, '=> None')
