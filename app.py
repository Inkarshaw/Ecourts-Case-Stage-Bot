import os, re, asyncio, json
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from playwright.async_api import async_playwright

TOKEN=os.environ["TELEGRAM_BOT_TOKEN"]
SHEET_ID=os.environ.get("GOOGLE_SHEET_ID","")
sessions={}
queues={}

CASE_RE=re.compile(r"^([A-Za-z. -]+)\s+(\d+)\s+(\d{4})$")

async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send a case as: CC 1001 2025")


def get_worksheet():
    info=json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds=Credentials.from_service_account_info(info,scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(SHEET_ID).worksheet("Cases")

def load_pending_cases(limit=50):
    ws=get_worksheet()
    rows=ws.get_all_records()
    result=[]
    for row_no,row in enumerate(rows,start=2):
        status=str(row.get("Bot Status","")).strip().lower()
        if status in ("","pending","retry") and row.get("Case Type") and row.get("Case Number") and row.get("Year"):
            result.append({
                "row":row_no,
                "case_type":str(row["Case Type"]).strip(),
                "case_no":str(row["Case Number"]).strip(),
                "year":str(row["Year"]).strip(),
            })
            if len(result)>=limit:
                break
    return result

def set_sheet_status(row_no,status):
    get_worksheet().update_cell(row_no,22,status)

async def start_next_queue_case(update):
    chat=update.effective_chat.id
    q=queues.get(chat)
    if not q:
        return
    if not q["items"]:
        await update.message.reply_text("✅ Queue finished: %s/%s processed." % (q["done"],q["total"]))
        queues.pop(chat,None)
        return
    item=q["items"][0]
    await asyncio.to_thread(set_sheet_status,item["row"],"CAPTCHA Pending")
    await begin_case(update,item["case_type"],item["case_no"],item["year"])
    if chat in sessions:
        sessions[chat]["queue_mode"]=True
        sessions[chat]["sheet_row"]=item["row"]

async def updatecases(update:Update, context:ContextTypes.DEFAULT_TYPE):
    chat=update.effective_chat.id
    try:
        limit=int(context.args[0]) if context.args else 50
        limit=max(1,min(limit,200))
        cases=await asyncio.to_thread(load_pending_cases,limit)
    except Exception as ex:
        await update.message.reply_text("Google Sheet error: %s: %s" % (type(ex).__name__,str(ex)[:500]))
        return
    if not cases:
        await update.message.reply_text("No pending cases found in the Cases sheet.")
        return
    queues[chat]={"items":cases,"done":0,"total":len(cases)}
    await update.message.reply_text("📋 Queue started: %s pending case(s). I will send one CAPTCHA at a time." % len(cases))
    await start_next_queue_case(update)

async def stop_queue(update:Update, context:ContextTypes.DEFAULT_TYPE):
    chat=update.effective_chat.id
    queues.pop(chat,None)
    s=sessions.pop(chat,None)
    if s:
        try:
            await s["browser"].close()
            await s["pw"].stop()
        except:
            pass
    await update.message.reply_text("Queue stopped.")

async def queue_status(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=queues.get(update.effective_chat.id)
    if not q:
        await update.message.reply_text("No active case queue.")
        return
    item=q["items"][0] if q["items"] else None
    current=("%s %s/%s" % (item["case_type"],item["case_no"],item["year"])) if item else "finishing"
    await update.message.reply_text("Queue: %s/%s completed. Current: %s" % (q["done"],q["total"],current))

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
        # eCourts paints the CAPTCHA asynchronously. The second/reference crop was
        # clearer, so wait a little longer and reproduce that tight challenge-only framing.
        await page.wait_for_timeout(2200)

        box=await cap_input.bounding_box()
        vp=page.viewport_size or {"width":1280,"height":720}

        # Tight crop around the challenge characters only; exclude label and audio/refresh.
        x=max(0,box["x"]-345)
        y=max(0,box["y"]-8)
        width=min(125,vp["width"]-x)
        height=min(48,vp["height"]-y)
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
    # Keep line structure: eCourts detail tables are easier to parse from adjacent lines
    # than from one flattened string.
    lines=[x.strip() for x in body.splitlines() if x.strip()]
    clean=" ".join(lines)

    def grab(pattern):
        m=re.search(pattern,clean,re.I)
        return m.group(1).strip(" :-|") if m else ""

    def after_label(label, stop_labels):
        # Supports both "Label Value" and label/value on adjacent table lines.
        for i,line in enumerate(lines):
            lowline=line.lower()
            ll=label.lower()
            if lowline.startswith(ll):
                rest=line[len(label):].strip(" :-|")
                if rest: return rest
                if i+1<len(lines): return lines[i+1].strip(" :-|")
        stop="|".join(re.escape(x) for x in stop_labels)
        return grab(re.escape(label)+r"\\s*:?\\s*(.+?)(?="+stop+r"|$)") if stop else ""

    filing=grab(r"Filing Number\\s*:?\\s*([A-Za-z0-9./-]+)")
    filing_date=grab(r"Filing Date\\s*:?\\s*([0-9/-]+)")
    reg=grab(r"Registration Number\\s*:?\\s*([A-Za-z0-9./-]+)")
    reg_date=grab(r"Registration Date\\s*:?\\s*([0-9/-]+)")
    cnr=grab(r"CNR Number\\s*:?\\s*([A-Z0-9]{12,})")
    first=after_label("First Hearing Date",["Next Hearing Date","Case Stage"])
    nxt=after_label("Next Hearing Date",["Case Stage","Court Number and Judge"])
    stage=after_label("Case Stage",["Court Number and Judge","Petitioner and Advocate"])
    judge=after_label("Court Number and Judge",["Petitioner and Advocate","Respondent and Advocate"])
    efno=grab(r"e-Filing Number\\s*:?\\s*([A-Za-z0-9./-]+)")
    efdate=grab(r"e-Filing Date\\s*:?\\s*([0-9/-]+)")

    # FIR section labels vary slightly across eCourts establishments.
    fir_no=after_label("FIR Number",["Police Station","Police Station Name","FIR Date","Year"])
    fir_ps=after_label("Police Station",["FIR Number","FIR Date","Year","State"])
    if not fir_ps: fir_ps=after_label("Police Station Name",["FIR Number","FIR Date","Year","State"])
    fir_date=after_label("FIR Date",["Police Station","FIR Number","Year","State"])

    import html
    esc=lambda v: html.escape(str(v or "").strip())
    parts=[f"📄 <b>{esc(case_type)} {esc(case_no)}/{esc(year)}</b>"]
    if cnr: parts.append(f"🔖 <b>CNR:</b> <code>{esc(cnr)}</code>")
    if judge: parts.append(f"⚖️ <b>Court:</b> {esc(judge)}")
    if reg: parts.append(f"📝 <b>Registration:</b> {esc(reg)}" + (f" • {esc(reg_date)}" if reg_date else ""))
    if filing: parts.append(f"📥 <b>Filing:</b> {esc(filing)}" + (f" • {esc(filing_date)}" if filing_date else ""))
    if efno: parts.append(f"💻 <b>e-Filing:</b> {esc(efno)}" + (f" • {esc(efdate)}" if efdate else ""))

    if fir_no or fir_ps or fir_date:
        parts.extend(["","🚔 <b>FIR DETAILS</b>"])
        if fir_no: parts.append(f"• <b>FIR No.:</b> {esc(fir_no)}")
        if fir_ps: parts.append(f"• <b>Police Station:</b> {esc(fir_ps)}")
        if fir_date: parts.append(f"• <b>FIR Date:</b> {esc(fir_date)}")

    parts.extend(["","📚 <b>LATEST CASE STATUS</b>"])
    if stage: parts.append(f"🔹 <b>Stage:</b> {esc(stage)}")
    if nxt: parts.append(f"📅 <b>Next Hearing:</b> {esc(nxt)}")

    # Find the Case History table and click the chronologically latest hearing-date link.
    try:
        date_re=re.compile(r"^(\\d{1,2})[-/](\\d{1,2})[-/](\\d{4})$")
        candidates=[]
        links=page.locator("a:visible,button:visible,[role='button']:visible")
        for i in range(await links.count()):
            el=links.nth(i)
            try:
                txt=(await el.inner_text()).strip()
                m=date_re.match(txt)
                if m:
                    dd,mm,yyyy=map(int,m.groups())
                    candidates.append(((yyyy,mm,dd),el,txt))
            except: pass
        if candidates:
            candidates.sort(key=lambda x:x[0])
            _,latest_link,history_date=candidates[-1]
            await latest_link.click()
            await page.wait_for_timeout(900)

            dlines=[x.strip() for x in (await page.locator("body").inner_text()).splitlines() if x.strip()]
            dclean=" ".join(dlines)
            def dgrab(label,stops):
                stop="|".join(re.escape(x) for x in stops)
                m=re.search(re.escape(label)+r"\\s*:?\\s*(.+?)(?="+stop+r"|$)",dclean,re.I)
                return m.group(1).strip(" :-|") if m else ""

            business=dgrab("Business",["Next Purpose","Next Hearing Date","Purpose of hearing"])
            purpose=dgrab("Next Purpose",["Next Hearing Date","Business"])
            if not purpose: purpose=dgrab("Purpose of hearing",["Next Hearing Date","Business"])
            hist_next=dgrab("Next Hearing Date",["Business","Next Purpose","Purpose of hearing"])

            parts.append(f"🕘 <b>Last Hearing:</b> {esc(history_date)}")
            if business: parts.append(f"📋 <b>Business:</b> {esc(business)}")
            if purpose: parts.append(f"➡️ <b>Next Purpose:</b> {esc(purpose)}")
            if hist_next: parts.append(f"📆 <b>History Next Hearing:</b> {esc(hist_next)}")
    except Exception as ex:
        pass

    await update.message.reply_text("\\n".join(parts),parse_mode="HTML")
    return True

async def handle(update:Update, context:ContextTypes.DEFAULT_TYPE):
    text=(update.message.text or "").strip()
    chat=update.effective_chat.id
    if chat in sessions:
        s=sessions[chat]
        ok=False
        try:
            ok=bool(await submit_captcha(update,text,s))
        except Exception as ex:
            await update.message.reply_text(f"Submission error: {type(ex).__name__}: {ex}")
        finally:
            await s["browser"].close(); await s["pw"].stop(); sessions.pop(chat,None)
        if s.get("queue_mode"):
            q=queues.get(chat)
            if ok and q and q["items"]:
                item=q["items"].pop(0)
                q["done"]+=1
                try: await asyncio.to_thread(set_sheet_status,item["row"],"Done")
                except: pass
                await start_next_queue_case(update)
            elif not ok:
                try: await asyncio.to_thread(set_sheet_status,s["sheet_row"],"Retry")
                except: pass
                await update.message.reply_text("This case remains pending. Send /updatecases to retry it.")
        return
    m=CASE_RE.match(text)
    if not m:
        await update.message.reply_text("Use: CC 1001 2025")
        return
    await begin_case(update,*m.groups())

async def main():
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("updatecases",updatecases))
    app.add_handler(CommandHandler("stop",stop_queue))
    app.add_handler(CommandHandler("status",queue_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle))
    await app.initialize(); await app.start(); await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__=="__main__":
    asyncio.run(main())
