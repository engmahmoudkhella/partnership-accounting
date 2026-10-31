(function () {
  "use strict";
  var AR = document.documentElement.lang === "ar";
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
  }
  document.querySelectorAll("input[name=pay_source]").forEach(function (r) { r.addEventListener("change", paysource); });
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
      var ratio = (document.querySelector("input[name=mode]:checked") || {}).value === "ratio";
      var ins = Array.prototype.slice.call(document.querySelectorAll(".share-in"));
      var ws = ins.map(function (i) { var r = parse(i.value, ratio ? 0 : 4); return r.minor !== undefined ? r.minor : 0n; });
      var sum = ws.reduce(function (a, b) { return a + b; }, 0n);
      ins.forEach(function (i, k) {
        var out = document.querySelector(".share-pct[data-for='" + i.name.replace("share_", "") + "']");
        if (out) out.textContent = sum > 0n ? "= " + fmtMinor((ws[k] * 2000000n + sum) / (2n * sum), 4) + "%" : "";
      });
      if (ratio) { st.textContent = fmtMinor(sum, 0); st.className = "num " + (sum > 0n ? "ok" : "neg"); }
      else { st.textContent = fmtMinor(sum, 4) + "%"; st.className = "num " + (sum === 1000000n ? "ok" : "neg"); }
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
  // confirmation before destructive actions
  document.querySelectorAll("form.confirm").forEach(function (f) {
    f.addEventListener("submit", function (e) { if (!window.confirm(f.dataset.confirm)) e.preventDefault(); });
  });
})();
