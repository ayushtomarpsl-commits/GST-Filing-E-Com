(function () {
  "use strict";

  /* ---------- Index page: folder / manual-upload form ---------- */
  var form = document.getElementById("processForm");
  if (form) {
    var filesInput = document.getElementById("filesInput");
    var filesName = document.getElementById("filesName");
    var submitBtn = document.getElementById("submitBtn");

    if (filesInput && filesName) {
      filesInput.addEventListener("change", function () {
        var drop = filesInput.closest(".file-drop");
        var count = filesInput.files ? filesInput.files.length : 0;
        if (count > 0) {
          filesName.textContent =
            count === 1
              ? filesInput.files[0].name
              : count + " files selected";
          if (drop) drop.classList.add("has-file");
        } else {
          filesName.textContent = "No files chosen";
          if (drop) drop.classList.remove("has-file");
        }
      });
    }

    /* ---------- Folder picker (browse and upload a whole folder) ---------- */
    var folderInput = document.getElementById("folderInput");
    var folderPickerBtn = document.getElementById("folderPickerBtn");
    var folderSummary = document.getElementById("folderSummary");
    var folderFilesDetails = document.getElementById("folderFilesDetails");
    var folderFileList = document.getElementById("folderFileList");
    var uploadProgress = document.getElementById("uploadProgress");
    var uploadProgressFill = document.getElementById("uploadProgressFill");
    var uploadProgressText = document.getElementById("uploadProgressText");
    var uploadError = document.getElementById("uploadError");

    var keptFiles = [];
    var pickedFolderEmpty = false;
    var uploading = false;
    var ALLOWED_EXT = /\.(csv|xlsx|xls|zip|json)$/i;
    var MAX_FILE_BYTES = 200 * 1024 * 1024; /* 200 MB per file */
    /* The server rejects the WHOLE request above 200 MB — keep a margin
       for multipart overhead so we can explain instead of dying with 413. */
    var MAX_TOTAL_BYTES = 199 * 1024 * 1024;

    function showEl(el) {
      if (el) el.hidden = false;
    }

    function hideEl(el) {
      if (el) el.hidden = true;
    }

    function showInlineError(msg) {
      if (uploadError) {
        uploadError.textContent = msg;
        showEl(uploadError);
      }
    }

    function pickedFolderName(files) {
      for (var i = 0; i < files.length; i++) {
        var rel = files[i].webkitRelativePath || "";
        var slash = rel.indexOf("/");
        if (slash > 0) return rel.slice(0, slash);
      }
      return "";
    }

    function setProgress(pct, label) {
      if (uploadProgressFill) uploadProgressFill.style.width = pct + "%";
      if (uploadProgressText) uploadProgressText.textContent = label;
    }

    if (folderPickerBtn && folderInput) {
      folderPickerBtn.addEventListener("click", function () {
        folderInput.click();
      });

      folderInput.addEventListener("change", function () {
        var all = folderInput.files || [];
        keptFiles = [];
        pickedFolderEmpty = false;
        hideEl(uploadError);
        if (folderFileList) folderFileList.innerHTML = "";

        if (!all.length) {
          hideEl(folderSummary);
          hideEl(folderFilesDetails);
          return;
        }

        var skipped = 0;
        for (var i = 0; i < all.length; i++) {
          var f = all[i];
          if (ALLOWED_EXT.test(f.name) && f.size < MAX_FILE_BYTES) {
            keptFiles.push(f);
          } else {
            skipped++;
          }
        }

        var folderName = pickedFolderName(all);
        var fromFolder = folderName ? ' from folder "' + folderName + '"' : "";

        if (folderSummary) {
          folderSummary.className = "picker-summary";
          if (keptFiles.length === 0) {
            pickedFolderEmpty = true;
            folderSummary.className += " is-error";
            folderSummary.textContent =
              "No report files found" + fromFolder +
              ". I need .csv, .xlsx, .xls, .zip or .json files under 200 MB. " +
              "Nothing will be uploaded — please pick another folder.";
          } else {
            folderSummary.className += " is-ok";
            var text =
              keptFiles.length +
              " report file" + (keptFiles.length === 1 ? "" : "s") +
              " selected" + fromFolder;
            if (skipped > 0) {
              text +=
                " — " + skipped +
                " other file" + (skipped === 1 ? "" : "s") +
                " skipped (PDFs/images are not needed for filing).";
            } else {
              text += ".";
            }
            folderSummary.textContent = text;
          }
          showEl(folderSummary);
        }

        if (folderFilesDetails && folderFileList) {
          if (keptFiles.length > 0) {
            for (var j = 0; j < keptFiles.length; j++) {
              var li = document.createElement("li");
              li.textContent = keptFiles[j].webkitRelativePath || keptFiles[j].name;
              folderFileList.appendChild(li);
            }
            var sum = folderFilesDetails.querySelector("summary");
            if (sum) {
              sum.textContent =
                "Show the " + keptFiles.length +
                " file" + (keptFiles.length === 1 ? "" : "s") +
                " that will be uploaded";
            }
            folderFilesDetails.open = false;
            showEl(folderFilesDetails);
          } else {
            hideEl(folderFilesDetails);
          }
        }
      });
    }

    function totalUploadBytes() {
      var total = 0;
      var i;
      if (filesInput && filesInput.files) {
        for (i = 0; i < filesInput.files.length; i++) {
          total += filesInput.files[i].size;
        }
      }
      for (i = 0; i < keptFiles.length; i++) {
        total += keptFiles[i].size;
      }
      return total;
    }

    function uploadFolderFiles() {
      /* Block over-size uploads BEFORE sending — the server would reject the
         whole request (413) and leave the visitor on a dead page. */
      if (totalUploadBytes() > MAX_TOTAL_BYTES) {
        showInlineError(
          "Your files together are larger than the 200 MB limit. " +
          "Remove big files that are not reports (videos, photos, backups) " +
          "from the folder and try again."
        );
        return;
      }
      uploading = true;
      hideEl(uploadError);

      var origLabel = submitBtn ? submitBtn.textContent : "";
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = "Uploading…";
      }

      var fd = new FormData();
      var periodEl = document.getElementById("period");
      fd.append("period", periodEl ? periodEl.value : "");
      var folderPathEl = document.getElementById("folder_path");
      if (folderPathEl) fd.append("folder_path", folderPathEl.value);

      /* Manually chosen files (if any) go along too, exactly as a native
         submit would have sent them. */
      if (filesInput && filesInput.files) {
        for (var i = 0; i < filesInput.files.length; i++) {
          var mf = filesInput.files[i];
          fd.append("files", mf, mf.name);
        }
      }
      /* Folder files: keep the relative path as the filename so the server
         sees where each file sat inside the folder. */
      for (var j = 0; j < keptFiles.length; j++) {
        var kf = keptFiles[j];
        fd.append("files", kf, kf.webkitRelativePath || kf.name);
      }

      if (uploadProgress) {
        uploadProgress.className = "upload-progress";
        showEl(uploadProgress);
      }
      setProgress(0, "Uploading… 0%");

      var xhr = new XMLHttpRequest();
      xhr.open("POST", form.getAttribute("action") || "/process", true);

      xhr.upload.onprogress = function (e) {
        if (e.lengthComputable && e.total > 0) {
          var pct = Math.round((e.loaded / e.total) * 100);
          if (pct > 100) pct = 100;
          setProgress(pct, "Uploading… " + pct + "%");
        }
      };

      xhr.upload.onload = function () {
        if (uploadProgress) uploadProgress.className = "upload-progress is-processing";
        setProgress(100, "Processing on server… this can take a minute.");
        if (submitBtn) submitBtn.textContent = "Processing…";
      };

      xhr.onload = function () {
        /* Success or an error page — show whatever the server returned. */
        document.open();
        document.write(xhr.responseText);
        document.close();
      };

      function failNetwork() {
        uploading = false;
        hideEl(uploadProgress);
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.textContent = origLabel;
        }
        showInlineError(
          "Upload failed. Please check your internet connection and try " +
          "again. If your folder is very big, try uploading fewer files."
        );
      }

      xhr.onerror = failNetwork;
      xhr.onabort = failNetwork;
      xhr.ontimeout = failNetwork;

      xhr.send(fd);
    }

    form.addEventListener("submit", function (ev) {
      if (keptFiles.length > 0) {
        /* Folder files picked — upload them ourselves with progress. */
        ev.preventDefault();
        if (!uploading) uploadFolderFiles();
        return;
      }

      /* A folder was picked but it had no usable files. If there is no
         other source of files either, block the empty submit and explain. */
      if (pickedFolderEmpty) {
        var folderPathEl = document.getElementById("folder_path");
        var hasPath =
          folderPathEl &&
          folderPathEl.value.replace(/^\s+|\s+$/g, "") !== "";
        var hasManual =
          filesInput && filesInput.files && filesInput.files.length > 0;
        if (!hasPath && !hasManual) {
          ev.preventDefault();
          showInlineError(
            "The picked folder has no report files. Pick another folder or upload files manually."
          );
          return;
        }
      }

      /* No folder files — keep today's native submit unchanged. */
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = "Processing…";
      }
    });
  }

  /* ---------- Results page: Copy JSON buttons ---------- */
  var copyButtons = document.querySelectorAll(".btn-copy[data-json-id]");
  Array.prototype.forEach.call(copyButtons, function (btn) {
    btn.addEventListener("click", function () {
      var source = document.getElementById(btn.getAttribute("data-json-id"));
      if (!source) return;
      var text = source.textContent;

      function markCopied() {
        if (!btn.getAttribute("data-orig-label")) {
          btn.setAttribute("data-orig-label", btn.textContent);
        }
        btn.textContent = "Copied ✓";
        btn.classList.add("copied");
        if (btn._copyTimer) clearTimeout(btn._copyTimer);
        btn._copyTimer = setTimeout(function () {
          btn.textContent = btn.getAttribute("data-orig-label");
          btn.classList.remove("copied");
          btn._copyTimer = null;
        }, 2000);
      }

      function fallbackCopy() {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        var ok = false;
        try {
          ok = document.execCommand("copy");
        } catch (e) {
          ok = false;
        }
        document.body.removeChild(ta);
        if (ok) markCopied();
      }

      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(markCopied, fallbackCopy);
      } else {
        fallbackCopy();
      }
    });
  });
})();
