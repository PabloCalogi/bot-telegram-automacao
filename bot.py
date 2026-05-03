import asyncio
import requests
from telegram import Bot

# Suas credenciais (Mantendo o que estava no seu print)
TOKEN = '8668731738:AAEBeGtu9xz4mW-ln1LOCyG5MUIY4N2UCdA'
ID_DESTINO = '@PCO_fertas'
URL_PRODUTO = 'https://www.mercadolivre.com.br/relogio-orient-submariner-39-yn6ss023-d1sx-prateado-azul/p/MLB66353464#polycard_client=recommendations_home_navigation-recommendations&reco_backend=machinalis-homes-univb-equivalent-offer&wid=MLB4516738345&reco_client=home_navigation-recommendations&reco_item_pos=5&reco_backend_type=function&reco_id=c638ea15-a23a-4d01-8df8-9a5286d0180f&sid=recos&c_id=/home/navigation-recommendations/element&c_uid=a89800d3-6b89-42e8-8ac1-f3d723de2fc2'


def extrair_id_produto(url):
    """Extrai o ID do produto a partir da URL do Mercado Livre."""
    try:
        # URL format: .../p/MLB66353464#...
        item_id = url.split('/p/')[1].split('#')[0].split('?')[0]
        return item_id if item_id else None
    except (IndexError, AttributeError):
        return None


def extrair_titulo_da_url(url):
    slug = url.split('/p/')[0].rstrip('/').split('/')[-1]
    if not slug:
        return 'Produto Mercado Livre'
    titulo = ' '.join(word.capitalize() for word in slug.replace('-', ' ').split())
    return titulo.replace('Relogio', 'Relógio')


async def verificar_preco():
    bot = Bot(token=TOKEN)
    
    print('Iniciando monitoramento... (Aperte Ctrl+C para parar)')
    ultimo_preco = None
    
    # Extrair ID do produto da URL
    item_id = extrair_id_produto(URL_PRODUTO)
    if not item_id:
        print(f'Erro: Não foi possível extrair o ID do produto da URL.')
        return
    
    print(f'ID do Produto: {item_id}')
    
    # URL da API do Mercado Livre
    api_url = f'https://api.mercadolibre.com/items/{item_id}'
    
    # Headers para evitar bloqueios
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json',
        'Accept-Language': 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7',
        'Connection': 'keep-alive'
    }
    
    while True:
        try:
            session = requests.Session()
            
            # Fazer requisição à API do Mercado Livre com headers
            resposta = session.get(api_url, headers=headers, timeout=10)
            
            # Verificar se o status é 200
            if resposta.status_code == 200:
                dados = resposta.json()
                print(f"Status da API: {resposta.status_code}")
                
                # Extrair título e preço da resposta JSON
                titulo = dados.get('title', extrair_titulo_da_url(URL_PRODUTO))
                preco_atual = dados.get('price')
                
                if preco_atual is None:
                    print("Preço não encontrado na resposta da API.")
                    await asyncio.sleep(600)
                    continue
                
                print(f"Título: {titulo}")
                print(f"Preço: R$ {preco_atual:.2f}")
                
                # Lógica de monitoramento
                if ultimo_preco is None or preco_atual < ultimo_preco:
                    mensagem = (
                        f'🔥 **OFERTA DETECTADA!** 🔥\n'
                        f'{titulo}\n'
                        f'💰 Por: R$ {preco_atual:.2f}\n'
                        f'🔗 Link: {URL_PRODUTO}'
                    )
                    await bot.send_message(chat_id=ID_DESTINO, text=mensagem, parse_mode='Markdown')
                    ultimo_preco = preco_atual
                    print(f"Sucesso: Notificação enviada para {titulo}")
                else:
                    print(f"Aguardando queda... Atual: R$ {preco_atual:.2f}")
                
                await asyncio.sleep(600)  # Espera 10 minutos para a próxima verificação
            else:
                print(f"Erro: Status {resposta.status_code} - {resposta.text[:200]}")
                await asyncio.sleep(60)

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 403:
                print(f'Acesso negado à API (403). Tentando novamente em 60 segundos...')
                await asyncio.sleep(60)
            else:
                print(f'Erro HTTP: {e}')
                await asyncio.sleep(30)
        except requests.exceptions.RequestException as e:
            print(f'Erro na requisição: {e}')
            await asyncio.sleep(30)  # Aguarda 30 segundos antes de tentar novamente
        except (ValueError, KeyError) as e:
            print(f'Erro ao processar resposta da API: {e}')
            await asyncio.sleep(30)
        except Exception as e:
            print(f'Erro inesperado: {e}')
            import traceback
            traceback.print_exc()
            await asyncio.sleep(30)


if __name__ == '__main__':
    try:
        asyncio.run(verificar_preco())
    except KeyboardInterrupt:
        print('\nMonitoramento encerrado pelo usuário.')
    except Exception as e:
        print(f'Erro fatal: {e}')
        import traceback
        traceback.print_exc()