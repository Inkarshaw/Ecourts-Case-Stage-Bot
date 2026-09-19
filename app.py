import os, re, asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from playwright.async_api import async_playwright

TOKEN=os.environ["TELEGRAM_BOT_TOKEN"]
sessions={}
CASE_RE=re.compile(r"^([A-Za-z. -]+)\s+(\d+)\s+(\d{4})$")

async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send a case as: CC 1001 2025")

async def begin_case(update, case_type, case_no, year):
    chat=update.effective_chat.id
    await update.message.reply_text("Opening eCourts and preparing the case search…")
    pw=await async_playwright().start()
    browser=await pw.chromium.launch(headless=True, args=["--no-sandbox"])
    page=await browser.new_page(viewport={"width":1280,"height":900})
    # Initial prototype: open official eCourts services portal.
    # Selectors are intentionally isolated here so they can be calibrated from live page behavior.
    await page.goto("https://services.ecourts.gov.in/ecourtindia_v6/", wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2500)
    shot=f"/tmp/captcha_{chat}.png"
    await page.screenshot(path=shot, full_page=False)
    sessions[chat]={"pw":pw,"browser":browser,"page":page,"case_type":case_type,"case_no":case_no,"year":year}
    with open(shot,"rb") as f:
        await update.message.reply_photo(f, caption=f"{case_type} {case_no}/{year}\nReply with the CAPTCHA text shown in the screenshot.")

async def handle(update:Update, context:ContextTypes.DEFAULT_TYPE):
    text=(update.message.text or "").strip()
    chat=update.effective_chat.id
    if chat in sessions:
        s=sessions[chat]
        # Live selectors must be calibrated after first Railway test.
        await update.message.reply_text(f"CAPTCHA received: {text}\nBrowser session is still active. Next step is calibrating the live eCourts case-number/CAPTCHA selectors.")
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
