"""Screenshot the landing page from multiple viewports + scroll positions
so we can visually verify it renders correctly before pushing to GitHub."""
import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright

# Repo-relative by default; override with AOPS_OUT_DIR.
REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = os.environ.get("AOPS_OUT_DIR", str(REPO_ROOT / "var" / "screenshots"))
os.makedirs(OUT_DIR, exist_ok=True)

async def shoot():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as e:
            print(f"chromium launch failed: {e}")
            return False

        # Desktop view — full page
        page = await browser.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=2)
        await page.goto("http://localhost:8888/", wait_until="networkidle", timeout=20000)
        await page.screenshot(path=f"{OUT_DIR}/site-fullpage.png", full_page=True)
        print("✓ full page screenshot")

        # Hero viewport
        await page.set_viewport_size({"width": 1280, "height": 800})
        await page.screenshot(path=f"{OUT_DIR}/site-hero.png")
        print("✓ hero screenshot")

        # Scroll to demo section
        await page.evaluate("document.querySelector('#demo').scrollIntoView({behavior:'instant'})")
        await page.wait_for_timeout(500)
        await page.screenshot(path=f"{OUT_DIR}/site-demo.png")
        print("✓ demo section screenshot")

        # Architecture
        await page.evaluate("document.querySelector('#architecture').scrollIntoView({behavior:'instant'})")
        await page.wait_for_timeout(500)
        await page.screenshot(path=f"{OUT_DIR}/site-arch.png")
        print("✓ architecture section")

        # Components
        await page.evaluate("document.querySelector('#components').scrollIntoView({behavior:'instant'})")
        await page.wait_for_timeout(500)
        await page.screenshot(path=f"{OUT_DIR}/site-components.png")
        print("✓ components section")

        # UAT
        await page.evaluate("document.querySelector('#uat').scrollIntoView({behavior:'instant'})")
        await page.wait_for_timeout(500)
        await page.screenshot(path=f"{OUT_DIR}/site-uat.png")
        print("✓ UAT section")

        # Mobile view
        mobile = await browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        await mobile.goto("http://localhost:8888/", wait_until="networkidle", timeout=20000)
        await mobile.screenshot(path=f"{OUT_DIR}/site-mobile.png")
        print("✓ mobile screenshot")

        await browser.close()
        return True

asyncio.run(shoot())
