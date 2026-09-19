import os, re, asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from playwright.async_api import async_playwright

TOKEN=os.environ["TELEGRAM_BOT_TOKEN"]
sessions={}
CASE_RE=re.compile(r"^([A-Za-z. -]+)\s+(\d+)\s+(\d{4})$")

async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send a case as: CC 1001 2025")

async def first_visible(page, selectors):
    for sel in selectors:
        loc=page.locator(sel)
        if await loc.count():
            try:
                if await loc.first.is_visible(): return loc.first
            except: pass
    return None

async def click_text(page, names):
    for name in names:
        try:
            loc=page.get_by_text(name, exact=False)
            if await loc.count():
                await loc.first.click(timeout=4000); return True
        except: pass
    return False

async def begin_case(update, case_type, case_no, year):
    chat=update.effective_chat.id
    await update.message.reply_text("Opening eCourts → Case Status → Tamil Nadu → Chennai → Court Complex → Case Number…")
    pw=await async_playwright().start()
    browser=await pw.chromium.launch(headless=True,args=["--no-sandbox","--disable-dev-shm-usage"])
    page=await browser.new_page(viewport={"width":1280,"height":900})
    try:
        await page.goto("https://services.ecourts.gov.in/ecourtindia_v6/",wait_until="domcontentloaded",timeout=60000)
        await page.wait_for_timeout(2000)
        await click_text(page,["Case Status"])
        await page.wait_for_timeout(1200)

        # eCourts requires State -> District -> Court Complex before Case Number.
        selects=page.locator("select")
        async def choose_by_text(words):
            for i in range(await selects.count()):
                el=selects.nth(i)
                try:
                    if not await el.is_visible(): continue
                    options=await el.locator("option").all_text_contents()
                    for opt in options:
                        if all(w.lower() in opt.lower() for w in words):
                            await el.select_option(label=opt)
                            await page.wait_for_timeout(1500)
                            return True
                except: pass
            return False

        if not await choose_by_text(["Tamil","Nadu"]):
            raise RuntimeError("State Tamil Nadu not found")
        if not await choose_by_text(["Chennai"]):
            raise RuntimeError("District Chennai not found")
        if not await choose_by_text(["Singaravelar","Maaligai"]):
            raise RuntimeError("Court Complex Singaravelar Maaligai not found")

        await page.wait_for_timeout(1200)
        # Click the Case Number TAB specifically (not a generic text occurrence).
        tab=page.get_by_role("tab", name=re.compile(r"Case Number", re.I))
        if await tab.count():
            await tab.first.click()
        else:
            candidates=page.get_by_text("Case Number", exact=True)
            if not await candidates.count(): raise RuntimeError("Case Number tab not found")
            await candidates.first.click()
        await page.wait_for_timeout(1200)

        # Fill by labels/placeholders first; fall back to likely input/select ordering.
        type_sel=await first_visible(page,["select[name*='case_type' i]","select[id*='case_type' i]","select[name*='casetype' i]","select[id*='casetype' i]"])
        if type_sel:
            try: await type_sel.select_option(label=case_type)
            except:
                try: await type_sel.select_option(value=case_type)
                except: pass

        num=await first_visible(page,["input[name*='case_no' i]","input[id*='case_no' i]","input[name*='caseno' i]","input[id*='caseno' i]"])
        if num: await num.fill(case_no)

        yr=await first_visible(page,["input[name*='year' i]","input[id*='year' i]","select[name*='year' i]","select[id*='year' i]"])
        if yr:
            try:
                if await yr.evaluate("(e)=>e.tagName")=="SELECT": await yr.select_option(label=year)
                else: await yr.fill(year)
            except: pass

        # Locate CAPTCHA image and send a tight screenshot when possible.
        # eCourts renders the captcha as a visible text/image-like box next to the Captcha label.
        captcha=await first_visible(page,["img[id*='captcha' i]","img[src*='captcha' i]","canvas[id*='captcha' i]",".captcha",".captcha_box","[id*='captcha' i]:not(input)"])
        shot=f"/tmp/captcha_{chat}.png"
        if captcha:
            await captcha.screenshot(path=shot)
        else:
            # Crop the form region around the Enter Captcha input rather than the whole page.
            cap_input=await first_visible(page,["input[placeholder='Enter Captcha']","input[placeholder*='Enter Captcha' i]"])
            if cap_input:
                box=await cap_input.bounding_box()
                await page.screenshot(path=shot,clip={"x":max(0,box["x"]-310),"y":max(0,box["y"]-25),"width":min(620,1280-max(0,box["x"]-310)),"height":90})
            else:
                await page.screenshot(path=shot,full_page=False)

        sessions[chat]={"pw":pw,"browser":browser,"page":page,"case_type":case_type,"case_no":case_no,"year":year}
        with open(shot,"rb") as f:
            await update.message.reply_photo(f,caption=f"{case_type} {case_no}/{year}\nReply with the CAPTCHA text.")
    except Exception as ex:
        await page.screenshot(path=f"/tmp/error_{chat}.png",full_page=False)
        with open(f"/tmp/error_{chat}.png","rb") as f:
            await update.message.reply_photo(f,caption=f"Could not reach the Case Number form. Error: {type(ex).__name__}. I saved this screen for selector calibration.")
        await browser.close(); await pw.stop()

async def submit_captcha(update,text,s):
    page=s["page"]
    cap=await first_visible(page,["input[placeholder='Enter Captcha']","input[placeholder*='Enter Captcha' i]","input[name*='captcha' i]","input[id*='captcha' i]"])
    if not cap:
        await update.message.reply_text("CAPTCHA field was not detected. I need to calibrate this live page.")
        return
    await cap.fill(text)
    clicked=await click_text(page,["Go","Search","Submit"])
    if not clicked:
        btn=await first_visible(page,["button[type='submit']","input[type='submit']"])
        if btn: await btn.click()
    await page.wait_for_timeout(3000)
    body=(await page.locator("body").inner_text())[:7000]
    await update.message.reply_text("eCourts result:\n\n"+body[:3500])

async def handle(update:Update, context:ContextTypes.DEFAULT_TYPE):
    text=(update.message.text or "").strip()
    chat=update.effective_chat.id
    if chat in sessions:
        s=sessions[chat]
        try: await submit_captcha(update,text,s)
        except Exception as ex: await update.message.reply_text(f"Submission error: {type(ex).__name__}: {ex}")
        finally:
            await s["browser"].close(); await s["pw"].stop(); sessions.pop(chat,None)
        return
    m=CASE_RE.match(text)
    if not m:
        await update.message.reply_text("Use: CC 1001 2025")
        return
    await begin_case(update,*m.groups())

async def main():
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle))
    await app.initialize(); await app.start(); await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__=="__main__":
    asyncio.run(main())
