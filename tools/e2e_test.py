"""End-to-end smoke test: publish a screen in one tab, watch it in another.

Usage:
    pip install playwright && playwright install chromium
    python tools/e2e_test.py https://screen.example.com <room> <share_token> <viewer_key>

The room / token come from the bot's "!ekran" private message, the viewer key
from "!izle". Chromium auto-selects the entire screen as the capture source.
"""
import sys

from playwright.sync_api import sync_playwright


def main():
    if len(sys.argv) < 5:
        raise SystemExit(__doc__)
    site, room, token, key = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3], sys.argv[4]
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False, args=[
            "--auto-select-desktop-capture-source=Entire screen",
            "--use-fake-ui-for-media-stream",
        ])
        ctx = b.new_context()
        share = ctx.new_page()
        share.goto(f"{site}/paylas?oda={room}&t={token}")
        share.wait_for_timeout(1500)
        share.click("#basla")
        share.wait_for_timeout(6000)
        print("share page:", share.inner_text("#durumYazi"), share.inner_text("#hata"))

        watch = ctx.new_page()
        watch.goto(f"{site}/izle?oda={room}&k={key}")
        ok = False
        for _ in range(20):
            watch.wait_for_timeout(1000)
            w = watch.evaluate("document.getElementById('v').videoWidth")
            if w:
                h = watch.evaluate("document.getElementById('v').videoHeight")
                print("watch page: video %dx%d, status=%s" % (w, h, watch.inner_text("#durumYazi")))
                ok = True
                break
        if not ok:
            print("watch page: NO VIDEO, status=%s" % watch.inner_text("#durumYazi"))
        watch.wait_for_timeout(3000)
        print("viewer count on share page:", share.inner_text("#izleyici"))
        share.click("#dur")
        watch.wait_for_timeout(7000)
        print("after stop, watch page:", watch.inner_text("#durumYazi"))
        b.close()
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
