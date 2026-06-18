const COPY_ICON =
  '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><rect x="8" y="8" width="11" height="11" rx="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v1"></path></svg>';
const CHECK_ICON =
  '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M20 6 9 17l-5-5"></path></svg>';

async function copyText(value: string) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.readOnly = true;
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  document.body.append(textarea);
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
}

function initializeCopyButtons() {
  document.querySelectorAll<HTMLElement>(".post-content pre").forEach((pre) => {
    if (pre.dataset.copyBound === "true" || !pre.querySelector("code")) return;
    pre.dataset.copyBound = "true";
    pre.classList.add("has-copy-button");

    const button = document.createElement("button");
    button.type = "button";
    button.className = "code-copy-button";
    button.innerHTML = COPY_ICON;
    button.title = "复制代码";
    button.ariaLabel = "复制代码";
    button.addEventListener("click", async () => {
      const code = pre.querySelector("code");
      if (!code) return;
      await copyText((code as HTMLElement).innerText);
      button.innerHTML = CHECK_ICON;
      button.classList.add("copied");
      button.title = "已复制";
      window.setTimeout(() => {
        button.innerHTML = COPY_ICON;
        button.classList.remove("copied");
        button.title = "复制代码";
      }, 1400);
    });
    pre.append(button);
  });
}

document.addEventListener("astro:page-load", initializeCopyButtons);
initializeCopyButtons();
