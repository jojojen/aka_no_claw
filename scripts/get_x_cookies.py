#!/usr/bin/env python3
"""
Manual X (Twitter) cookies acquisition tool.

This script uses a visible Playwright browser window so you can manually
login to X and the script will capture and save the cookies automatically.

Usage:
    python scripts/get_x_cookies.py [--output data/x_cookies.json]
"""

import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright


async def get_x_cookies_interactive(output_path: str | Path = "data/x_cookies.json") -> None:
    """Interactively login to X and save cookies using a visible browser."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        # Launch browser in NON-headless mode so user can see it
        browser = await p.chromium.launch(
            headless=False,  # 👈 KEY: Show the browser window
            slow_mo=100,  # Slow down actions so you can see what's happening
        )

        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        )

        page = await context.new_page()

        print("=" * 70)
        print("🌐 X (Twitter) 手動登錄助手")
        print("=" * 70)
        print()
        print("📋 説明:")
        print("  1. 瀏覽器視窗即將打開")
        print("  2. 在顯示的視窗中手動登錄 X (https://x.com)")
        print("  3. 登錄完成後，關閉瀏覽器視窗")
        print("  4. Cookies 將自動保存到:", output_path)
        print()
        print("⚠️  注意:")
        print("  • 不要關閉終端視窗（只關閉瀏覽器視窗）")
        print("  • 如果登錄失敗，請重新運行此腳本")
        print("=" * 70)
        print()

        # Navigate to X login
        print("⏳ 打開 X 登錄頁面...")
        await page.goto("https://x.com/login", wait_until="domcontentloaded")

        # Wait for user to close the browser (or press Ctrl+C)
        try:
            print("✅ 瀏覽器已打開！請在視窗中手動登錄...")
            print("   登錄後，關閉瀏覽器視窗以保存 Cookies")
            print()

            # Wait for browser to stay open until closed by user
            while True:
                try:
                    # Check if page is still accessible
                    await page.evaluate("() => true")
                    await asyncio.sleep(1)
                except Exception:
                    # Browser was closed
                    break

        except KeyboardInterrupt:
            print("\n⏸️  用戶中止")

        # Save cookies
        try:
            cookies = await context.cookies()
            print()
            print("=" * 70)
            print("💾 正在保存 Cookies...")
            print(f"   總共 {len(cookies)} 個 Cookies")

            with open(output_path, "w") as f:
                json.dump(cookies, f, indent=2)

            print(f"✅ Cookies 已成功保存到: {output_path}")
            print()
            print("📝 下次啟動時，將使用這些 Cookies 自動登錄")
            print("=" * 70)

        except Exception as e:
            print(f"❌ 保存 Cookies 失敗: {e}")
            sys.exit(1)

        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    output = sys.argv[1] if len(sys.argv) > 1 else "data/x_cookies.json"

    try:
        asyncio.run(get_x_cookies_interactive(output))
    except KeyboardInterrupt:
        print("\n\n⏹️  程序已中止")
        sys.exit(0)
