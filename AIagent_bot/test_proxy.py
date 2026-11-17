import httpx
import asyncio

PROXIES = [
    "http://45.131.214.22:8080",
    "socks5://195.62.49.84:10808",
    "socks5://5.255.117.127:1080",
    "http://47.85.161.37:3128",
    "socks5://45.74.31.47:4527",
    "http://134.185.85.16:3128",
    "http://5.200.72.61:3128",
    "socks5://45.74.31.46:9809",
    "socks5://107.175.130.24:1080",
    "socks5://195.245.238.86:1080",
    "socks5://49.13.22.249:10805",
    "socks5://45.74.31.42:6528",
    "socks5://45.74.31.47:16276",
    "socks5://45.74.31.46:15552",
    "socks5://45.74.31.47:5086",
    "socks5://45.74.31.47:5947",
    "http://47.89.184.18:3128",
    "socks5://184.170.248.5:4145",
    "socks5://45.74.31.41:6336",
    "http://142.93.202.130:3128"
]

async def test_proxy(proxy):
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=5.0) as client:
            # Проверяем доступ к API Telegram
            resp = await client.get("https://api.telegram.org")
            if resp.status_code == 200:
                return proxy, True
    except Exception:
        pass
    return proxy, False

async def main():
    print("🔍 Проверяю прокси из списка (по 5 секунд на каждый)...")
    tasks = [test_proxy(p) for p in PROXIES]
    results = await asyncio.gather(*tasks)
    
    working = [r[0] for r in results if r[1]]
    
    if working:
        print(f"\n✅ Найдено рабочих прокси: {len(working)}")
        for w in working:
            print(f" -> {w}")
        print("\nСкопируй ЛЮБОЙ из них выше и вставь в PROXY_URL в своих ботах!")
    else:
        print("\n❌ Все прокси мертвы. Возьми новый список в канале @ps_free_proxy_list.")

if __name__ == "__main__":
    asyncio.run(main())