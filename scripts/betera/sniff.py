"""
Betera - extract rendered event data using JS evaluation.
"""
import json, time, re
from pathlib import Path
from playwright.sync_api import sync_playwright

OUTPUT = Path(__file__).resolve().parent / "events_parsed.json"


def sniff():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(locale="ru-RU", timezone_id="Europe/Minsk", viewport={"width": 1280, "height": 720})
        page = ctx.new_page()

        page.goto("https://pm.by/ru/sport/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(20_000)

        # Wait for sport component to render events
        print("[sniff] Waiting for sport component to render...")
        try:
            page.wait_for_selector(".stakeRow, [class*=Stake], [class*=event], [class*=match]", timeout=30000)
        except:
            print("  No match selectors found after 30s")

        # Try to access the sport component's data store
        print("[sniff] Probing JS state...")
        result = page.evaluate("""() => {
            // Try to find event data in various global objects
            let data = {};

            // Check window.__INITIAL_STATE__, __NEXT_DATA__, etc
            if (window.__INITIAL_STATE__) data.initialState = JSON.stringify(window.__INITIAL_STATE__).substring(0, 500);
            if (window.__NEXT_DATA__) data.nextData = JSON.stringify(window.__NEXT_DATA__).substring(0, 500);
            if (window.__NUXT__) data.nuxt = JSON.stringify(window.__NUXT__).substring(0, 500);

            // Check for React fiber/state
            let appRoot = document.getElementById('app') || document.getElementById('root') || document.querySelector('[data-reactroot]');
            if (appRoot) {
                let key = Object.keys(appRoot).find(k => k.startsWith('__react'));
                if (key) data.reactFiber = 'found: ' + key;
            }

            // Try to find sport events in DOM (text content with odds pattern)
            let textNodes = [];
            let walker = document.createTreeWalker(document.body, 4); // TEXT_NODE
            let n;
            while (n = walker.nextNode()) {
                let t = n.textContent.trim();
                if (t && t.match(/\\d+\\.\\d{2}/) && t.length < 100) {
                    textNodes.push(t);
                    if (textNodes.length > 20) break;
                }
            }
            data.textNodes = textNodes;

            // Check how many iframes
            data.iframes = document.querySelectorAll('iframe').length;

            return data;
        }""")

        for k, v in result.items():
            print(f"  {k}: {v}")

        # Get full text with odds if any
        text = page.inner_text("body")
        Path(OUTPUT).parent.joinpath("page_text.txt").write_text(text, encoding="utf-8")

        # Count lines matching odds pattern
        lines = text.split("\n")
        odds_lines = [l.strip() for l in lines if re.search(r'\d+\.\d{2}', l) and len(l.strip()) < 200]
        print(f"\n  Lines with possible odds: {len(odds_lines)}")
        for l in odds_lines[:20]:
            print(f"    {l[:120]}")

        browser.close()


if __name__ == "__main__":
    sniff()
