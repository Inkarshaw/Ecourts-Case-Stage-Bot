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
        # Work from the visible Case Number controls. Do not rely on eCourts'
        # select option text being present in the initial DOM: populate/select through
        # the visible control and dispatch the same change/input events a user action does.
        all_selects=page.locator("select:visible")
        type_sel=None
        for i in range(await all_selects.count()):
            el=all_selects.nth(i)
            try:
                first=(await el.locator("option").first.inner_text()).strip().lower()
                if "case type" in first:
                    type_sel=el; break
            except: pass
        if type_sel is None:
            raise RuntimeError("Visible Select Case Type dropdown not found")

        # Inspect options and choose CC/AP/etc. by either text prefix or value.
        opts=type_sel.locator("option")
        chosen=None
        for i in range(await opts.count()):
            o=opts.nth(i)
            txt=(await o.inner_text()).strip()
            val=(await o.get_attribute("value")) or ""
            # case_type is already plain text such as "CC"; match "CC - Calendar Case".
            # Do not double-escape the regex whitespace token.
            if txt.upper().startswith(case_type.upper()+" -") or val.strip().upper()==case_type.upper():
                chosen=val; break
        if chosen is None:
            # Diagnostic includes the actual live option list instead of a generic RuntimeError.
            texts=await opts.all_text_contents()
            raise RuntimeError("Case Type option not found. Live options: "+" | ".join(texts[:12]))

        await type_sel.select_option(value=chosen)
        await type_sel.dispatch_event("change")
        await page.wait_for_timeout(300)

        num=page.locator("input[placeholder='Case Number']:visible").first
        yr=page.locator("input[placeholder='Year']:visible").first
        if not await num.count(): raise RuntimeError("Visible Case Number input not found")
        if not await yr.count(): raise RuntimeError("Visible Year input not found")
        await num.click(); await num.fill(case_no); await num.dispatch_event("input"); await num.dispatch_event("change")
        await yr.click(); await yr.fill(year); await yr.dispatch_event("input"); await yr.dispatch_event("change")
        await page.wait_for_timeout(200)

        selected_case_type=(await type_sel.locator("option:checked").inner_text()).strip()
        actual_no=await num.input_value(); actual_year=await yr.input_value()
        if not selected_case_type.upper().startswith(case_type.upper()+" -"):
            raise RuntimeError(f"Case Type selection failed after change: {selected_case_type}")
        if actual_no != case_no or actual_year != year:
            raise RuntimeError(f"Case fields failed: {actual_no}/{actual_year}")

        # Send a reliable crop of the CAPTCHA row. Do not screenshot the first
        # element whose id/class contains "captcha" because eCourts can expose a 1px
        # decorative/border element that produces a blank line image.
        shot=f"/tmp/captcha_{chat}.png"
        cap_input=page.locator("input[placeholder='Enter Captcha']:visible").first
        if not await cap_input.count():
            cap_input=await first_visible(page,["input[placeholder*='captcha' i]:visible","input[name*='captcha' i]:visible","input[id*='captcha' i]:visible"])
        if not cap_input:
            raise RuntimeError("Visible Enter Captcha input not found")
        # eCourts paints the CAPTCHA asynchronously. Wait until the challenge area
        # has had time to render, then capture a stable region around it.
        await page.wait_for_timeout(1800)

        box=await cap_input.bounding_box()
        vp=page.viewport_size or {"width":1280,"height":720}

        # Capture the complete challenge box with a little padding, but stop before
        # the speaker button. Coordinates are anchored to the stable Enter Captcha input.
        x=max(0,box["x"]-390)
        y=max(0,box["y"]-18)
        width=min(190,vp["width"]-x)
        height=min(70,vp["height"]-y)
        await page.screenshot(path=shot,clip={"x":x,"y":y,"width":width,"height":height})

        sessions[chat]={"pw":pw,"browser":browser,"page":page,"case_type":case_type,"case_no":case_no,"year":year}
        try:
            with open(shot,"rb") as f:
                await update.message.reply_photo(f,caption=f"{case_type} {case_no}/{year}\nReply with the CAPTCHA text.")
        except Exception:
            # If Telegram rejects image dimensions, send it as a document instead.
            with open(shot,"rb") as f:
                await update.message.reply_document(f,caption=f"{case_type} {case_no}/{year}\nReply with the CAPTCHA text.")
    except Exception as ex:
        err=f"/tmp/error_{chat}.png"
        await page.screenshot(path=err,full_page=False)
        try:
            with open(err,"rb") as f:
                await update.message.reply_photo(f,caption=f"Could not reach the Case Number form. {type(ex).__name__}: {str(ex)[:700]}")
        except Exception:
            await update.message.reply_text(f"Could not reach the Case Number form. {type(ex).__name__}: {str(ex)[:700]}")
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
    # Search result is a table row (CC/1001/2025 ... View). Click that row's
    # View control before attempting to parse case history/details.
    clicked_view=False
    try:
        rows=page.locator("tr")
        target=None
        case_key=f"{case_type}/{case_no}/{year}".replace(" ","").lower()
        for i in range(await rows.count()):
            row=rows.nth(i)
            try:
                txt=(await row.inner_text()).replace(" ","").lower()
                if case_key in txt:
                    target=row; break
            except: pass
        scope=target if target is not None else page
        candidates=scope.locator("a,button,input[type='button'],input[type='submit']")
        for i in range(await candidates.count()):
            el=candidates.nth(i)
            try:
                txt=((await el.inner_text()) or "").strip()
            except: txt=""
            try:
                val=((await el.get_attribute("value")) or "").strip()
            except: val=""
            if txt.lower()=="view" or val.lower()=="view":
                await el.click()
                clicked_view=True
                break
        if not clicked_view:
            v=scope.get_by_text("View",exact=True)
            if await v.count():
                await v.first.click(); clicked_view=True
        if clicked_view:
            await page.wait_for_timeout(3000)
            body=await page.locator("body").inner_text()
            low=body.lower()
    except Exception as ex:
        await update.message.reply_text(f"Result found, but View could not be opened: {type(ex).__name__}: {str(ex)[:300]}")

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

    # Build a compact Telegram result from the real case-detail page.
    clean=" ".join(body.split())
    def grab(pattern):
        m=re.search(pattern,clean,re.I)
        return m.group(1).strip() if m else ""

    filing=grab(r"Filing Number\\s+([^|]+?)(?=Filing Date)")
    filing_date=grab(r"Filing Date\\s+([^|]+?)(?=Registration Number)")
    reg=grab(r"Registration Number\\s+([^|]+?)(?=Registration Date)")
    reg_date=grab(r"Registration Date\\s+([^|]+?)(?=CNR Number)")
    cnr=grab(r"CNR Number\\s+([A-Z0-9]+)")
    first=grab(r"First Hearing Date\\s+(.+?)(?=Next Hearing Date)")
    nxt=grab(r"Next Hearing Date\\s+(.+?)(?=Case Stage)")
    stage=grab(r"Case Stage\\s+(.+?)(?=Court Number and Judge)")
    judge=grab(r"Court Number and Judge\\s+(.+?)(?=Petitioner and Advocate)")
    efno=grab(r"e-Filing Number\\s+(.+?)(?=e-Filing Date)")
    efdate=grab(r"e-Filing Date\\s+(.+?)(?=First Hearing Date|Case Status|$)")
    fir_no=grab(r"FIR Number\\s+(.+?)(?=Police Station|FIR Date|State|District|$)")
    fir_ps=grab(r"Police Station\\s+(.+?)(?=FIR Number|FIR Date|State|District|$)")
    fir_date=grab(r"FIR Date\\s+(.+?)(?=Police Station|FIR Number|State|District|$)")

    parts=[f"📄 {case_type} {case_no}/{year}"]
    if cnr: parts.append(f"CNR: {cnr}")
    if first: parts.append(f"First Hearing: {first}")
    if judge: parts.append(f"Court/Judge: {judge}")
    if reg: parts.append(f"Registration: {reg}" + (f" ({reg_date})" if reg_date else ""))
    if filing: parts.append(f"Filing: {filing}" + (f" ({filing_date})" if filing_date else ""))
    if efno: parts.append(f"e-Filing: {efno}" + (f" ({efdate})" if efdate else ""))
    if fir_no or fir_ps or fir_date:
        parts.append("")
        parts.append("🚔 FIR Details")
        if fir_no: parts.append(f"FIR Number: {fir_no}")
        if fir_ps: parts.append(f"Police Station: {fir_ps}")
        if fir_date: parts.append(f"FIR Date: {fir_date}")

    # Case History: click the last hearing-date link/row and extract its Business,
    # Next Purpose and Next Hearing Date. eCourts expands these details on click.
    try:
        # Prefer the Case History section, then use the last date-like clickable element.
        hist=page.get_by_text(re.compile(r"Case History",re.I))
        hist_scope=page
        if await hist.count():
            try:
                hist_scope=hist.last.locator("xpath=ancestor::*[self::div or self::section or self::table][1]")
            except: pass

        clickables=hist_scope.locator("a,button,[role='button']")
        dated=[]
        for i in range(await clickables.count()):
            el=clickables.nth(i)
            try:
                txt=(await el.inner_text()).strip()
                if re.search(r"\\b\\d{1,2}[-/]\\d{1,2}[-/]\\d{4}\\b|\\b\\d{1,2}(?:st|nd|rd|th)?\\s+[A-Za-z]+\\s+\\d{4}\\b",txt,re.I):
                    dated.append(el)
            except: pass
        if dated:
            await dated[-1].click()
            await page.wait_for_timeout(700)
            detail=" ".join((await page.locator("body").inner_text()).split())
            business=grab_from= None
            def histgrab(pattern):
                m=re.search(pattern,detail,re.I)
                return m.group(1).strip() if m else ""
            business=histgrab(r"Business\\s*:?\\s*(.+?)(?=Next Purpose\\s*:|Next Hearing Date\\s*:|$)")
            purpose=histgrab(r"Next Purpose\\s*:?\\s*(.+?)(?=Next Hearing Date\\s*:|$)")
            hist_next=histgrab(r"Next Hearing Date\\s*:?\\s*(.+?)(?=Business\\s*:|Next Purpose\\s*:|$)")
            if business or purpose or hist_next:
                parts.append("")
                parts.append("📚 Latest Case History")
                if stage: parts.append(f"Stage: {stage}")
                if nxt: parts.append(f"Current Next Hearing: {nxt}")
                if business: parts.append(f"Business: {business}")
                if purpose: parts.append(f"Next Purpose: {purpose}")
                if hist_next and hist_next.lower()!=nxt.lower(): parts.append(f"History Next Hearing Date: {hist_next}")
    except Exception:
        if stage or nxt:
            parts.append("")
            parts.append("📚 Case Status")
            if stage: parts.append(f"Stage: {stage}")
            if nxt: parts.append(f"Next Hearing: {nxt}")

    await update.message.reply_text("\\n".join(parts))

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
