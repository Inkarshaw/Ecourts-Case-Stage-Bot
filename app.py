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
        if not await choose_by_text(["C M M Court","Egmore"]):
            raise RuntimeError("Court Complex C M M Court, Egmore not found")

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
        # The Case Number tab is active now. Find the VISIBLE dropdown whose
        # options contain the requested case type (e.g. "CC - Calendar Case").
        # Do not depend on ancestor/form markup: eCourts changes that wrapper dynamically.
        type_sel=None
        all_selects=page.locator("select:visible")
        for i in range(await all_selects.count()):
            el=all_selects.nth(i)
            try:
                opts=await el.locator("option").all_text_contents()
                match=next((o for o in opts if re.match(r"^\\s*"+re.escape(case_type)+r"\\s*-",o,re.I)),None)
                if match:
                    type_sel=el
                    await el.select_option(label=match)
                    await page.wait_for_timeout(300)
                    break
            except: pass
        if type_sel is None:
            raise RuntimeError(f"Case Type {case_type} not found in visible Case Number form")

        num=await first_visible(page,["input[placeholder='Case Number']:visible","input[name*='case_no' i]:visible","input[id*='case_no' i]:visible","input[name*='caseno' i]:visible","input[id*='caseno' i]:visible"])
        if num: await num.fill(case_no)

        yr=await first_visible(page,["input[placeholder='Year']:visible","input[name*='year' i]:visible","input[id*='year' i]:visible","select[name*='year' i]:visible","select[id*='year' i]:visible"])
        if yr:
            try:
                if await yr.evaluate("(e)=>e.tagName")=="SELECT": await yr.select_option(label=year)
                else: await yr.fill(year)
            except: pass

        # Verify the required case type really remained selected before asking for CAPTCHA.
        selected_case_type=""
        try: selected_case_type=(await type_sel.locator("option:checked").inner_text()).strip()
        except: pass
        if not re.match(r"^"+re.escape(case_type)+r"\\s*-",selected_case_type,re.I):
            raise RuntimeError(f"Case Type selection failed: {selected_case_type or 'Select Case Type'}")

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
    case_type=s["case_type"].strip().upper(); case_no=s["case_no"].strip(); year=s["year"].strip()

    # Re-assert case type immediately before submission.
    type_sel=None
    sels=page.locator("select")
    for i in range(await sels.count()):
        el=sels.nth(i)
        try:
            if not await el.is_visible(): continue
            opts=await el.locator("option").all_text_contents()
            match=next((o for o in opts if re.match(r"^\\s*"+re.escape(case_type)+r"\\s*-",o,re.I)),None)
            if match:
                type_sel=el; await el.select_option(label=match); break
        except: pass

    # Visible text inputs on Case Number form: case number, year, captcha.
    inputs=page.locator("input[type='text'], input:not([type])")
    vis=[]
    for i in range(await inputs.count()):
        try:
            if await inputs.nth(i).is_visible(): vis.append(inputs.nth(i))
        except: pass

    num=await first_visible(page,["input[name*='case_no' i]","input[id*='case_no' i]","input[name*='caseno' i]","input[id*='caseno' i]"])
    yr=await first_visible(page,["input[name*='year' i]","input[id*='year' i]"])
    cap=await first_visible(page,["input[placeholder='Enter Captcha']","input[placeholder*='captcha' i]","input[name*='captcha' i]","input[id*='captcha' i]"])

    # Position fallbacks based on the live Case Number form.
    if not num and len(vis)>=3: num=vis[-3]
    if not yr and len(vis)>=3: yr=vis[-2]
    if not cap and vis: cap=vis[-1]

    if num: await num.fill(case_no)
    if yr: await yr.fill(year)
    if not cap:
        await update.message.reply_text("CAPTCHA field could not be located.")
        return
    await cap.fill(text.strip())

    selected_type=""
    if type_sel:
        try: selected_type=await type_sel.locator("option:checked").inner_text()
        except: pass
    actual_no=await num.input_value() if num else ""
    actual_year=await yr.input_value() if yr else ""

    # Click the actual Go button.
    go=page.get_by_role("button",name=re.compile(r"^Go$",re.I))
    if await go.count(): await go.first.click()
    else:
        if not await click_text(page,["Go"]):
            btn=await first_visible(page,["button[type='submit']","input[type='submit']"])
            if btn: await btn.click()
    await page.wait_for_timeout(3500)

    body=await page.locator("body").inner_text()
    low=body.lower()
    submitted=f"{selected_type or case_type} | {actual_no or case_no}/{actual_year or year}"

    if "invalid captcha" in low or "captcha is invalid" in low or "wrong captcha" in low:
        await update.message.reply_text(f"Invalid CAPTCHA. Submitted: {submitted}. Send the case again for a fresh CAPTCHA.")
        return
    if "no record found" in low or "case not found" in low or "record not found" in low:
        await update.message.reply_text(f"No case record found. Submitted: {submitted}.")
        return

    # Do not mistake the site's global navigation ("CNR Number / Case Status / Court Orders")
    # for an actual case result. A successful Case Number search first shows a result row
    # with a View action; open it before parsing case details.
    view=page.get_by_text(re.compile(r"^View$",re.I))
    if await view.count():
        try:
            for i in range(await view.count()):
                if await view.nth(i).is_visible():
                    await view.nth(i).click()
                    await page.wait_for_timeout(2500)
                    body=await page.locator("body").inner_text()
                    low=body.lower()
                    break
        except: pass

    # Require detail-page labels, not generic menu/help text.  "Case Type" and
    # navigation headings alone are NOT evidence that a case result was opened.
    detail_keys=["registration date","first hearing date","next hearing date","stage of case",
                 "nature of disposal","petitioner and advocate","respondent and advocate",
                 "case history"]
    is_detail=any(k in low for k in detail_keys)

    # If the search form is still visible, it always wins over weak detail matches.
    still_search=("search by case number" in low and "enter captcha" in low and "fields marked with" in low)
    if still_search:
        is_detail=False

    if not is_detail:
        if still_search:
            shot=f"/tmp/after_go_{update.effective_chat.id}.png"
            await page.screenshot(path=shot,full_page=False)
            await update.message.reply_text(f"eCourts stayed on the Case Number search form after Go. Submitted: {submitted}.")
            with open(shot,"rb") as fh:
                await update.message.reply_photo(fh,caption="Browser screen immediately after Go. Send me this screenshot if the form appears filled or shows an error.")
        else:
            # Send a compact live-page excerpt so the next parser calibration uses the real result markup.
            lines=[x.strip() for x in body.splitlines() if x.strip()]
            useful=[]
            for line in lines:
                if line.lower() not in ["cnr number","case status","court orders","cause list"]:
                    useful.append(line)
            await update.message.reply_text("Search submitted, but the actual case-detail page was not opened yet. Live result excerpt:\\n\\n"+"\\n".join(useful[-35:])[:3000])
        return

    lines=[x.strip() for x in body.splitlines() if x.strip()]
    wanted=["CNR Number","Case Type","Filing Number","Filing Date","Registration Number",
            "Registration Date","First Hearing Date","Next Hearing Date","Case Stage",
            "Stage of Case","Court Number and Judge","Nature of Disposal"]
    found=[]
    for label in wanted:
        for i,line in enumerate(lines):
            if label.lower() in line.lower():
                # Include the label plus nearby value lines from the detail table.
                snippet=" | ".join(lines[i:i+3])
                if snippet not in found: found.append(snippet)
                break

    await update.message.reply_text(
        f"Case: {case_type} {case_no}/{year}\\n\\n" +
        ("\\n".join(found[:12]) if found else "Case detail page opened; field parser needs one final calibration.")
    )

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
