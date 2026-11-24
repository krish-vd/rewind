"""Regenerate the screenshots: python docs/shoot.py"""
import asyncio, subprocess, sys, tempfile
from pathlib import Path
from rewind.demo import build_demo
from rewind.tui import RewindApp, make_loader, MainScreen

OUT = Path(__file__).parent / "screenshots"
h, p, t = build_demo(Path(tempfile.mkdtemp(prefix="rw-shot-")))
check = subprocess.list2cmdline([sys.executable, "check_discount.py"])

async def go():
    app = RewindApp(make_loader(t, h, False), check=check)
    async with app.run_test(size=(170, 46)) as pilot:
        await pilot.pause(0.9)
        app.save_screenshot(filename="1-boot.svg", path=str(OUT))
        for _ in range(50):
            await pilot.pause(0.1)
            if isinstance(app.screen, MainScreen): break
        s = app.screen
        for k in ["n", "n", "]", "]", "]"]: await pilot.press(k)
        await pilot.pause(0.6)
        app.save_screenshot(filename="2-timeline-diff.svg", path=str(OUT))
        await pilot.press("f"); await pilot.pause(0.8)
        app.save_screenshot(filename="3-file-at-step.svg", path=str(OUT))
        await pilot.press("1"); await pilot.press("b"); await pilot.pause(0.4)
        app.save_screenshot(filename="4-bisect-popup.svg", path=str(OUT))
        await pilot.press("enter")
        for _ in range(200):
            await pilot.pause(0.1)
            if not s.bisecting: break
        await pilot.pause(0.8)
        app.save_screenshot(filename="5-bisect-found.svg", path=str(OUT))

asyncio.run(go())
