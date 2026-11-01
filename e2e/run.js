// Real-browser walkthrough (Chromium via Playwright).
// Usage: node e2e/run.js   (starts its own server on a temporary database)
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const PORT = 8765;
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = path.join(__dirname, "screenshots");
fs.mkdirSync(SHOTS, { recursive: true });

let failures = 0;
function check(cond, msg) {
  if (cond) console.log("  ✓ " + msg);
  else { failures++; console.log("  ✗ " + msg); }
}

async function waitForServer() {
  for (let i = 0; i < 100; i++) {
    try { const r = await fetch(BASE + "/healthz"); if (r.ok) return; } catch (e) {}
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error("server did not start");
}

(async () => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "pa-e2e-"));
  const server = spawn("python3", ["run.py"], {
    cwd: ROOT, env: { ...process.env, PA_DATA_DIR: dataDir, PA_PORT: String(PORT), PA_SECRET_KEY: "e2e" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let serverLog = "";
  server.stdout.on("data", (d) => (serverLog += d));
  server.stderr.on("data", (d) => (serverLog += d));
  const browser = await chromium.launch();
  try {
    await waitForServer();
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await ctx.newPage();
    const consoleErrors = [];
    // invalid submissions intentionally return HTTP 422; the browser logs those as "Failed to load resource"
    page.on("console", (m) => { if (m.type() === "error" && !/status of 422/.test(m.text())) consoleErrors.push(m.text()); });
    page.on("pageerror", (e) => consoleErrors.push(String(e)));
    page.on("dialog", (d) => d.accept()); // confirm() before void / delete
    const today = new Date().toISOString().slice(0, 10);
    const monthStart = today.slice(0, 8) + "01";

    console.log("1. First-time setup (Arabic)");
    await page.goto(BASE + "/");
    check(page.url().endsWith("/setup"), "redirected to setup");
    check((await page.getAttribute("html", "dir")) === "rtl", "Arabic page is right-to-left");
    await page.fill("input[name=company_name]", "شراكة المطبخ");
    await page.fill("input[name=full_name]", "محمود خله");
    await page.fill("input[name=username]", "mahmoud");
    await page.fill("input[name=password]", "password123");
    await page.fill("input[name=password2]", "password123");
    await page.click("button.primary");
    check(page.url() === BASE + "/", "logged in on dashboard");
    await page.screenshot({ path: path.join(SHOTS, "01-dashboard-empty-ar.png"), fullPage: true });

    console.log("2. Partners and shares 50% + 16.6667% x 3, typed with Arabic digits");
    await page.goto(BASE + "/partners");
    for (const n of ["عبد الوهاب سهيل مزيد", "محمود عدنان خله", "مؤمن سليم", "أحمد خالد سليم"]) {
      await page.fill("[data-testid=partner-name]", n);
      await page.click("[data-testid=partner-add]");
    }
    check((await page.locator("[data-testid=partners-table] tbody tr").count()) === 4, "4 partners listed");
    await page.fill("[data-testid=share-date]", "2026-01-01");
    const shareInputs = page.locator(".share-in");
    const vals = ["٥٠", "١٦٫٦٦٦٧", "16.6667", "١٦.٦٦٦٧"];
    for (let i = 0; i < 4; i++) await shareInputs.nth(i).fill(vals[i]);
    check((await page.textContent("#share-total")).includes("100%"), "16.6667 x 3 + 50 shows total 100%");
    await shareInputs.nth(3).fill("16.6");
    check((await page.textContent("#share-total")).includes("99.9"), "a wrong total is shown as such");
    await shareInputs.nth(3).fill("16.6667");
    await page.click("[data-testid=share-save]");
    check(await page.locator(".flash.ok").isVisible(), "shares saved");
    const ver = await page.textContent("[data-testid=share-version]");
    check(ver.includes("16.6667%") && ver.includes("50%") && !ver.includes("أعداد صحيحة"), "saved shares shown as plain percentages");

    console.log("3. Accounts, customer, project");
    for (const [url, name] of [["/master/money_account", "الصندوق"], ["/master/money_account", "بنك فلسطين"],
                               ["/master/customer", "Gift of the Givers"], ["/master/project", "مطبخ الوجبات"]]) {
      await page.goto(BASE + url);
      await page.fill("[data-testid=m-name]", name);
      await page.click("[data-testid=m-save]");
    }
    await page.goto(BASE + "/master/money_account");
    check((await page.locator("[data-testid=master-table] tbody tr").count()) === 2, "2 money accounts");

    console.log("4. Opening balances with Arabic digits and live balance check");
    await page.goto(BASE + "/opening");
    await page.fill("[data-testid=f-date]", monthStart);
    await page.locator(".ob-debit").first().fill("٢٠٬٠٠٠");
    await page.fill("[data-testid=asset-name-0]", "بركس المطبخ");
    await page.fill("[data-testid=asset-amount-0]", "180,000");
    const credits = page.locator(".ob-credit");
    await credits.nth(0).fill("200000");
    check((await page.textContent("#ob-diff")).trim() === "0.00", "difference shows 0.00 when balanced");
    await credits.nth(0).fill("199999.99");
    check((await page.textContent("#ob-diff")).trim() === "0.01", "difference shows 0.01 when off by one agora");
    await page.click("[data-testid=save]");
    check(await page.locator("[data-testid=form-error]").isVisible(), "unbalanced opening refused");
    check((await page.textContent("[data-testid=form-error]")).includes("غير متوازنة"), "message explains imbalance");
    await page.locator(".ob-credit").nth(0).fill("100000");
    await page.locator(".ob-credit").nth(1).fill("١٠٠٬٠٠٠");
    await page.click("[data-testid=save]");
    check(await page.locator(".flash.ok").isVisible(), "balanced opening saved");
    await page.screenshot({ path: path.join(SHOTS, "02-opening-ar.png"), fullPage: true });

    console.log("5. Expense: invalid then valid Arabic number, live preview");
    await page.goto(BASE + "/documents/new/EXPENSE");
    await page.selectOption("[data-testid=f-category]", "meat");
    await page.fill("[data-testid=f-amount]", "12,5");
    check((await page.textContent(".preview[data-for=amount]")).includes("صيغة"), "preview flags 12,5 instantly");
    await page.fill("[data-testid=f-amount]", "١٬٢٣٤٫٥");
    check((await page.textContent(".preview[data-for=amount]")).includes("1,234.50"), "preview shows 1,234.50");
    await page.fill("[data-testid=f-amount]", "12,5");
    await page.selectOption("[data-testid=f-money_account_id]", { label: "الصندوق" });
    await page.fill("[data-testid=f-description]", "لحم مجمد");
    await page.click("[data-testid=save]");
    check((await page.textContent("body")).includes("استخدم النقطة"), "server refuses 12,5 with Arabic explanation");
    check((await page.inputValue("[data-testid=f-description]")) === "لحم مجمد", "typed description kept");
    await page.fill("[data-testid=f-amount]", "٨٠٠");
    await page.click("[data-testid=save]");
    check((await page.textContent("[data-testid=doc-amount]")).includes("800.00"), "expense saved as 800.00");
    const expenseUrl = page.url();

    console.log("6. Paid by a partner personally");
    await page.goto(BASE + "/documents/new/EXPENSE");
    check(!(await page.locator(".ps-partner").isVisible()), "partner selector hidden by default");
    await page.check("[data-testid=pay-partner]");
    check(await page.locator(".ps-partner").isVisible(), "partner selector appears");
    await page.selectOption("[data-testid=f-category]", "rent");
    await page.fill("[data-testid=f-amount]", "2000");
    await page.selectOption("[data-testid=f-paid_by_partner_id]", { label: "عبد الوهاب سهيل مزيد" });
    await page.click("[data-testid=save]");
    check((await page.textContent("[data-testid=journal]")).includes("رأس مال الشركاء"), "credited to partner capital");

    console.log("7. Invoice with live total, then receipt, then overdraw attempt");
    await page.goto(BASE + "/documents/new/INVOICE");
    await page.selectOption("[data-testid=f-customer_id]", { index: 1 });
    await page.selectOption("[data-testid=f-project_id]", { index: 1 });
    await page.fill("[data-testid=f-quantity]", "١٠٠٠");
    await page.fill("[data-testid=f-unit_price]", "17");
    check((await page.textContent("[data-testid=invoice-total]")).trim() === "17,000.00", "live total 17,000.00");
    await page.click("[data-testid=save]");
    check((await page.textContent("[data-testid=doc-amount]")).includes("17,000.00"), "invoice saved 17,000.00");
    await page.goto(BASE + "/documents/new/RECEIPT");
    await page.selectOption("[data-testid=f-customer_id]", { index: 1 });
    await page.selectOption("[data-testid=f-money_account_id]", { label: "بنك فلسطين" });
    await page.fill("[data-testid=f-amount]", "17000.01");
    await page.click("[data-testid=save]");
    check((await page.textContent("[data-testid=form-error]")).includes("يتجاوز ما هو مستحق"), "cannot receive more than owed");
    await page.fill("[data-testid=f-amount]", "17000");
    await page.fill("[data-testid=f-fee]", "٨٥٠");
    await page.click("[data-testid=save]");
    check((await page.textContent("[data-testid=journal]")).includes("16,150.00"), "bank gets net 16,150.00");

    console.log("8. Void with reason and confirmation");
    await page.goto(expenseUrl);
    await page.fill("[data-testid=void-reason]", "فاتورة مكررة");
    await page.click("[data-testid=void]");
    check((await page.textContent("h1")).includes("ملغاة"), "status is void");
    check((await page.textContent("[data-testid=history]")).includes("فاتورة مكررة"), "reason in history");
    await page.screenshot({ path: path.join(SHOTS, "03-void-ar.png"), fullPage: true });

    console.log("9. Reports balance");
    await page.goto(BASE + "/reports/trial");
    check((await page.textContent("[data-testid=trial-status]")).includes("✓"), "trial balance balanced");
    await page.goto(BASE + "/reports/partners");
    await page.screenshot({ path: path.join(SHOTS, "04-partners-ar.png"), fullPage: true });
    await page.goto(BASE + "/");
    await page.screenshot({ path: path.join(SHOTS, "05-dashboard-ar.png"), fullPage: true });

    console.log("9b. Monthly contracts (rents)");
    await page.goto(BASE + "/contracts");
    check(!(await page.locator(".ck-income").isVisible()), "income fields hidden for an expense contract");
    await page.fill("[data-testid=c-name]", "إيجار الأرض");
    await page.fill("[data-testid=c-amount]", "٢٠٠٠");
    await page.fill("[data-testid=c-start]", monthStart);
    await page.check("[data-testid=c-pay-partner]");
    await page.selectOption("[data-testid=c-partner]", { index: 1 });
    await page.click("[data-testid=c-save]");
    check((await page.textContent(".flash.ok")).includes("1"), "land rent saved and this month recorded");
    await page.check("[data-testid=ck-income]");
    check(await page.locator("[data-testid=c-customer]").isVisible(), "customer field shown for an income contract");
    await page.fill("[data-testid=c-name]", "تأجير البركس");
    await page.fill("[data-testid=c-amount]", "3000");
    await page.fill("[data-testid=c-start]", monthStart);
    await page.selectOption("[data-testid=c-project]", { index: 1 });
    await page.selectOption("[data-testid=c-customer]", { index: 1 });
    await page.click("[data-testid=c-save]");
    check((await page.locator("[data-testid=contracts-table] tbody tr").count()) === 2, "2 contracts listed");
    check((await page.textContent("[data-testid=contracts-table]")).includes("1 / 12"), "progress 1 / 12");
    await page.screenshot({ path: path.join(SHOTS, "10-contracts-ar.png"), fullPage: true });
    await page.goto(BASE + "/reports/trial");
    check((await page.textContent("[data-testid=trial-status]")).includes("✓"), "trial balance still balanced");
    await page.goto(BASE + "/");

    console.log("10. Switch to English");
    await page.click("[data-testid=lang-switch]");
    check((await page.getAttribute("html", "dir")) === "ltr", "English page is left-to-right");
    check((await page.textContent("h1")).includes("Dashboard"), "English heading");
    await page.goto(BASE + "/documents/new/EXPENSE");
    await page.fill("[data-testid=f-amount]", "-5");
    check((await page.textContent(".preview[data-for=amount]")).includes("Negative"), "English live preview error");
    await page.screenshot({ path: path.join(SHOTS, "06-expense-form-en.png"), fullPage: true });
    await page.goto(BASE + "/reports/trial");
    await page.screenshot({ path: path.join(SHOTS, "07-trial-en.png"), fullPage: true });

    console.log("11. Mobile layout");
    const mobile = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true,
                                              storageState: await ctx.storageState() });
    const mp = await mobile.newPage();
    await mp.goto(BASE + "/lang/ar");
    await mp.goto(BASE + "/");
    check(await mp.locator(".menu-btn").isVisible(), "mobile menu button visible");
    const overflow = await mp.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    check(overflow <= 1, "no horizontal page overflow on mobile (" + overflow + "px)");
    await mp.screenshot({ path: path.join(SHOTS, "08-mobile-ar.png"), fullPage: true });
    await mp.goto(BASE + "/documents/new/EXPENSE");
    await mp.screenshot({ path: path.join(SHOTS, "09-mobile-expense-ar.png"), fullPage: true });

    check(consoleErrors.length === 0, "no JavaScript errors (" + consoleErrors.join(" | ") + ")");
  } catch (e) {
    failures++;
    console.log("ERROR", e);
    console.log(serverLog.slice(-3000));
  } finally {
    await browser.close();
    server.kill();
  }
  console.log(failures ? `\n${failures} check(s) FAILED` : "\nALL BROWSER CHECKS PASSED");
  process.exit(failures ? 1 : 0);
})();
