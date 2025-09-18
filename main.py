def with_browser(fn):
    def _wrap():
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding",
                    "--no-zygote",
                    "--single-process",
                    "--js-flags=--max-old-space-size=128"
                ]
            )
            context = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/125.0 Safari/537.36"),
                viewport={"width": 1280, "height": 720}
            )

            # Bloqueia imagens, fontes e mídia para economizar memória/banda
            def _route_handler(route):
                rtype = route.request.resource_type
                if rtype in ("image", "media", "font"):
                    return route.abort()
                return route.continue_()
            context.route("**/*", _route_handler)

            if COOKIES_JSON:
                try:
                    import json; context.add_cookies(json.loads(COOKIES_JSON))
                except Exception as e:
                    print("cookies load error:", e)

            page = context.new_page()
            page.set_default_timeout(20000)
            page.goto(BET365_URL, timeout=60000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except:
                pass
            page.wait_for_timeout(6000)  # pequena folga pro grid aparecer
            res = fn(page)
            context.close(); browser.close()
            return res
    return _wrap
