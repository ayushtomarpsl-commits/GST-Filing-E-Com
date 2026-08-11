(function () {
  "use strict";

  var STORAGE_KEY = "gstHelperTourDone";

  /* Each step: targets = list of element ids (first one found is used),
     or null for a centered step with no spotlight. */
  var STEPS = [
    {
      targets: null,
      title: "Welcome",
      text: "Welcome! This tool reads your Amazon, Flipkart and Meesho tax reports. It builds the JSON files for GSTR-1 and GSTR-3B, ready for the GST portal."
    },
    {
      targets: ["folderPickerBtn", "folder_path"],
      title: "One folder with all reports",
      text: "Put ALL the reports you downloaded into one folder (zips are fine — no need to extract). Then select that folder here."
    },
    {
      targets: ["period"],
      title: "Month is automatic",
      text: "Leave the month empty — it is detected from your files. Fill it only if you want to force a month (MMYYYY)."
    },
    {
      targets: ["submitBtn"],
      title: "Process your reports",
      text: "Click here. You will get one card per GSTR-1 table, a complete GSTR-1 JSON, and a GSTR-3B card. Read every red/yellow message before filing."
    },
    {
      targets: null,
      title: "On the results page",
      text: "On the results page, download the big 'Complete GSTR-1 JSON' card. Upload that file on the GST portal with 'Prepare Offline'. The GSTR-3B card shows every value to verify."
    },
    {
      targets: ["navHelp"],
      title: "Need more help?",
      text: "Full picture guide is here: which report to download from each website, with screens, and how to upload on the GST portal."
    }
  ];

  var overlay = null;
  var spotlight = null;
  var card = null;
  var stepEl = null;
  var titleEl = null;
  var textEl = null;
  var backBtn = null;
  var nextBtn = null;
  var skipBtn = null;
  var activeSteps = [];
  var current = 0;
  var running = false;

  function isIndexPage() {
    return !!document.getElementById("processForm");
  }

  function targetFor(step) {
    if (!step.targets) return null;
    for (var i = 0; i < step.targets.length; i++) {
      var el = document.getElementById(step.targets[i]);
      if (el) return el;
    }
    return null;
  }

  /* Steps whose target element is missing are dropped automatically
     (e.g. folder_path does not exist in public mode). */
  function collectSteps() {
    var list = [];
    for (var i = 0; i < STEPS.length; i++) {
      if (!STEPS[i].targets || targetFor(STEPS[i])) {
        list.push(STEPS[i]);
      }
    }
    return list;
  }

  function markDone() {
    try {
      window.localStorage.setItem(STORAGE_KEY, "1");
    } catch (e) {
      /* private mode / storage blocked — ignore */
    }
  }

  function isDone() {
    try {
      return window.localStorage.getItem(STORAGE_KEY) === "1";
    } catch (e) {
      return false;
    }
  }

  function onKey(e) {
    var key = e.key || e.keyCode;
    if (key === "Escape" || key === "Esc" || key === 27) {
      endTour();
    }
  }

  function position() {
    if (!running || !card) return;
    var step = activeSteps[current];
    var el = targetFor(step);
    var pad = 6;
    var gap = 14;
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    var top;
    var left;

    if (el) {
      var r = el.getBoundingClientRect();
      overlay.className = "gst-tour-overlay";
      spotlight.style.display = "block";
      spotlight.style.top = (r.top - pad) + "px";
      spotlight.style.left = (r.left - pad) + "px";
      spotlight.style.width = (r.width + pad * 2) + "px";
      spotlight.style.height = (r.height + pad * 2) + "px";
    } else {
      overlay.className = "gst-tour-overlay gst-tour-dimmed";
      spotlight.style.display = "none";
    }

    var cardW = card.offsetWidth;
    var cardH = card.offsetHeight;

    if (el) {
      var r2 = el.getBoundingClientRect();
      top = r2.bottom + pad + gap;
      if (top + cardH > vh - 10) {
        top = r2.top - pad - gap - cardH;
      }
      if (top < 10) top = 10;
      left = r2.left + r2.width / 2 - cardW / 2;
      if (left + cardW > vw - 10) left = vw - cardW - 10;
      if (left < 10) left = 10;
    } else {
      top = (vh - cardH) / 2;
      if (top < 10) top = 10;
      left = (vw - cardW) / 2;
      if (left < 10) left = 10;
    }

    card.style.top = top + "px";
    card.style.left = left + "px";
  }

  function showStep(index) {
    current = index;
    var step = activeSteps[index];
    var el = targetFor(step);
    var last = index === activeSteps.length - 1;

    stepEl.textContent = "Step " + (index + 1) + " of " + activeSteps.length;
    titleEl.textContent = step.title;
    textEl.textContent = step.text;
    backBtn.style.display = index === 0 ? "none" : "";
    nextBtn.textContent = last ? "Finish" : "Next";
    skipBtn.style.display = last ? "none" : "";

    if (el && el.scrollIntoView) {
      try {
        el.scrollIntoView({ block: "center" });
      } catch (e) {
        el.scrollIntoView();
      }
    }

    position();
    /* re-align once scroll/layout settles */
    window.setTimeout(position, 80);

    try {
      nextBtn.focus();
    } catch (e2) {
      /* ignore */
    }
  }

  function buildUi() {
    overlay = document.createElement("div");
    overlay.className = "gst-tour-overlay";

    spotlight = document.createElement("div");
    spotlight.className = "gst-tour-spotlight";
    spotlight.style.display = "none";

    card = document.createElement("div");
    card.className = "gst-tour-card";
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-live", "polite");

    stepEl = document.createElement("p");
    stepEl.className = "gst-tour-step";

    titleEl = document.createElement("h3");
    titleEl.className = "gst-tour-title";

    textEl = document.createElement("p");
    textEl.className = "gst-tour-text";

    var actions = document.createElement("div");
    actions.className = "gst-tour-actions";

    skipBtn = document.createElement("button");
    skipBtn.type = "button";
    skipBtn.className = "gst-tour-skip";
    skipBtn.textContent = "Skip";

    backBtn = document.createElement("button");
    backBtn.type = "button";
    backBtn.className = "gst-tour-btn gst-tour-back";
    backBtn.textContent = "Back";

    nextBtn = document.createElement("button");
    nextBtn.type = "button";
    nextBtn.className = "gst-tour-btn gst-tour-next";
    nextBtn.textContent = "Next";

    actions.appendChild(skipBtn);
    actions.appendChild(backBtn);
    actions.appendChild(nextBtn);

    card.appendChild(stepEl);
    card.appendChild(titleEl);
    card.appendChild(textEl);
    card.appendChild(actions);

    document.body.appendChild(overlay);
    document.body.appendChild(spotlight);
    document.body.appendChild(card);

    skipBtn.addEventListener("click", endTour);
    backBtn.addEventListener("click", function () {
      if (current > 0) showStep(current - 1);
    });
    nextBtn.addEventListener("click", function () {
      if (current >= activeSteps.length - 1) {
        endTour();
      } else {
        showStep(current + 1);
      }
    });
  }

  function endTour() {
    if (!running) return;
    running = false;
    markDone();
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("resize", position);
    window.removeEventListener("scroll", position, true);
    if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
    if (spotlight && spotlight.parentNode) spotlight.parentNode.removeChild(spotlight);
    if (card && card.parentNode) card.parentNode.removeChild(card);
    overlay = spotlight = card = null;
    stepEl = titleEl = textEl = backBtn = nextBtn = skipBtn = null;
  }

  function startTour() {
    if (running) return;
    if (!isIndexPage()) return;
    activeSteps = collectSteps();
    if (activeSteps.length === 0) return;
    buildUi();
    running = true;
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", position);
    window.addEventListener("scroll", position, true);
    showStep(0);
  }

  function hasGuideParam() {
    return /[?&]guide=1(&|$)/.test(window.location.search);
  }

  function cleanGuideParam() {
    if (!(window.history && window.history.replaceState)) return;
    var qs = window.location.search.replace(/([?&])guide=1(&?)/, function (m, p1, p2) {
      return p2 ? p1 : "";
    });
    if (qs === "?") qs = "";
    window.history.replaceState(
      null,
      document.title,
      window.location.pathname + qs + window.location.hash
    );
  }

  function init() {
    var guideBtn = document.getElementById("showGuideBtn");
    if (guideBtn) {
      guideBtn.addEventListener("click", function () {
        if (isIndexPage()) {
          startTour();
        } else {
          window.location.href = "/?guide=1";
        }
      });
    }

    if (!isIndexPage()) return;

    if (hasGuideParam()) {
      cleanGuideParam();
      window.setTimeout(startTour, 100);
      return;
    }

    if (!isDone()) {
      window.setTimeout(startTour, 600);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
