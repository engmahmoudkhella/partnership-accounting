(function () {
  "use strict";
  var AR = document.documentElement.lang === "ar";
  // side menu (mobile drawer)
  var menuBtn = document.querySelector(".menu-btn");
  function setNav(open) {
    document.body.classList.toggle("nav-open", open);
    if (menuBtn) menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
  }
  if (menuBtn) menuBtn.addEventListener("click", function () { setNav(!document.body.classList.contains("nav-open")); });
  document.querySelectorAll("[data-close-nav]").forEach(function (el) { el.addEventListener("click", function () { setNav(false); }); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") setNav(false); });
  // highlight the current tab in sub-navigation bars
  document.querySelectorAll(".subnav a").forEach(function (a) {
    if (a.getAttribute("href") === location.pathname) a.setAttribute("aria-current", "page");
  });
  var DIGITS = {"٠":"0","١":"1","٢":"2","٣":"3","٤":"4","٥":"5","٦":"6","٧":"7","٨":"8","٩":"9",
                "۰":"0","۱":"1","۲":"2","۳":"3","۴":"4","۵":"5","۶":"6","۷":"7","۸":"8","۹":"9"};
  // Mirrors app/numbers.py for an instant preview. The server always re-checks.
  function parse(raw, decimals) {
    var s = (raw || "").replace(/[‎‏​-‍⁦-⁩‪-‮﻿؜]/g, "").trim();
    s = s.replace(/[٠-٩۰-۹]/g, function (c) { return DIGITS[c]; });
    s = s.replace(/^(₪|ils|nis|شيكل|شيقل)\s*/i, "").replace(/\s*(₪|ils|nis|شيكل|شيقل|شواكل)$/i, "");
    if (s === "") return {empty: true};
    if (/^[-−(]/.test(s)) return {error: AR ? "لا يُقبل رقم سالب" : "Negative numbers are not allowed"};
    s = s.replace(/[٫]/g, ".").replace(/[٬،'    ]/g, ",");
    var m = /^(\d+)(?:\.(\d+))?$/.exec(s) || /^(\d{1,3}(?:,\d{3})+)(?:\.(\d+))?$/.exec(s);
    if (!m) return {error: AR ? "صيغة الرقم غير صحيحة" : "Invalid number format"};
    var frac = m[2] || "";
    if (frac.length > decimals && /[1-9]/.test(frac.slice(decimals)))
      return {error: (AR ? "الحد الأقصى للكسور: " : "Max decimals: ") + decimals};
    frac = (frac.slice(0, decimals) + "0000").slice(0, decimals);
    var whole = m[1].replace(/,/g, "").replace(/^0+(?=\d)/, "");
    return {value: whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (decimals ? "." + frac : ""), minor: BigInt(whole + frac)};
  }
  function dec(kind) { return kind === "qty" ? 3 : 2; }
  function fmtMinor(v, d) {
    var neg = v < 0n; if (neg) v = -v;
    var s = v.toString().padStart(d + 1, "0");
    var w = s.slice(0, s.length - d), f = s.slice(s.length - d);
    return (neg ? "-" : "") + w.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (d ? "." + f : "");
  }
  function preview(input) {
    var out = input.parentElement.querySelector(".preview");
    if (!out) return;
    var r = parse(input.value, dec(input.dataset.kind));
    out.classList.toggle("bad", !!r.error);
    out.textContent = r.empty ? "" : r.error ? r.error : (AR ? "سيُحفظ: " : "Will save: ") + r.value;
  }
  document.querySelectorAll("input.numin").forEach(function (i) {
    i.addEventListener("input", function () { preview(i); update(); });
    preview(i);
  });
  // who paid: money account or partner
  function paysource() {
    var fs = document.querySelector(".paysource");
    if (!fs) return;
    var v = (fs.querySelector("input[name=pay_source]:checked") || {}).value || "account";
    fs.querySelector(".ps-account").style.display = v === "account" ? "" : "none";
    fs.querySelector(".ps-partner").style.display = v === "partner" ? "" : "none";
    var cr = fs.querySelector(".ps-credit");
    if (cr) cr.style.display = v === "credit" ? "" : "none";
  }
  document.querySelectorAll("input[name=pay_source]").forEach(function (r) { r.addEventListener("change", paysource); });
  // monthly contract form: show the fields of the chosen type
  function ckind() {
    var f = document.querySelector("[data-testid=contract-form]");
    if (!f) return;
    var v = (f.querySelector("input[name=kind]:checked") || {}).value || "expense";
    f.querySelector(".ck-income").style.display = v === "income" ? "" : "none";
    f.querySelector(".ck-expense").style.display = v === "expense" ? "" : "none";
  }
  document.querySelectorAll("input[name=kind]").forEach(function (r) { r.addEventListener("change", ckind); });
  ckind();
  paysource();
  function val(name, d) {
    var el = document.querySelector("[name=" + name + "]");
    if (!el) return null;
    var r = parse(el.value, d);
    return r.minor !== undefined ? r.minor : null;
  }
  function update() {
    var tot = document.getElementById("invoice-total");
    if (tot) {
      var q = val("quantity", 3), p = val("unit_price", 2);
      if (q !== null && p !== null) {
        var n = q * p, r = n % 1000n, t = n / 1000n + (2n * r >= 1000n ? 1n : 0n);
        tot.textContent = fmtMinor(t, 2);
      } else tot.textContent = "—";
    }
    var split = document.getElementById("dist-split");
    if (split) {
      var amt = val("amount", 2), shares = JSON.parse(split.dataset.shares || "[]"),
          names = JSON.parse(split.dataset.names || "{}");
      split.innerHTML = "";
      if (amt !== null) {
        var total = shares.reduce(function (a, s) { return a + BigInt(s[1]); }, 0n), parts = [], left = amt;
        shares.forEach(function (s, i) { var x = amt * BigInt(s[1]); parts.push({i: i, q: x / total, r: x % total}); left -= x / total; });
        parts.slice().sort(function (a, b) { return a.r === b.r ? a.i - b.i : (b.r > a.r ? 1 : -1); })
          .slice(0, Number(left)).forEach(function (p) { p.q += 1n; });
        parts.forEach(function (p) {
          var li = document.createElement("li");
          li.textContent = (names[shares[p.i][0]] || ("#" + shares[p.i][0])) + ": " + fmtMinor(p.q, 2);
          split.appendChild(li);
        });
      }
    }
    var d = document.getElementById("ob-d");
    if (d) {
      var sd = 0n, sc = 0n;
      document.querySelectorAll(".ob-debit").forEach(function (i) { var r = parse(i.value, 2); if (r.minor !== undefined) sd += r.minor; });
      document.querySelectorAll(".ob-credit").forEach(function (i) { var r = parse(i.value, 2); if (r.minor !== undefined) sc += r.minor; });
      d.textContent = fmtMinor(sd, 2);
      document.getElementById("ob-c").textContent = fmtMinor(sc, 2);
      var diff = document.getElementById("ob-diff");
      diff.textContent = fmtMinor(sd - sc, 2);
      diff.className = "num " + (sd === sc ? "ok" : "neg");
    }
    var st = document.getElementById("share-total");
    if (st) {
      // percentages, decimals allowed; the total must be 100%
      var ins = Array.prototype.slice.call(document.querySelectorAll(".share-in"));
      var rs = ins.map(function (i) { return parse(i.value, 4); });
      var bad = rs.some(function (r) { return !!r.error; });
      var ws = rs.map(function (r) { return r.minor !== undefined ? r.minor : 0n; });
      var sum = ws.reduce(function (a, b) { return a + b; }, 0n);
      ins.forEach(function (i, k) {
        var out = document.querySelector(".share-pct[data-for='" + i.name.replace("share_", "") + "']");
        if (!out) return;
        out.classList.toggle("neg", !!rs[k].error);
        out.textContent = rs[k].error || "";
      });
      // 16.6667 + 16.6667 + 16.6667 + 50 = 100.0001 counts as 100% (rounded thirds)
      var n = BigInt(ws.filter(function (w) { return w > 0n; }).length), diff = sum - 1000000n;
      if (diff < 0n) diff = -diff;
      var ok = !bad && n > 0n && 2n * diff <= n;
      st.textContent = bad ? "—" : ok ? "100% ✓" : fmtMinor(sum, 4) + "%";
      st.className = "num " + (ok ? "ok" : "neg");
    }
  }
  document.querySelectorAll("input[name=mode]").forEach(function (r) { r.addEventListener("change", update); });
  document.querySelectorAll(".share-in,.ob-debit,.ob-credit,[name=quantity],[name=unit_price],[name=amount]").forEach(function (i) {
    i.addEventListener("input", update);
  });
  update();
  // opening: add an asset row
  var add = document.getElementById("add-asset-row");
  if (add) add.addEventListener("click", function () {
    var body = document.querySelector("#asset-rows tbody"), last = body.lastElementChild, n = body.children.length;
    var row = last.cloneNode(true);
    row.querySelectorAll("input,select").forEach(function (el) {
      el.name = el.name.replace(/_\d+$/, "_" + n);
      if (el.dataset.testid) el.dataset.testid = el.dataset.testid.replace(/-\d+$/, "-" + n);
      if (el.tagName === "SELECT") el.selectedIndex = 0; else el.value = "";
    });
    row.querySelectorAll(".field-error").forEach(function (e) { e.remove(); });
    body.appendChild(row);
    row.querySelectorAll(".ob-debit").forEach(function (i) { i.addEventListener("input", update); });
  });
  // item rows (purchase / stock issue): add a row, line totals, purchase total and remaining debt
  var itemTable = document.getElementById("item-rows");
  function itemRowsUpdate() {
    if (!itemTable) return;
    var total = 0n, anyTotal = false;
    itemTable.querySelectorAll("tbody tr").forEach(function (tr) {
      var q = parse((tr.querySelector(".it-qty") || {}).value, 3), sel = tr.querySelector(".item-sel");
      var out = tr.querySelector(".it-total");
      if (out) {
        var p = parse((tr.querySelector(".it-price") || {}).value, 2);
        if (q.minor !== undefined && p.minor !== undefined) {
          var n = q.minor * p.minor, r = n % 1000n, t = n / 1000n + (2n * r >= 1000n ? 1n : 0n);
          out.textContent = fmtMinor(t, 2); total += t; anyTotal = true;
        } else out.textContent = "—";
      }
      var st = tr.querySelector(".it-stock");
      if (st && sel) {
        var o = sel.options[sel.selectedIndex];
        st.textContent = o && o.value ? (o.dataset.stock || "0") + " " + (o.dataset.unit || "") : "";
      }
    });
    var pt = document.getElementById("purchase-total");
    if (pt) {
      pt.textContent = anyTotal ? fmtMinor(total, 2) : "—";
      var paid = val("paid", 2) || 0n, debt = total - paid;
      var pd = document.getElementById("purchase-debt");
      pd.textContent = anyTotal ? fmtMinor(debt < 0n ? 0n : debt, 2) : "—";
    }
  }
  if (itemTable) {
    itemTable.addEventListener("input", itemRowsUpdate);
    itemTable.addEventListener("change", itemRowsUpdate);
    var paidIn = document.querySelector("[name=paid]");
    if (paidIn) paidIn.addEventListener("input", itemRowsUpdate);
    var addItem = document.getElementById("add-item-row");
    if (addItem) addItem.addEventListener("click", function () {
      var body = itemTable.querySelector("tbody"), last = body.lastElementChild, n = body.children.length;
      var row = last.cloneNode(true);
      row.querySelectorAll("input,select").forEach(function (el) {
        el.name = el.name.replace(/_\d+$/, "_" + n);
        if (el.dataset.testid) el.dataset.testid = el.dataset.testid.replace(/-\d+$/, "-" + n);
        if (el.tagName === "SELECT") el.selectedIndex = 0; else el.value = "";
      });
      row.querySelectorAll(".preview,.it-total,.it-stock").forEach(function (e) { e.textContent = ""; });
      body.appendChild(row);
      row.querySelectorAll("input.numin").forEach(function (i) { i.addEventListener("input", function () { preview(i); }); });
    });
    itemRowsUpdate();
  }
  // production: show the recipe of the chosen project only
  var prodProject = document.querySelector("[data-testid=doc-form] [name=project_id]");
  function recipeInfo() {
    var v = prodProject ? prodProject.value : "";
    document.querySelectorAll(".recipe-info").forEach(function (d) { d.style.display = d.dataset.project === v ? "" : "none"; });
  }
  if (prodProject && document.querySelector(".recipe-info")) { prodProject.addEventListener("change", recipeInfo); recipeInfo(); }
  // payment to supplier: show what we owe him and warn when paying more
  var supDebt = document.getElementById("sup-debt");
  function supDebtUpdate() {
    if (!supDebt) return;
    var debts = JSON.parse(supDebt.dataset.debts || "{}"), sel = document.querySelector("[name=supplier_id]");
    var id = sel ? sel.value : "";
    if (!id || debts[id] === undefined) { supDebt.textContent = ""; supDebt.style.display = "none"; return; }
    supDebt.style.display = "";
    var owed = BigInt(debts[id]), amt = val("amount", 2);
    var txt = (owed >= 0n ? supDebt.dataset.lOwed + " " + fmtMinor(owed, 2) : supDebt.dataset.lAdv + " " + fmtMinor(-owed, 2));
    var over = amt !== null && amt > (owed > 0n ? owed : 0n);
    supDebt.textContent = txt + (over ? " — " + supDebt.dataset.lWarn : "");
    supDebt.classList.toggle("warnbox", over);
  }
  if (supDebt) {
    document.querySelectorAll("[name=supplier_id],[name=amount]").forEach(function (el) {
      el.addEventListener("input", supDebtUpdate); el.addEventListener("change", supDebtUpdate);
    });
    supDebtUpdate();
  }
  // shares of one project: by amounts paid (exact proportion) or by percentages
  var pst = document.getElementById("pshare-total");
  function pshareUpdate() {
    if (!pst) return;
    var amounts = (document.querySelector("input[name=mode][value=amounts]") || {}).checked;
    var ins = Array.prototype.slice.call(document.querySelectorAll(".pshare-in"));
    var rs = ins.map(function (i) { return parse(i.value, amounts ? 2 : 4); });
    var bad = rs.some(function (r) { return !!r.error; });
    var ws = rs.map(function (r) { return r.minor !== undefined ? r.minor : 0n; });
    var sum = ws.reduce(function (a, b) { return a + b; }, 0n);
    ins.forEach(function (i, k) {
      var out = document.querySelector(".pshare-pct[data-for='" + i.name.replace("pshare_", "") + "']");
      if (!out) return;
      out.classList.toggle("neg", !!rs[k].error);
      out.textContent = rs[k].error ? rs[k].error : (amounts && sum > 0n && ws[k] > 0n ? "= " + fmtMinor((ws[k] * 2000000n + sum) / (2n * sum), 4) + "%" : "");
    });
    if (bad) { pst.textContent = "—"; pst.className = "num neg"; return; }
    if (amounts) { pst.textContent = fmtMinor(sum, 2); pst.className = "num " + (sum > 0n ? "ok" : "neg"); }
    else {
      var n = BigInt(ws.filter(function (w) { return w > 0n; }).length), diff = sum - 1000000n;
      if (diff < 0n) diff = -diff;
      var ok = n > 0n && 2n * diff <= n;
      pst.textContent = ok ? "100% ✓" : fmtMinor(sum, 4) + "%"; pst.className = "num " + (ok ? "ok" : "neg");
    }
  }
  if (pst) {
    document.querySelectorAll(".pshare-in, #pshares-form input[name=mode]").forEach(function (el) {
      el.addEventListener("input", pshareUpdate); el.addEventListener("change", pshareUpdate);
    });
    pshareUpdate();
  }
  // confirmation before destructive actions
  document.querySelectorAll("form.confirm").forEach(function (f) {
    f.addEventListener("submit", function (e) { if (!window.confirm(f.dataset.confirm)) e.preventDefault(); });
  });
})();
